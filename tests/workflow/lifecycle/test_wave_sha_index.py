"""Unit tests for the P30-I14-W10 one-pass wave-SHA index.

Covers :func:`eawf.workflow.lifecycle.wave_sha.build_wave_sha_index` and the
three consumers it speeds up (``derive_wave_sha`` via the index fast path,
``detect_git_state_drift``, ``scan_commit_pins``):

- the bulk reconciler walks history in a SINGLE ``git log`` subprocess
  instead of one ``git log --grep`` per closed wave (timed + call-counted);
- twin commits (same wave subject on an integration ref AND a transient
  worktree/agent ref) resolve DETERMINISTICALLY to the integration SHA, so
  no phantom ``pinned_mismatch`` surfaces;
- the prefix-form (canonical-then-alt) + ``Eawf-Wave`` trailer fallback +
  most-recent-wins semantics survive the refactor;
- boundary: empty history and git-absent both degrade to an empty map / None.

The twin-commit + boundary cases use a real tmp git repo so the
``--source`` reachability annotation that drives twin resolution is
exercised end to end; the single-pass timing test monkeypatches
``subprocess.run`` so the call count is deterministic without disk IO.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.models import State
from eawf.workflow.lifecycle.wave_sha import (
    _parse_index,
    build_wave_sha_index,
    derive_wave_sha,
    detect_git_state_drift,
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


# ---- minimal State payload -------------------------------------------------


def _base_state_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ZZ",
        "updated_at": "2026-05-27T00:00:00Z",
        "project": {
            "code": "ZZ",
            "slug": "zz",
            "title": "ZZ",
            "description": "",
            "domains": [],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ZZ",
        },
        "current": {
            "project_code": "ZZ",
            "subproject_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _wave_payload(
    wave_id: str,
    *,
    status: str = "closed",
    commit: str | None = None,
) -> dict[str, Any]:
    return {
        "id": wave_id,
        "iter_id": wave_id.rsplit("-", 1)[0],
        "title": f"wave {wave_id}",
        "status": status,
        "deps": [],
        "blocks": [],
        "file_scopes": [],
        "claim_session_id": None,
        "worktree_id": None,
        "token_budget": None,
        "tokens_consumed": 0,
        "outcome": None,
        "commit": commit,
        "opened_at": "2026-05-27T00:00:00Z",
        "closed_at": "2026-05-27T00:01:00Z" if status == "closed" else None,
    }


def _state_with_waves(waves: list[dict[str, Any]]) -> State:
    payload = _base_state_payload()
    payload["waves"] = {w["id"]: w for w in waves}
    return State.model_validate(payload)


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


@_GIT
def test_twin_commit_no_phantom_pinned_mismatch(tmp_path: Path) -> None:
    """A wave pinned to the integration SHA does NOT drift against a twin worktree ref.

    The regression: before the index, ``git log --all --grep`` could return
    the transient worktree commit (a TWIN of the same subject) and report a
    phantom ``pinned_mismatch`` even though the state pin is correct.
    """
    _init_repo(tmp_path)
    integ = _commit(tmp_path, name="a.txt", msg="[P30-I07-W08] feat: on main")
    _run(tmp_path, "checkout", "-q", "-b", "worktree-agent-abc123")
    _commit(tmp_path, name="b.txt", msg="[P30-I07-W08] feat: twin on worktree")
    _run(tmp_path, "checkout", "-q", "main")

    state = _state_with_waves([_wave_payload("P30-I07-W08", commit=integ)])
    drifts = detect_git_state_drift(state, repo_root=tmp_path)
    assert drifts == []


# ---- single-pass + timing (criterion 1) ------------------------------------


def test_detect_drift_uses_single_git_log_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole-state reconciler shells out ONCE, not per closed wave."""
    git_log_calls: list[list[str]] = []

    # Build a one-pass log payload that resolves every wave to its own SHA.
    waves = [f"P30-I07-W{n:02d}" for n in range(1, 41)]
    records: list[str] = []
    sep_rec = "\x00"
    sep_field = "\x1f"
    sha_by_wave: dict[str, str] = {}
    for n, wid in enumerate(waves, start=1):
        sha = f"{n:040x}"
        sha_by_wave[wid] = sha
        subject = f"[{wid}] feat: w{n}"
        records.append(sep_rec + sep_field.join((sha, "refs/heads/main", subject, "")))
    fake_log = "".join(records)

    class _Completed:
        returncode = 0
        stderr = ""

        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def _fake_run(cmd: list[str], **_kw: Any) -> _Completed:
        if cmd[:2] == ["git", "log"]:
            git_log_calls.append(cmd)
            return _Completed(fake_log)
        return _Completed("")

    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.subprocess.run", _fake_run)

    state = _state_with_waves([_wave_payload(wid, commit=sha_by_wave[wid]) for wid in waves])

    start = time.perf_counter()
    drifts = detect_git_state_drift(state, repo_root=tmp_path)
    elapsed = time.perf_counter() - start

    # Single bulk-path subprocess, not one per closed wave.
    assert len(git_log_calls) == 1
    assert "--source" in git_log_calls[0]
    # Every pin matches its derived SHA -> no drift.
    assert drifts == []
    # Comfortably under the 2s budget (this is a pure dict lookup loop).
    assert elapsed < 2.0


def test_detect_drift_single_pass_classifies_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The single index pass still classifies a real mismatch correctly."""
    git_log_calls: list[list[str]] = []
    sep_rec = "\x00"
    sep_field = "\x1f"
    # git derives "b"*40 for the wave; state pins "a"*40 -> mismatch.
    record = sep_rec + sep_field.join(("b" * 40, "refs/heads/main", "[P30-I07-W01] feat: w1", ""))

    class _Completed:
        returncode = 0
        stderr = ""

        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def _fake_run(cmd: list[str], **_kw: Any) -> _Completed:
        if cmd[:2] == ["git", "log"]:
            git_log_calls.append(cmd)
            return _Completed(record)
        return _Completed("")

    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.subprocess.run", _fake_run)

    state = _state_with_waves([_wave_payload("P30-I07-W01", commit="a" * 40)])
    drifts = detect_git_state_drift(state, repo_root=tmp_path)
    assert len(git_log_calls) == 1
    assert len(drifts) == 1
    assert drifts[0].kind == "pinned_mismatch"
    assert drifts[0].git_commit == "b" * 40


# ---- boundary: empty history + git-absent (criterion 4) --------------------


@_GIT
def test_build_index_empty_history_is_empty_map(tmp_path: Path) -> None:
    """A repo with no commits yields an empty index, not a raise."""
    _init_repo(tmp_path)
    assert build_wave_sha_index(tmp_path) == {}


def test_build_index_git_absent_is_empty_map(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: None)
    assert build_wave_sha_index(tmp_path) == {}


def test_build_index_timeout_is_empty_map(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _raise_timeout(*_a: Any, **_k: Any) -> None:
        raise subprocess.TimeoutExpired(cmd="git", timeout=1.0)

    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.subprocess.run", _raise_timeout)
    assert build_wave_sha_index(tmp_path) == {}


def test_build_index_non_zero_rc_is_empty_map(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _Completed:
        returncode = 128
        stdout = ""
        stderr = "fatal: not a git repository\n"

    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.subprocess.run", lambda *a, **k: _Completed()
    )
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
