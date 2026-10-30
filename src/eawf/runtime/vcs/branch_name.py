"""The Milestone and Task branch-name grammar, the Task a checked-out branch names, and
the main checkout a linked worktree belongs to.

A Milestone branch is ``feature/<symbol>-v<X.Y>`` and a Task worktree branch is
``feature/<symbol>-v<X.Y>-<task-key>``, the Task key in lower case
(``feature/eawf-v0.7-eawf-0628``). The branch-name lint refuses any other suffix on a
branch that looks like one of them, and a host session started on a Task branch is
bound to that Task, so both read the one grammar held here.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Final

from eawf.platform.subprocess_detach import no_window_kwargs

# Mirrors the project-code grammar (one letter, then 1 to 15 letters, digits,
# underscores or hyphens) in lower case; the Task key repeats the symbol.
_CANONICAL_RE: Final = re.compile(
    r"^feature/(?P<symbol>[a-z][a-z0-9_-]{1,15})-v\d+\.\d+(?:-(?P<task>(?P=symbol)-\d{4}))?$"
)
_IN_SCOPE_RE: Final = re.compile(r"^feature/[A-Za-z][^/]*-v\d")

#: How long the branch read may take. A healthy repository answers in milliseconds, and
#: the read runs while a host session starts, so a stalled mount must not hold it long.
_GIT_TIMEOUT_SECONDS: Final = 2.0


def branch_name_error(branch: str) -> str | None:
    """Return why *branch* breaks the Milestone and Task naming, or ``None``.

    Args:
        branch: A short branch name, such as ``feature/eawf-v0.7``.

    Returns:
        A diagnostic for a branch that looks like a Milestone or Task branch
        but is not in canonical form; ``None`` for a canonical branch or one
        out of scope.
    """
    if not _IN_SCOPE_RE.match(branch) or _CANONICAL_RE.match(branch):
        return None
    return (
        f"branch {branch!r} is not a canonical Milestone or Task branch: name a Milestone "
        "branch feature/<symbol>-v<X.Y> and a Task branch feature/<symbol>-v<X.Y>-<task-key> "
        "with the Task key in lower case (feature/eawf-v0.7-eawf-0628); no other suffix"
    )


def branch_task_key(branch: str) -> str | None:
    """Return the Task key a canonical Task branch names, in upper case.

    Args:
        branch: A short branch name.

    Returns:
        ``EAWF-0628`` for ``feature/eawf-v0.7-eawf-0628``; ``None`` for a
        Milestone branch, a non-canonical branch or any other branch.
    """
    match = _CANONICAL_RE.match(branch)
    task = None if match is None else match.group("task")
    return None if task is None else task.upper()


def checked_out_branch(root: Path) -> str | None:
    """Return the branch checked out in the repository at *root*.

    Args:
        root: Any directory inside the repository.

    Returns:
        The short branch name; ``None`` on a detached ``HEAD``, outside a
        repository, or when ``git`` is missing or does not answer in time.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            **no_window_kwargs(),
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def main_checkout(root: Path) -> Path:
    """Return the main working tree of the repository *root* is in.

    A linked worktree shares its repository's common git directory, whose
    parent is the main checkout; machine-local files kept for the repository
    belong there, because the main tree reads them and removing the worktree
    would delete them.

    Args:
        root: Any directory, inside a repository or not.

    Returns:
        The main checkout's root; *root* itself outside a repository, in a
        bare repository, or when ``git`` is missing or does not answer in time.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            **no_window_kwargs(),
        )
    except OSError, subprocess.TimeoutExpired:
        return root
    common = Path(proc.stdout.strip())
    if proc.returncode != 0 or common.name != ".git":
        return root
    return common.parent


__all__ = ["branch_name_error", "branch_task_key", "checked_out_branch", "main_checkout"]
