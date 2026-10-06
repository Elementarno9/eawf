"""Tests for the one-pass wave-SHA index in :mod:`eawf.runtime.vcs.wave_sha`.

Covers :func:`eawf.runtime.vcs.wave_sha.build_wave_sha_index` and the
``derive_wave_sha`` index fast path:

- twin commits (same wave subject on an integration ref AND a transient
  worktree/agent ref) resolve DETERMINISTICALLY to the integration SHA;
- the prefix-form (canonical-then-alt) + ``Eawf-Wave`` trailer fallback +
  most-recent-wins semantics hold;
- boundary: empty history and git-absent both degrade to an empty map / None.

The twin-commit + boundary cases use a real tmp git repo so the
``--source`` reachability annotation that drives twin resolution is
exercised end to end.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.vcs.wave_sha import (
    _parse_index,
    build_wave_sha_index,
    commit_identity_digest,
    derive_wave_sha,
)

pytestmark = pytest.mark.filterwarnings("ignore")


# ---- real-git helpers ------------------------------------------------------


def _run(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _run(root, "init", "-q", "-b", "main")
    _run(root, "config", "user.email", "test@example.com")
    _run(root, "config", "user.name", "Test")


def _commit(root: Path, *, name: str, msg: str) -> str:
    (root / name).write_text("x\n", encoding="utf-8")
    _run(root, "add", name)
    _run(root, "commit", "-q", "-m", msg)
    return _run(root, "rev-parse", "HEAD")


_GIT = pytest.mark.skipif(shutil.which("git") is None, reason="git required")


# ---- build_wave_sha_index: prefix + trailer + ordering ---------------------


@_GIT
def test_build_index_maps_bracket_prefix_and_trailer(tmp_path: Path) -> None:
    """One pass indexes both the bracketed subject prefix AND the trailer."""
    _init_repo(tmp_path)
    sha_pref = _commit(tmp_path, name="a.txt", msg="[P30-I07-W08] feat: prefix form")
    body = "feat: trailer form\n\nEawf-Wave: P30-I07-W09"
    sha_trail = _commit(tmp_path, name="b.txt", msg=body)

    index = build_wave_sha_index(tmp_path)
    assert index["[P30-I07-W08]"] == sha_pref
    assert index["P30-I07-W09"] == sha_trail


@_GIT
def test_build_index_most_recent_wins_within_tier(tmp_path: Path) -> None:
    """Two integration commits with the same key -> the newer SHA wins."""
    _init_repo(tmp_path)
    _commit(tmp_path, name="a.txt", msg="[P30-I07-W08] feat: first")
    newer = _commit(tmp_path, name="b.txt", msg="[P30-I07-W08] feat: second (newer)")

    index = build_wave_sha_index(tmp_path)
    assert index["[P30-I07-W08]"] == newer


@_GIT
def test_derive_wave_sha_via_index_tries_alt_prefix_form(tmp_path: Path) -> None:
    """An I01 wave committed in the long form still resolves through the index."""
    _init_repo(tmp_path)
    # Committed as the long form; canonical for an I01 wave is the short form.
    sha = _commit(tmp_path, name="a.txt", msg="[P30-I01-W03] feat: long form for I01")

    index = build_wave_sha_index(tmp_path)
    # derive_wave_sha consults canonical [P30-W03] first, then alt [P30-I01-W03].
    assert derive_wave_sha("P30-I01-W03", index=index) == sha


@_GIT
def test_commit_identity_digest_survives_rebase_twin(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, name="base.txt", msg="chore: base")
    _run(tmp_path, "switch", "-q", "-c", "topic")
    original = _commit(tmp_path, name="wave.txt", msg="[P30-I07-W01] feat: stable patch")
    _run(tmp_path, "switch", "-q", "main")
    _commit(tmp_path, name="other.txt", msg="chore: move parent")
    _run(tmp_path, "cherry-pick", original)
    rebased = _run(tmp_path, "rev-parse", "HEAD")

    assert rebased != original
    assert commit_identity_digest(original, repo_root=tmp_path) == commit_identity_digest(
        rebased,
        repo_root=tmp_path,
    )


# ---- twin-commit determinism (criterion 3) ---------------------------------


@_GIT
def test_twin_commit_resolves_to_integration_sha(tmp_path: Path) -> None:
    """Same wave subject on main + a worktree-agent ref -> integration SHA wins."""
    _init_repo(tmp_path)
    integ = _commit(tmp_path, name="a.txt", msg="[P30-I07-W08] feat: on main")
    # A transient per-wave ref carrying a TWIN subject at a different SHA.
    _run(tmp_path, "checkout", "-q", "-b", "worktree-agent-abc123")
    transient = _commit(tmp_path, name="b.txt", msg="[P30-I07-W08] feat: twin on worktree")
    _run(tmp_path, "checkout", "-q", "main")
    assert integ != transient

    index = build_wave_sha_index(tmp_path)
    assert index["[P30-I07-W08]"] == integ


# ---- boundary: empty history + git-absent (criterion 4) --------------------


@_GIT
def test_build_index_empty_history_is_empty_map(tmp_path: Path) -> None:
    """A repo with no commits yields an empty index, not a raise."""
    _init_repo(tmp_path)
    assert build_wave_sha_index(tmp_path) == {}


def test_build_index_git_absent_is_empty_map(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("eawf.runtime.vcs.wave_sha.shutil.which", lambda _: None)
    assert build_wave_sha_index(tmp_path) == {}


def test_build_index_timeout_is_empty_map(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _raise_timeout(*_a: Any, **_k: Any) -> None:
        raise subprocess.TimeoutExpired(cmd="git", timeout=1.0)

    monkeypatch.setattr("eawf.runtime.vcs.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr("eawf.runtime.vcs.wave_sha.subprocess.run", _raise_timeout)
    assert build_wave_sha_index(tmp_path) == {}


def test_build_index_non_zero_rc_is_empty_map(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _Completed:
        returncode = 128
        stdout = ""
        stderr = "fatal: not a git repository\n"

    monkeypatch.setattr("eawf.runtime.vcs.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr("eawf.runtime.vcs.wave_sha.subprocess.run", lambda *a, **k: _Completed())
    assert build_wave_sha_index(tmp_path) == {}


def test_derive_wave_sha_index_miss_returns_none() -> None:
    """An index that lacks every candidate key resolves to None."""
    assert derive_wave_sha("P30-I07-W08", index={}) is None
    assert derive_wave_sha("not-a-wave", index={"[P30-I07-W08]": "a" * 40}) is None


# ---- _parse_index unit semantics -------------------------------------------


def test_parse_index_integration_beats_transient_regardless_of_order() -> None:
    """Even when the transient sighting comes FIRST, integration still wins.

    ``git log`` is newest-first, so a transient twin can precede the
    integration commit in the stream; the tier guard must still prefer the
    integration SHA.
    """
    sep_rec = "\x00"
    sep_field = "\x1f"
    transient = sep_rec + sep_field.join(
        ("t" * 40, "refs/heads/worktree-agent-abc", "[P30-I07-W08] feat: twin", "")
    )
    integ = sep_rec + sep_field.join(
        ("i" * 40, "refs/heads/feature/eawf-v0.6-p30", "[P30-I07-W08] feat: real", "")
    )
    index = _parse_index(transient + integ)
    assert index["[P30-I07-W08]"] == "i" * 40


def test_parse_index_transient_fills_key_unseen_on_integration() -> None:
    """A wave that only ever landed on a transient ref is still discoverable."""
    sep_rec = "\x00"
    sep_field = "\x1f"
    transient = sep_rec + sep_field.join(
        ("t" * 40, "refs/heads/worktree-agent-abc", "[P30-I07-W08] feat: only here", "")
    )
    index = _parse_index(transient)
    assert index["[P30-I07-W08]"] == "t" * 40


def test_parse_index_worktree_branch_suffix_is_transient() -> None:
    """A ``-pNN-wMM`` per-wave worktree branch counts as a transient ref."""
    sep_rec = "\x00"
    sep_field = "\x1f"
    transient = sep_rec + sep_field.join(
        ("t" * 40, "refs/heads/feature/eawf-v0.6-p30-w10", "[P30-I07-W08] feat: twin", "")
    )
    integ = sep_rec + sep_field.join(("i" * 40, "refs/heads/main", "[P30-I07-W08] feat: real", ""))
    index = _parse_index(transient + integ)
    assert index["[P30-I07-W08]"] == "i" * 40


def test_parse_index_skips_malformed_records() -> None:
    """Records with too few fields or a blank SHA are ignored, not raised."""
    sep_rec = "\x00"
    sep_field = "\x1f"
    good = sep_rec + sep_field.join(("a" * 40, "refs/heads/main", "[P30-I07-W08] feat: ok", ""))
    blank_sha = sep_rec + sep_field.join(("", "refs/heads/main", "[P30-I07-W09] x", ""))
    too_few = sep_rec + "only-one-field"
    index = _parse_index(good + blank_sha + too_few)
    assert index == {"[P30-I07-W08]": "a" * 40}
