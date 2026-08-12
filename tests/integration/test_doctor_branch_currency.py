"""Integration tests for the ``branch_currency`` doctor check.

Integration tier because the check shells out to ``git``: the assertions are
about real ahead/behind arithmetic against a real clone, not a stub.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from eawf.observability.doctor import checks


def _stub_project_branch(monkeypatch: pytest.MonkeyPatch, default_branch: str | None) -> None:
    """Force ``check_branch_currency`` to read *default_branch* off the project.

    The check reads only ``state.project.default_branch``, so a namespace is a
    faithful stand-in; ``None`` stands for a state carrying no project record.
    """
    from types import SimpleNamespace

    project = None if default_branch is None else SimpleNamespace(default_branch=default_branch)
    stub_state = SimpleNamespace(project=project)

    def fake_load(workspace: Path, *, name: str) -> tuple[object, Path]:
        return stub_state, workspace / ".ea" / "state.json"

    monkeypatch.setattr("eawf.observability.doctor.checks._load_state_for_check", fake_load)


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _repo_with_origin(tmp_path: Path) -> Path:
    """Build a clone whose local ``main`` tracks an ``origin/main`` one ahead."""
    import subprocess

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "config", "user.email", "test@example.com")
    _git(upstream, "config", "user.name", "Test")
    _git(upstream, "commit", "--allow-empty", "-q", "-m", "base")

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(upstream), str(clone)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")
    return clone


def test_check_branch_currency_no_workspace_ok() -> None:
    """A missing workspace anchor is informational, not a failure."""
    result = checks.check_branch_currency(workspace=None)
    assert result.status == "ok"
    assert result.name == "branch_currency"


def test_check_branch_currency_no_project_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state without a project record has no base branch to compare."""
    _stub_project_branch(monkeypatch, None)
    result = checks.check_branch_currency(workspace=tmp_path)
    assert result.status == "ok"
    assert "no project record" in (result.detail or "")


def test_check_branch_currency_without_remote_ref_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo with no ``origin/<base>`` cannot be stale against it."""
    _stub_project_branch(monkeypatch, "main")
    repo = tmp_path / "solo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    result = checks.check_branch_currency(workspace=repo)
    assert result.status == "ok"
    assert "no origin/main to compare" in (result.detail or "")


def test_check_branch_currency_current_is_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A freshly cloned base branch matches its remote (boundary: behind == 0)."""
    _stub_project_branch(monkeypatch, "main")
    repo = _repo_with_origin(tmp_path)
    result = checks.check_branch_currency(workspace=repo)
    assert result.status == "ok"
    assert "current with origin/main" in (result.detail or "")


def test_check_branch_currency_behind_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local base left behind its remote warns and names the gap.

    This is the real defect: a stale local base reads as current, so every
    comparison drawn against it inherits the staleness silently.
    """
    _stub_project_branch(monkeypatch, "main")
    repo = _repo_with_origin(tmp_path)
    _git(tmp_path / "upstream", "commit", "--allow-empty", "-q", "-m", "moved on")
    _git(repo, "fetch", "-q", "origin")

    result = checks.check_branch_currency(workspace=repo)
    assert result.status == "warn"
    assert "local main is 1 commit(s) behind origin/main" in (result.detail or "")
    assert "git fetch" in (result.detail or "")


def test_check_branch_currency_stale_fetch_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old FETCH_HEAD warns even when the base is level with its remote.

    Without a recent fetch, "level" only means the last snapshot agreed.
    """
    import os

    _stub_project_branch(monkeypatch, "main")
    repo = _repo_with_origin(tmp_path)
    fetch_head = repo / ".git" / "FETCH_HEAD"
    fetch_head.touch()
    stale = time.time() - (checks._FETCH_STALE_HOURS + 1) * 3600
    os.utime(fetch_head, (stale, stale))

    result = checks.check_branch_currency(workspace=repo)
    assert result.status == "warn"
    assert "last fetch" in (result.detail or "")
