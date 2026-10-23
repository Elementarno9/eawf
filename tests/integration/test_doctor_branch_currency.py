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
    """Force ``check_branch_currency`` to read *default_branch* off the project record.

    The check reads only the brief's ``default_branch`` from the generation's
    project record, so a namespace is a faithful stand-in; ``None`` stands
    for a generation whose project names no branch.
    """
    from types import SimpleNamespace

    brief = SimpleNamespace(default_branch=default_branch)
    monkeypatch.setattr(checks, "_tree_for_check", lambda workspace, *, name: workspace / ".ea")
    monkeypatch.setattr("eawf.platform.rules.render.load_project_brief", lambda _root: brief)


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
    """A generation whose project names no branch has no base branch to compare."""
    _stub_project_branch(monkeypatch, None)
    result = checks.check_branch_currency(workspace=tmp_path)
    assert result.status == "ok"
    assert "no project default branch" in (result.detail or "")


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


def test_check_branch_currency_reads_the_generation_project_row(tmp_path: Path) -> None:
    """A migrated tree's imported project row names the base branch the check compares."""
    from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
    from eawf.kernel.state.epoch2.authority import resolve_authority
    from eawf.kernel.store.compaction import read_document, write_document
    from tests._epoch2_helpers import lay_epoch2_tree

    clone = _repo_with_origin(tmp_path)
    state_path = lay_epoch2_tree(clone)
    authority = resolve_authority(state_path.parent)
    assert authority.target is not None and authority.generation_id is not None
    document = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    body = read_document(document)
    project = {
        "code": "QR",
        "slug": "qr",
        "title": "QR",
        "description": None,
        "domains": ["quant"],
        "default_branch": "trunk",
        "status": "active",
        "repo_urn": "urn:eawf:v1:repo:QR",
    }
    body["project"] = {"QR": {"payload": {"payload": project}, "status": "imported"}}
    write_document(document, body)
    result = checks.check_branch_currency(workspace=clone)
    assert (result.status, result.detail) == ("ok", "no origin/trunk to compare")
