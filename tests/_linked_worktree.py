"""Test helper that builds a repository holding an eawf tree and one linked worktree."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests._epoch2_helpers import lay_epoch2_tree

#: Identity and isolation for fixture commits: no signing, no host hooks.
_GIT_CONFIG: tuple[str, ...] = (
    "-c",
    "user.name=ci",
    "-c",
    "user.email=ci@example.com",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.hooksPath=/dev/null",
)


def git(cwd: Path, *args: str) -> str:
    """Run ``git`` in *cwd* and return its stdout; raise on a non-zero exit."""
    completed = subprocess.run(
        ["git", *_GIT_CONFIG, "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def repo_with_linked_worktree(root: Path, *, project_code: str = "QR") -> tuple[Path, Path]:
    """Commit an epoch-2 tree in a repository at ``root/main`` and add a linked worktree.

    The worktree at ``root/linked`` checks out the committed ``.ea``, a frozen
    copy the resolver must never pick.

    Args:
        root: An empty directory outside any repository.
        project_code: The code the tree's generation is named for.

    Returns:
        ``(main, linked)``: the main checkout and the linked worktree, resolved.
    """
    main = root / "main"
    main.mkdir(parents=True)
    git(main, "init", "-q", "-b", "main")
    lay_epoch2_tree(main, project_code=project_code)
    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "tree")
    linked = root / "linked"
    git(main, "worktree", "add", "-q", "-b", "task", str(linked))
    return main.resolve(), linked.resolve()
