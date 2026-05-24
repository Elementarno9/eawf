"""Unit tests for :func:`eawf.runtime.worktree.cleanup.cleanup_worktree`.

The tests stand up a real git repo + worktree, then exercise the
refusal contract (dirty / CONFLICTED) and the success-path
transitions.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from eawf.kernel.state.enums import WorktreeStatus
from eawf.runtime.worktree.cleanup import cleanup_worktree
from eawf.runtime.worktree.create import create_worktree
from eawf.runtime.worktree.git import worktree_list
from eawf.surfaces.cli import errors as cli_errors
from tests.unit.test_worktree_create import _claimed_state, _make_repo

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git is required for worktree cleanup tests",
)


def test_cleanup_refuses_dirty(tmp_path: Path) -> None:
    """Uncommitted file in the worktree -> :class:`IntegrityViolation`."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    # Leave the worktree dirty (untracked file).
    ((repo / record.path) / "scratch.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(cli_errors.StateConflict) as exc_info:
        cleanup_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    assert "dirty" in str(exc_info.value)


def test_cleanup_force_removes_dirty(tmp_path: Path) -> None:
    """``--force`` succeeds even when the worktree is dirty."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    ((repo / record.path) / "scratch.txt").write_text("dirty\n", encoding="utf-8")
    result = cleanup_worktree(state, repo_root=repo, wave_id="P05-I01-W01", force=True)
    assert not Path(result.removed_path).exists()
    assert state.worktrees is not None
    assert state.worktrees[record.id].status == WorktreeStatus.ABANDONED


def test_cleanup_prunes_git_registry(tmp_path: Path) -> None:
    """After cleanup, ``git worktree list --porcelain`` no longer shows the path."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    abs_path = str((repo / record.path).resolve())
    pre = worktree_list(repo)
    assert any(abs_path == entry.get("worktree", "") for entry in pre)

    cleanup_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    post = worktree_list(repo)
    assert not any(abs_path == entry.get("worktree", "") for entry in post)


def test_cleanup_keep_branch_preserves_branch(tmp_path: Path) -> None:
    """``keep_branch=True`` skips ``git branch -D``."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    cleanup_worktree(
        state,
        repo_root=repo,
        wave_id="P05-I01-W01",
        keep_branch=True,
    )
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", record.branch],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert record.branch in branches


def test_cleanup_active_transitions_to_abandoned(tmp_path: Path) -> None:
    """An ACTIVE+forced cleanup transitions the record to ABANDONED."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    assert state.worktrees is not None
    assert state.worktrees[record.id].status == WorktreeStatus.ACTIVE
    cleanup_worktree(state, repo_root=repo, wave_id="P05-I01-W01", force=True)
    assert state.worktrees[record.id].status == WorktreeStatus.ABANDONED


def test_cleanup_refuses_conflicted_without_force(tmp_path: Path) -> None:
    """A CONFLICTED record refuses cleanup unless ``--force`` is passed."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    # Manually transition to CONFLICTED to model post-merge-back state.
    assert state.worktrees is not None
    state.worktrees[record.id].status = WorktreeStatus.CONFLICTED
    with pytest.raises(cli_errors.StateConflict) as exc_info:
        cleanup_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    assert "preserve evidence" in str(exc_info.value)
