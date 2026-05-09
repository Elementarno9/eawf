"""Unit tests for :func:`eawf.worktree.merge_back.merge_back`.

The tests build a tmp git repo with a parent feature branch and a
worktree branch carrying one or more commits. They exercise both
strategies (cherry-pick / rebase-then-ff), the conflict-preservation
flow, and the --continue / --abort surfaces.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from eawf.cli import errors as cli_errors
from eawf.state.enums import WorktreeStatus
from eawf.worktree.create import create_worktree
from eawf.worktree.merge_back import (
    STRATEGY_REBASE_THEN_FF,
    merge_back,
)
from tests.unit.test_worktree_create import _claimed_state, _make_repo

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git is required for worktree merge-back tests",
)


def _commit_in(worktree: Path, *, name: str, content: str, msg: str) -> str:
    """Make one commit in *worktree* and return its short sha."""
    target = worktree / name
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "."], check=True)
    subprocess.run(["git", "-C", str(worktree), "commit", "-q", "-m", msg], check=True)
    sha = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return sha


def test_cherry_pick_clean_marks_merged(tmp_path: Path) -> None:
    """Single-commit cherry-pick succeeds and updates record.status to MERGED."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    sha = _commit_in(Path(record.path), name="hello.txt", content="x\n", msg="add hello")

    result = merge_back(state, repo_root=repo, wave_id="P05-I01-W01")
    assert not result.conflicted
    assert result.merged_commit is not None
    assert state.worktrees is not None
    assert state.worktrees[record.id].status == WorktreeStatus.MERGED
    assert sha in result.picked_commits or len(result.picked_commits) >= 1


def test_cherry_pick_conflict_marks_conflicted(tmp_path: Path) -> None:
    """A synthetic same-file divergence -> CONFLICTED record + envelope files."""
    repo = _make_repo(tmp_path / "repo")
    # Add `conflict.txt` on the parent first so the worktree can diverge.
    (repo / "conflict.txt").write_text("parent\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "parent edit"], check=True)

    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    # Worktree edits the same file with different content.
    _commit_in(
        Path(record.path),
        name="conflict.txt",
        content="worktree\n",
        msg="worktree edit",
    )
    # Now move the parent forward so cherry-pick will see the divergence.
    (repo / "conflict.txt").write_text("parent v2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "parent v2"], check=True)

    result = merge_back(state, repo_root=repo, wave_id="P05-I01-W01")
    assert result.conflicted
    assert result.conflict_files
    assert any("conflict.txt" in f for f in result.conflict_files)
    assert state.worktrees is not None
    assert state.worktrees[record.id].status == WorktreeStatus.CONFLICTED


def test_continue_after_resolution_completes(tmp_path: Path) -> None:
    """After staging a resolution, ``--continue`` finishes the cherry-pick."""
    repo = _make_repo(tmp_path / "repo")
    (repo / "conflict.txt").write_text("parent\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "parent edit"], check=True)

    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    _commit_in(
        Path(record.path),
        name="conflict.txt",
        content="worktree\n",
        msg="worktree edit",
    )
    (repo / "conflict.txt").write_text("parent v2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "parent v2"], check=True)

    # Provoke a conflict.
    res1 = merge_back(state, repo_root=repo, wave_id="P05-I01-W01")
    assert res1.conflicted

    # Resolve in the parent worktree by accepting the worktree version.
    (repo / "conflict.txt").write_text("worktree\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "conflict.txt"], check=True)

    # Continue.
    res2 = merge_back(state, repo_root=repo, wave_id="P05-I01-W01", continue_=True)
    assert not res2.conflicted
    assert state.worktrees is not None
    assert state.worktrees[record.id].status == WorktreeStatus.MERGED


def test_abort_marks_abandoned(tmp_path: Path) -> None:
    """``--abort`` resets the in-progress merge and marks the record ABANDONED."""
    repo = _make_repo(tmp_path / "repo")
    (repo / "conflict.txt").write_text("parent\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "parent edit"], check=True)

    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    _commit_in(
        Path(record.path),
        name="conflict.txt",
        content="worktree\n",
        msg="worktree edit",
    )
    (repo / "conflict.txt").write_text("parent v2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "parent v2"], check=True)

    merge_back(state, repo_root=repo, wave_id="P05-I01-W01")

    res = merge_back(state, repo_root=repo, wave_id="P05-I01-W01", abort=True)
    assert not res.conflicted
    assert state.worktrees is not None
    assert state.worktrees[record.id].status == WorktreeStatus.ABANDONED
    assert state.worktrees[record.id].merged_commit is None


def test_rebase_then_ff_clean(tmp_path: Path) -> None:
    """Rebase strategy happy path: worktree commit lands on parent via ff."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    _commit_in(Path(record.path), name="hello.txt", content="x\n", msg="add hello")

    result = merge_back(
        state,
        repo_root=repo,
        wave_id="P05-I01-W01",
        strategy=STRATEGY_REBASE_THEN_FF,
    )
    assert not result.conflicted
    assert result.merged_commit is not None
    assert state.worktrees is not None
    assert state.worktrees[record.id].status == WorktreeStatus.MERGED


def test_invalid_strategy_raises_invalid_input(tmp_path: Path) -> None:
    """Unknown strategy -> :class:`InvalidInput` (exit 3)."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    with pytest.raises(cli_errors.InvalidInput) as exc_info:
        merge_back(
            state,
            repo_root=repo,
            wave_id="P05-I01-W01",
            strategy="bogus_strategy",
        )
    assert "unknown merge-back strategy" in str(exc_info.value)


def test_continue_and_abort_mutually_exclusive(tmp_path: Path) -> None:
    """Passing both ``continue_`` and ``abort`` -> :class:`InvalidInput`."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    with pytest.raises(cli_errors.InvalidInput):
        merge_back(
            state,
            repo_root=repo,
            wave_id="P05-I01-W01",
            continue_=True,
            abort=True,
        )
