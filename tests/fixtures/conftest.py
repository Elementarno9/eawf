"""Shared fixtures for git-backed worktree tests.

The :func:`dirty_repo` fixture materialises a real, throwaway git
repository left in a dirty/uncommitted state so error-class paths in
:mod:`eawf.worktree.git` (and the worktree subsystem at large) can be
exercised against actual ``git`` invocations rather than mocked
:func:`subprocess.run` shims.

The repo-building logic lives in :func:`make_dirty_repo` so test modules
outside this directory (which pytest does not auto-wire into) can build
their own local ``dirty_repo`` fixture by delegating to it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_GIT_MISSING = shutil.which("git") is None


def _git(*args: str, cwd: Path) -> None:
    """Run ``git *args`` in *cwd*, raising on non-zero exit."""
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def make_dirty_repo(tmp_path: Path) -> Path:
    """Build a freshly-initialised git repo left in a dirty state.

    The repo has one committed file on ``main`` plus a checked-out
    ``feature/eawf-v0.1`` branch, then a tracked file is modified and an
    untracked file is created so ``git status --porcelain`` reports two
    dirty entries. Callers drive worktree error-class paths against it
    (e.g. ``worktree remove`` refusing a dirty tree, cherry-pick
    conflicts).

    Args:
        tmp_path: A pytest temp directory the repo is created under.

    Returns:
        The path to the dirty repository root.

    Raises:
        pytest.skip.Exception: When ``git`` is not on PATH.
    """
    if _GIT_MISSING:
        pytest.skip("git is required for the dirty_repo fixture")
    repo = tmp_path / "dirty"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "ci@example.com", cwd=repo)
    _git("config", "user.name", "ci", cwd=repo)
    (repo / "tracked.txt").write_text("original\n", encoding="utf-8")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "initial", cwd=repo)
    _git("checkout", "-q", "-b", "feature/eawf-v0.1", cwd=repo)
    # Leave the tree dirty: modify a tracked file + add an untracked one.
    (repo / "tracked.txt").write_text("modified\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("new\n", encoding="utf-8")
    return repo


@pytest.fixture
def dirty_repo(tmp_path: Path) -> Path:
    """Return a freshly-initialised git repo left in a dirty state.

    See :func:`make_dirty_repo` for the repository shape.
    """
    return make_dirty_repo(tmp_path)
