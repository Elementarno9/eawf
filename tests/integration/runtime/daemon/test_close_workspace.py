"""Exact-revision close workspace tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from eawf.runtime.daemon.close_workspace import (
    CloseWorkspaceError,
    cleanup_close_workspace,
    prepare_close_workspace,
    resolve_exact_revision,
    workspace_path,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "payload.txt").write_text("frozen\n", encoding="utf-8")
    _git(repo, "add", "payload.txt")
    _git(repo, "commit", "-m", "test: frozen revision")
    return repo


def test_prepare_close_workspace_is_exact_clean_and_idempotent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    commit_sha, tree_sha = resolve_exact_revision(repo, "HEAD")

    first = prepare_close_workspace(
        repo,
        attempt_id="close-abc123",
        commit_ref=commit_sha,
        expected_tree_sha=tree_sha,
    )
    second = prepare_close_workspace(
        repo,
        attempt_id="close-abc123",
        commit_ref=commit_sha,
        expected_tree_sha=tree_sha,
    )

    assert first.created is True
    assert second.created is False
    assert second.commit_sha == commit_sha
    assert second.tree_sha == tree_sha
    assert _git(second.path, "status", "--porcelain=v1") == ""
    assert cleanup_close_workspace(repo, attempt_id="close-abc123") is True
    assert cleanup_close_workspace(repo, attempt_id="close-abc123") is False


def test_prepare_close_workspace_rejects_expected_tree_mismatch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    commit_sha, _tree_sha = resolve_exact_revision(repo, "HEAD")

    with pytest.raises(CloseWorkspaceError, match="close tree mismatch"):
        prepare_close_workspace(
            repo,
            attempt_id="close-tree-mismatch",
            commit_ref=commit_sha,
            expected_tree_sha="0" * 40,
        )


def test_prepare_close_workspace_rejects_dirty_reentry(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    commit_sha, tree_sha = resolve_exact_revision(repo, "HEAD")
    prepared = prepare_close_workspace(
        repo,
        attempt_id="close-dirty",
        commit_ref=commit_sha,
        expected_tree_sha=tree_sha,
    )
    (prepared.path / "untracked.txt").write_text("drift\n", encoding="utf-8")

    with pytest.raises(CloseWorkspaceError, match="close worktree is dirty"):
        prepare_close_workspace(
            repo,
            attempt_id="close-dirty",
            commit_ref=commit_sha,
            expected_tree_sha=tree_sha,
        )
    cleanup_close_workspace(repo, attempt_id="close-dirty")


def test_workspace_path_rejects_path_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid close attempt id"):
        workspace_path(tmp_path, "../escape")
