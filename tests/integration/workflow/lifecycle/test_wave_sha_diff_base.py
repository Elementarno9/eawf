"""Unit tests for the W15 ``derive_diff_base`` helper + merge-base fallback.

Sibling of :func:`~eawf.workflow.lifecycle.wave_sha.derive_wave_sha` added
in W15 so the audit_dsl runner has a one-call entry point that:

1. Returns ``derive_wave_sha(wave_id) + "~1"`` when the wave's SHA is
   discoverable on the current branch.
2. Falls back to ``git merge-base HEAD main`` so a fresh-clone / CI
   context still scopes the diff to "commits unique to this branch".
3. Falls back further to a literal ref string (default ``"origin/main"``)
   when even the merge-base lookup fails — keeps the call site fail-open
   in line with :data:`~eawf.platform.lint._conditional.DEFAULT_DIFF_BASE`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from eawf.workflow.lifecycle.wave_sha import (
    _git_merge_base_head_main,
    commit_wave_trailer,
    derive_diff_base,
    derive_wave_sha,
)


def test_commit_wave_trailer_emits_eawf_wave() -> None:
    assert commit_wave_trailer("P28-I03-W02") == "Eawf-Wave: P28-I03-W02"
    assert commit_wave_trailer("bad") is None


def test_derive_wave_sha_searches_eawf_wave_trailer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    class _FakeCompleted:
        returncode = 0
        stderr = ""

        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        calls.append(cmd)
        if "--grep=Eawf-Wave: P28-I03-W02" in cmd:
            return _FakeCompleted("abc123\n")
        return _FakeCompleted("")

    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.subprocess.run", _fake_run)

    assert derive_wave_sha("P28-I03-W02", repo_root=tmp_path) == "abc123"
    assert [cmd[3] for cmd in calls] == [
        "--grep=[P28-I03-W02]",
        "--grep=[P28-W02]",
        "--grep=Eawf-Wave: P28-I03-W02",
    ]


# ---- derive_diff_base ------------------------------------------------------


def test_derive_diff_base_uses_wave_sha_when_resolvable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.derive_wave_sha",
        lambda wid, repo_root=None: "abc12345" if wid == "P28-I01-W15" else None,
    )
    assert derive_diff_base("P28-I01-W15", repo_root=tmp_path) == "abc12345~1"


def test_derive_diff_base_falls_back_when_sha_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.derive_wave_sha", lambda wid, repo_root=None: None
    )
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha._git_merge_base_head_main",
        lambda *, repo_root=None, fallback="origin/main": "MB-SHA",
    )
    assert derive_diff_base("P99-I99-W99", repo_root=tmp_path) == "MB-SHA"


def test_derive_diff_base_without_wave_id_skips_derive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When wave_id is None the helper goes straight to merge-base."""
    called: dict[str, Any] = {"derive": 0}

    def _fail_derive(wid: str, *, repo_root: Any = None) -> str | None:
        called["derive"] += 1
        return "nope"

    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.derive_wave_sha", _fail_derive)
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha._git_merge_base_head_main",
        lambda *, repo_root=None, fallback="origin/main": "MB-ONLY",
    )
    assert derive_diff_base(None, repo_root=tmp_path) == "MB-ONLY"
    assert called["derive"] == 0


# ---- _git_merge_base_head_main --------------------------------------------


def test_merge_base_returns_fallback_when_git_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: None)
    assert _git_merge_base_head_main(repo_root=tmp_path, fallback="origin/main") == "origin/main"


def test_merge_base_returns_fallback_on_non_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """git merge-base returning non-zero (e.g., no 'main' ref) falls back."""

    class _FakeCompleted:
        returncode = 128
        stdout = ""
        stderr = "fatal: Not a valid object name main\n"

    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.subprocess.run",
        lambda *a, **k: _FakeCompleted(),
    )
    assert _git_merge_base_head_main(repo_root=tmp_path, fallback="origin/main") == "origin/main"


def test_merge_base_returns_sha_on_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class _FakeCompleted:
        returncode = 0
        stdout = "abcdef0123456789\n"
        stderr = ""

    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.subprocess.run",
        lambda *a, **k: _FakeCompleted(),
    )
    assert _git_merge_base_head_main(repo_root=tmp_path) == "abcdef0123456789"


def test_merge_base_returns_fallback_on_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _raise_timeout(*a: Any, **k: Any) -> None:
        raise subprocess.TimeoutExpired(cmd="git", timeout=1.0)

    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.subprocess.run", _raise_timeout)
    assert _git_merge_base_head_main(repo_root=tmp_path, fallback="origin/main") == "origin/main"


def test_merge_base_returns_fallback_on_empty_stdout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _FakeCompleted:
        returncode = 0
        stdout = "   \n"
        stderr = ""

    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.subprocess.run",
        lambda *a, **k: _FakeCompleted(),
    )
    assert _git_merge_base_head_main(repo_root=tmp_path, fallback="origin/main") == "origin/main"
