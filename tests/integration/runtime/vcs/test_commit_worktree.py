"""A worker's kept edits become one eawf-authored commit on the worktree's own branch."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Final

import pytest

from eawf.runtime.worktree.tree_change import commit_worktree
from eawf.surfaces.cli import errors as cli_errors
from tests.integration.runtime.daemon.test_native_dispatch import make_repo

pytestmark = pytest.mark.integration

MESSAGE: Final = "Run RUN-00000001 changes for EAWF-0001\n\nTask: EAWF-0001\n"


def git_out(repo: Path, *args: str) -> str:
    """Return one git command's stripped stdout in *repo*."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A one-commit repository on ``main``."""
    root = tmp_path / "repo"
    make_repo(root)
    return root


def test_an_unchanged_worktree_makes_no_commit(repo: Path) -> None:
    head = git_out(repo, "rev-parse", "HEAD")

    assert commit_worktree(repo, message=MESSAGE) is None
    assert git_out(repo, "rev-parse", "HEAD") == head


def test_a_change_is_committed_on_the_checked_out_branch_as_eawf(repo: Path) -> None:
    base = git_out(repo, "rev-parse", "HEAD")
    (repo / "src" / "hello.txt").write_text("canary\n", encoding="utf-8")

    commit = commit_worktree(repo, message=MESSAGE)

    assert commit is not None
    assert git_out(repo, "rev-parse", "main") == commit
    assert git_out(repo, "rev-parse", f"{commit}^") == base
    assert git_out(repo, "show", f"{commit}:src/hello.txt") == "canary"
    assert git_out(repo, "log", "-1", "--format=%an|%cn", commit) == "eawf|eawf"
    assert git_out(repo, "log", "-1", "--format=%(trailers:key=Task,valueonly)", commit) == (
        "EAWF-0001"
    )
    assert git_out(repo, "status", "--porcelain") == ""


def test_an_excluded_path_stays_out_of_the_commit_and_in_the_tree(repo: Path) -> None:
    (repo / "src" / "module.py").write_text("x = 2\n", encoding="utf-8")
    (repo / ".isolation").mkdir()
    (repo / ".isolation" / "capsule.json").write_text("{}", encoding="utf-8")

    commit = commit_worktree(repo, message=MESSAGE, exclude=(".isolation",))

    assert commit is not None
    assert git_out(repo, "show", "--name-only", "--format=", commit) == "src/module.py"
    assert (repo / ".isolation" / "capsule.json").is_file()


def test_only_an_excluded_change_makes_no_commit(repo: Path) -> None:
    (repo / ".isolation").mkdir()
    (repo / ".isolation" / "capsule.json").write_text("{}", encoding="utf-8")

    assert commit_worktree(repo, message=MESSAGE, exclude=(".isolation",)) is None


def test_a_directory_that_is_not_a_worktree_is_refused(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(cli_errors.StateConflict):
        commit_worktree(plain, message=MESSAGE)


def test_an_excluded_path_the_checkout_already_ignores_still_commits_the_work(
    repo: Path,
) -> None:
    (repo / ".gitignore").write_text(".eawf-mcp/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "i"],
        cwd=repo,
        check=True,
    )
    (repo / ".eawf-mcp").mkdir()
    (repo / ".eawf-mcp" / "config.json").write_text("{}\n", encoding="utf-8")
    (repo / "notes.txt").write_text("alpha\n", encoding="utf-8")

    commit = commit_worktree(repo, message=MESSAGE, exclude=(".eawf-mcp",))

    assert commit is not None
    assert git_out(repo, "show", f"{commit}:notes.txt") == "alpha"
    assert ".eawf-mcp" not in git_out(repo, "ls-tree", "-r", "--name-only", commit)
