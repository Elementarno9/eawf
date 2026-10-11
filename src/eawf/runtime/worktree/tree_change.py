"""The working tree on either side of an edit: its digest, and what changed between two.

A snapshot is the git tree the working tree would commit to right now, tracked and
untracked-but-not-ignored files alike. It is written through a private copy of the
checkout's index, so the operator's staging area is never touched and git's stat cache
still spares re-hashing every unchanged file. The tree object it writes stays in the
object store, which is what lets a later snapshot be diffed against it by id alone.

The digest is the same ``{"git_tree": <id>}`` canonical digest a delivered revision
carries, so a file change and a delivery name one tree the same way. When a Run's
work is kept, :func:`commit_worktree` records the working tree as a commit on the
branch its worktree has checked out.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

import eawf.runtime.worktree.git as git
from eawf.kernel.delivery.receipts import canonical_digest
from eawf.surfaces.cli import errors as cli_errors

#: Options every diff here is read with, so an operator's git configuration cannot
#: route it through an external driver, a text conversion or colour.
_PLAIN_DIFF: tuple[str, ...] = ("--no-color", "--no-ext-diff", "--no-textconv")


@dataclass(frozen=True, slots=True)
class TreeSnapshot:
    """One working tree, as the tree object it would commit to.

    Attributes:
        git_tree: The tree object's id.
        digest: The ``sha256:`` digest the tree is named by in a record.
    """

    git_tree: str
    digest: str


def _git(workspace: Path, *args: str, env: dict[str, str] | None = None) -> str:
    """Run one git subcommand in *workspace* and return its stdout.

    Raises:
        StateConflict: The subcommand exited non-zero.
    """
    result = git.invoke(workspace, *args, env=env)
    if result.returncode != 0:
        raise cli_errors.StateConflict(
            f"git {args[0]} failed while reading the working tree (rc={result.returncode}): "
            f"{(result.stderr or result.stdout).strip() or 'unknown'}",
            kind="IntegrityViolation",
        )
    return result.stdout


def snapshot_tree(workspace: Path) -> TreeSnapshot:
    """Return the tree *workspace*'s working files would commit to now.

    Args:
        workspace: Any directory inside a git working tree.

    Returns:
        The tree and its digest.

    Raises:
        StateConflict: *workspace* is not in a git working tree, or git could
            not write the tree.
    """
    root = Path(_git(workspace, "rev-parse", "--show-toplevel").strip())
    index = root / _git(root, "rev-parse", "--git-path", "index").strip()
    with tempfile.TemporaryDirectory(prefix="eawf-tree-") as scratch:
        private = Path(scratch) / "index"
        if index.is_file():
            # The copy keeps the index's own mtime: git trusts a cached stat only for
            # files older than the index, and a fresh mtime would hide an edit made in
            # the same second the index was last written.
            shutil.copy2(index, private)
        env = os.environ | {"GIT_INDEX_FILE": str(private)}
        _git(root, "add", "--all", "--", ".", env=env)
        tree = _git(root, "write-tree", env=env).strip()
    return TreeSnapshot(git_tree=tree, digest=canonical_digest({"git_tree": tree}))


def changed_paths(
    workspace: Path, before: TreeSnapshot, after: TreeSnapshot, *, paths: Sequence[str] = ()
) -> tuple[str, ...]:
    """Return the repository-relative paths that differ between two snapshots.

    Args:
        workspace: Any directory inside the working tree both were taken of.
        before: The earlier snapshot.
        after: The later snapshot.
        paths: Limits the comparison to these repository-relative paths; every
            path when empty.

    Returns:
        The differing paths in git's order; empty when the trees agree.
    """
    if before.git_tree == after.git_tree:
        return ()
    out = _git(
        workspace,
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        before.git_tree,
        after.git_tree,
        "--",
        *paths,
    )
    return tuple(path for path in out.split("\0") if path)


def tree_diff(
    workspace: Path,
    before: TreeSnapshot,
    after: TreeSnapshot,
    *,
    paths: Sequence[str] = (),
    stat_only: bool = False,
) -> str:
    """Return the unified diff, or its per-file summary, between two snapshots.

    Args:
        workspace: Any directory inside the working tree both were taken of.
        before: The earlier snapshot.
        after: The later snapshot.
        paths: Limits the diff to these repository-relative paths; every path
            when empty.
        stat_only: Return git's per-file change summary instead of the hunks.

    Returns:
        The diff text; empty when the trees agree on *paths*.
    """
    shape = ("--stat=200", "--summary") if stat_only else ()
    return _git(
        workspace, "diff", *_PLAIN_DIFF, *shape, before.git_tree, after.git_tree, "--", *paths
    )


#: Who a commit eawf makes on a worker's behalf is authored and committed by. Fixed
#: rather than read from git configuration, so a Run's commit never carries the
#: operator's identity for work the operator did not write.
COMMIT_IDENTITY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "GIT_AUTHOR_NAME": "eawf",
        "GIT_AUTHOR_EMAIL": "eawf@example.invalid",
        "GIT_COMMITTER_NAME": "eawf",
        "GIT_COMMITTER_EMAIL": "eawf@example.invalid",
    }
)


def commit_worktree(workspace: Path, *, message: str, exclude: Sequence[str] = ()) -> str | None:
    """Commit every change in *workspace* on the branch it has checked out.

    The commit is written with plumbing rather than ``git commit``: hooks are a
    person's policy over their own commits, and this one is eawf's record of a
    worker's. The index is the checkout's own, so the worktree reads as clean
    afterwards except for whatever *exclude* kept out.

    Args:
        workspace: The root of a git worktree.
        message: The commit message, trailers included.
        exclude: Repository-relative paths left out of the commit.

    Returns:
        The new commit's SHA, or ``None`` when nothing changed and no commit was
        made.

    Raises:
        StateConflict: Git could not stage, write or record the commit.
    """
    # An excluded path the checkout's .gitignore already ignores makes an
    # exclude pathspec fail ``git add``, so stage everything and unstage it.
    _git(workspace, "add", "--all", "--", ".")
    if exclude:
        _git(workspace, "rm", "-r", "--cached", "--quiet", "--ignore-unmatch", "--", *exclude)
    head = _git(workspace, "rev-parse", "HEAD").strip()
    tree = _git(workspace, "write-tree").strip()
    if tree == _git(workspace, "rev-parse", "HEAD^{tree}").strip():
        return None
    made = git.invoke(
        workspace,
        "commit-tree",
        tree,
        "-p",
        head,
        "-F",
        "-",
        input_text=message,
        env=os.environ | dict(COMMIT_IDENTITY),
    )
    if made.returncode != 0:
        raise cli_errors.StateConflict(
            f"git commit-tree failed in the worktree (rc={made.returncode}): "
            f"{(made.stderr or made.stdout).strip() or 'unknown'}",
            kind="IntegrityViolation",
        )
    commit = made.stdout.strip()
    _git(workspace, "update-ref", "HEAD", commit, head)
    return commit


__all__ = [
    "COMMIT_IDENTITY",
    "TreeSnapshot",
    "changed_paths",
    "commit_worktree",
    "snapshot_tree",
    "tree_diff",
]
