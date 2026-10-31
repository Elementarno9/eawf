"""Coverage-lift tests for :mod:`eawf.runtime.worktree.git`.

Two complementary strategies:

* **Monkeypatched ``subprocess.run``** drives the per-helper error-mapping
  branches (well-known stderr markers -> canonical CliError subclasses)
  without spinning up a real repo. This is the only practical way to hit
  branches like "timeout -> IntegrityViolation" deterministically.
* **The real :func:`dirty_repo` fixture** (a temp git repo left dirty)
  exercises the error-class paths against actual ``git`` so the mapping
  is validated end-to-end, not just against synthetic stderr.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.worktree import git
from eawf.surfaces.cli import errors as cli_errors
from tests.fixtures.conftest import make_dirty_repo

_FAKE_GIT = "/usr/bin/git"


@pytest.fixture
def dirty_repo(tmp_path: Path) -> Path:
    """A real, dirty git repo (delegates to the shared fixtures builder).

    ``tests/fixtures/conftest.py`` is not on this module's conftest
    ancestry, so we re-expose the shared builder as a local fixture.
    """
    return make_dirty_repo(tmp_path)


def _patch_git_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("eawf.runtime.worktree.git.shutil.which", lambda _name: _FAKE_GIT)


def _patch_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> None:
    """Patch ``subprocess.run`` to return a canned CompletedProcess."""
    _patch_git_present(monkeypatch)

    def _fake_run(*_a: Any, **_k: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
        )

    monkeypatch.setattr("eawf.runtime.worktree.git.subprocess.run", _fake_run)


# --- _ensure_git / _run --------------------------------------------------


def test_ensure_git_missing_raises_instrument_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("eawf.runtime.worktree.git.shutil.which", lambda _name: None)
    with pytest.raises(cli_errors.UserError, match="git executable not found"):
        git._ensure_git()


def test_run_timeout_maps_to_integrity_violation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_git_present(monkeypatch)

    def _raise_timeout(*_a: Any, **_k: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["git", "status"], timeout=1.0)

    monkeypatch.setattr("eawf.runtime.worktree.git.subprocess.run", _raise_timeout)
    with pytest.raises(cli_errors.StateConflict, match="timed out"):
        git._run(["git", "-C", str(tmp_path), "status"])


# --- repo_root -----------------------------------------------------------


def test_repo_root_outside_repo_raises_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=128, stderr="fatal: not a git repository")
    with pytest.raises(cli_errors.UserError, match="not a git repository"):
        git.repo_root(tmp_path)


def test_repo_root_clean_returns_toplevel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_run(monkeypatch, returncode=0, stdout=f"{tmp_path}\n")
    assert git.repo_root(tmp_path) == tmp_path


def test_repo_root_real_dirty_repo_returns_root(dirty_repo: Path) -> None:
    assert git.repo_root(dirty_repo) == dirty_repo.resolve()


# --- worktree_add error mapping -----------------------------------------


def test_worktree_add_already_in_use_maps_to_lock_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=128, stderr="fatal: 'x' is already used by worktree")
    with pytest.raises(cli_errors.StateConflict, match="already"):
        git.worktree_add(tmp_path, branch="b", path=tmp_path / "wt", base="main")


def test_worktree_add_invalid_reference_maps_to_invalid_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=128, stderr="fatal: invalid reference: nope")
    with pytest.raises(cli_errors.UserError, match="invalid reference"):
        git.worktree_add(tmp_path, branch="b", path=tmp_path / "wt", base="nope")


def test_worktree_add_not_a_repo_maps_to_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=128, stderr="fatal: not a git repository")
    with pytest.raises(cli_errors.UserError, match="not a git repository"):
        git.worktree_add(tmp_path, branch="b", path=tmp_path / "wt", base="main")


def test_worktree_add_unknown_failure_maps_to_integrity_violation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=1, stderr="some other git boom")
    with pytest.raises(cli_errors.StateConflict, match="worktree add failed"):
        git.worktree_add(tmp_path, branch="b", path=tmp_path / "wt", base="main")


def test_worktree_add_success_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_run(monkeypatch, returncode=0)
    assert git.worktree_add(tmp_path, branch="b", path=tmp_path / "wt", base="main") is None


def test_worktree_add_real_invalid_base_raises_invalid_input(dirty_repo: Path) -> None:
    """Real git: adding a worktree off a non-existent base raises InvalidInput."""
    with pytest.raises(cli_errors.UserError):
        git.worktree_add(
            dirty_repo,
            branch="feature/new",
            path=dirty_repo.parent / "wt-new",
            base="does-not-exist-ref",
        )


# --- worktree_remove -----------------------------------------------------


def test_worktree_remove_failure_maps_to_integrity_violation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=1, stderr="fatal: cannot remove")
    with pytest.raises(cli_errors.StateConflict, match="worktree remove failed"):
        git.worktree_remove(tmp_path, path=tmp_path / "wt")


def test_worktree_remove_success_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=0)
    assert git.worktree_remove(tmp_path, path=tmp_path / "wt", force=True) is None


def test_worktree_remove_real_missing_path_raises(dirty_repo: Path) -> None:
    with pytest.raises(cli_errors.StateConflict):
        git.worktree_remove(dirty_repo, path=dirty_repo / "no-such-worktree")


# --- worktree_list -------------------------------------------------------


# --- status_porcelain ----------------------------------------------------


def test_status_porcelain_failure_maps_to_integrity_violation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=1, stderr="fatal: not a git repository")
    with pytest.raises(cli_errors.StateConflict, match="status failed"):
        git.status_porcelain(tmp_path)


def test_status_porcelain_real_dirty_repo_reports_entries(dirty_repo: Path) -> None:
    rows = git.status_porcelain(dirty_repo)
    assert any("tracked.txt" in r for r in rows)
    assert any("untracked.txt" in r for r in rows)


# --- rev_list ------------------------------------------------------------


# --- cherry_pick ---------------------------------------------------------


# --- cherry_pick_continue ------------------------------------------------


# --- cherry_pick_abort ---------------------------------------------------


# --- rebase --------------------------------------------------------------


def test_rebase_clean_returns_true(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_run(monkeypatch, returncode=0)
    assert git.rebase(tmp_path, target="main") == (True, "")


def test_rebase_conflict_returns_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_run(monkeypatch, returncode=1, stderr="CONFLICT (content): Merge conflict in x")
    clean, detail = git.rebase(tmp_path, target="main")
    assert clean is False
    assert "conflict" in detail.lower()


def test_rebase_invalid_upstream_maps_to_invalid_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=128, stderr="fatal: invalid upstream 'nope'")
    with pytest.raises(cli_errors.UserError, match="rebase"):
        git.rebase(tmp_path, target="nope")


def test_rebase_unknown_failure_maps_to_integrity_violation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=1, stderr="weird rebase boom")
    with pytest.raises(cli_errors.StateConflict, match="rebase failed"):
        git.rebase(tmp_path, target="main")


# --- rebase_continue / rebase_abort -------------------------------------


# --- merge_ff_only -------------------------------------------------------


# --- head_sha ------------------------------------------------------------


def test_head_sha_failure_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_run(monkeypatch, returncode=128, stderr="fatal: ambiguous argument 'HEAD'")
    with pytest.raises(cli_errors.StateConflict, match="rev-parse HEAD failed"):
        git.head_sha(tmp_path)


def test_head_sha_real_dirty_repo_returns_short_sha(dirty_repo: Path) -> None:
    sha = git.head_sha(dirty_repo)
    assert sha
    assert len(sha) >= 7


# --- branch_delete -------------------------------------------------------


def test_branch_delete_failure_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=1, stderr="error: branch 'x' not found")
    assert git.branch_delete(tmp_path, name="x") is False


def test_branch_delete_success_returns_true(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run(monkeypatch, returncode=0)
    assert git.branch_delete(tmp_path, name="feature/foo") is True


# --- cherry_pick_in_progress / rebase_in_progress -----------------------
