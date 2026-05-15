"""Focused unit tests for the live git-pane helpers (P20-I03-W01).

The pane composer is tested at the integration level in
:mod:`tests.unit.test_tui_layout`; this file pins the low-level shell-
out helpers (``_git_run``, ``_gather_git_fields``, ``_git_pane_fields``
cache, ``_resolve_repo_root``) so future refactors of the subprocess
layer keep the contract.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from eawf.tui import layout as layout_mod
from eawf.tui.layout import (
    GIT_PANE_CACHE_TTL,
    _gather_git_fields,
    _git_run,
    _reset_git_pane_cache,
    _resolve_repo_root,
)


def _fake_completed(returncode: int, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=""
    )


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    _reset_git_pane_cache()


# ---------------------------------------------------------------------------
# _git_run
# ---------------------------------------------------------------------------


def test_git_run_returns_stripped_stdout_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_git_run`` strips trailing whitespace from stdout."""

    def fake_run(*_: Any, **__: Any) -> subprocess.CompletedProcess[str]:
        return _fake_completed(0, "main\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _git_run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=Path("/tmp")) == "main"


def test_git_run_returns_none_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-zero exit (e.g. not a git repo) returns ``None``."""

    def fake_run(*_: Any, **__: Any) -> subprocess.CompletedProcess[str]:
        return _fake_completed(128, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _git_run(["status"], cwd=Path("/tmp")) is None


def test_git_run_handles_file_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """No git binary on PATH → ``None`` (never raised)."""

    def fake_run(*_: Any, **__: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("git: not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _git_run(["status"], cwd=Path("/tmp")) is None


def test_git_run_handles_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hung git invocation times out cleanly."""

    def fake_run(*_: Any, **__: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="git", timeout=1.0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _git_run(["status"], cwd=Path("/tmp")) is None


def test_git_run_passes_cwd_to_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """The workspace root is forwarded to :func:`subprocess.run`."""
    captured: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured["cwd"] = kwargs.get("cwd")
        captured["timeout"] = kwargs.get("timeout")
        return _fake_completed(0, "x")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _git_run(["rev-parse"], cwd=Path("/workspace/eawf"))
    assert captured["cwd"] == "/workspace/eawf"
    # The timeout is set short enough that a stuck command can't freeze
    # the render loop.
    assert captured["timeout"] is not None
    assert captured["timeout"] <= 5.0
    assert captured["args"][0] == "git"


# ---------------------------------------------------------------------------
# _gather_git_fields
# ---------------------------------------------------------------------------


def test_gather_git_fields_clean_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty porcelain → ``clean``."""

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if "--abbrev-ref" in args:
            return _fake_completed(0, "main")
        if "--short" in args:
            return _fake_completed(0, "abc1234")
        if "status" in args:
            return _fake_completed(0, "")
        if "rev-list" in args:
            return _fake_completed(0, "0")
        return _fake_completed(1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    fields = _gather_git_fields(Path("/tmp"))
    assert fields == {
        "branch": "main",
        "head": "abc1234",
        "status": "clean",
        "upstream": "up-to-date",
    }


def test_gather_git_fields_dirty_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiple porcelain lines → ``<N> modified``."""

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if "--abbrev-ref" in args:
            return _fake_completed(0, "feature/x")
        if "--short" in args:
            return _fake_completed(0, "abc1234")
        if "status" in args:
            return _fake_completed(0, " M file1.py\n?? file2.py\n")
        return _fake_completed(0, "0")

    monkeypatch.setattr(subprocess, "run", fake_run)
    fields = _gather_git_fields(Path("/tmp"))
    assert fields["status"] == "2 modified"


def test_gather_git_fields_ahead_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-zero ahead/behind counts render with ``+N / -N`` suffix."""

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if "--abbrev-ref" in args:
            return _fake_completed(0, "main")
        if "--short" in args:
            return _fake_completed(0, "abc1234")
        if "status" in args:
            return _fake_completed(0, "")
        # rev-list: HEAD ahead by 3 of upstream
        if "@{u}..HEAD" in args:
            return _fake_completed(0, "3")
        if "HEAD..@{u}" in args:
            return _fake_completed(0, "1")
        return _fake_completed(1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    fields = _gather_git_fields(Path("/tmp"))
    assert fields["upstream"] == "+3 / -1"


def test_gather_git_fields_no_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """rev-list against ``@{u}`` returning non-zero → upstream is a dash."""

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if "--abbrev-ref" in args:
            return _fake_completed(0, "feature/foo")
        if "--short" in args:
            return _fake_completed(0, "abc1234")
        if "status" in args:
            return _fake_completed(0, "")
        return _fake_completed(128, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    fields = _gather_git_fields(Path("/tmp"))
    assert fields["upstream"] == "-"


def test_gather_git_fields_non_git_cwd_all_dashes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every command non-zero → every field is a dash."""

    def fake_run(*_: Any, **__: Any) -> subprocess.CompletedProcess[str]:
        return _fake_completed(128)

    monkeypatch.setattr(subprocess, "run", fake_run)
    fields = _gather_git_fields(Path("/tmp"))
    assert fields == {"branch": "-", "head": "-", "status": "-", "upstream": "-"}


# ---------------------------------------------------------------------------
# _git_pane_fields cache
# ---------------------------------------------------------------------------


def test_git_pane_fields_cache_hit_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two calls within :data:`GIT_PANE_CACHE_TTL` share one shell-out."""
    counter = {"calls": 0}

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        counter["calls"] += 1
        if "--abbrev-ref" in args:
            return _fake_completed(0, "main")
        if "--short" in args:
            return _fake_completed(0, "abc1234")
        if "status" in args:
            return _fake_completed(0, "")
        return _fake_completed(0, "0")

    monkeypatch.setattr(subprocess, "run", fake_run)
    workspace = Path("/tmp/cache-A")
    layout_mod._git_pane_fields(workspace, now=0.0)
    first = counter["calls"]
    layout_mod._git_pane_fields(workspace, now=GIT_PANE_CACHE_TTL / 4)
    assert counter["calls"] == first


def test_git_pane_fields_cache_miss_past_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the TTL, the cache misses and re-runs git."""
    counter = {"calls": 0}

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        counter["calls"] += 1
        if "--abbrev-ref" in args:
            return _fake_completed(0, "main")
        if "--short" in args:
            return _fake_completed(0, "abc1234")
        if "status" in args:
            return _fake_completed(0, "")
        return _fake_completed(0, "0")

    monkeypatch.setattr(subprocess, "run", fake_run)
    workspace = Path("/tmp/cache-B")
    layout_mod._git_pane_fields(workspace, now=0.0)
    first = counter["calls"]
    # Jump well past the TTL.
    layout_mod._git_pane_fields(workspace, now=GIT_PANE_CACHE_TTL * 4 + 1.0)
    assert counter["calls"] > first


def test_git_pane_fields_cache_keyed_per_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different workspaces cache independently."""
    counter = {"calls": 0}

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        counter["calls"] += 1
        if "--abbrev-ref" in args:
            return _fake_completed(0, "main")
        if "--short" in args:
            return _fake_completed(0, "abc1234")
        if "status" in args:
            return _fake_completed(0, "")
        return _fake_completed(0, "0")

    monkeypatch.setattr(subprocess, "run", fake_run)
    layout_mod._git_pane_fields(Path("/tmp/cache-C"), now=0.0)
    first = counter["calls"]
    layout_mod._git_pane_fields(Path("/tmp/cache-D"), now=0.0)
    assert counter["calls"] > first


# ---------------------------------------------------------------------------
# _resolve_repo_root
# ---------------------------------------------------------------------------


def test_resolve_repo_root_prefers_workspace_arg() -> None:
    """When given a workspace, return it verbatim."""
    assert _resolve_repo_root(Path("/explicit/path")) == Path("/explicit/path")


def test_resolve_repo_root_walks_for_ea_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falls back to walking upward for a ``.ea`` directory."""
    (tmp_path / ".ea").mkdir()
    nested = tmp_path / "src" / "deeper"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert _resolve_repo_root(None) == tmp_path


def test_resolve_repo_root_falls_back_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``.ea`` anywhere → return the current cwd."""
    nested = tmp_path / "no-workspace"
    nested.mkdir()
    monkeypatch.chdir(nested)
    # No .ea on the way up — function returns cwd.
    resolved = _resolve_repo_root(None)
    assert resolved == nested
