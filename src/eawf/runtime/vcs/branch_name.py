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
from dataclasses import dataclass
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


#: The refusal for a repository with no main working tree to hold its ``.ea``.
BARE_REPOSITORY_MESSAGE: Final = "bare repository: pass -w <checkout> or set EA_STATE"

_TRUE_WORDS: Final = frozenset({"true", "yes", "on", "1"})


class BareRepositoryError(FileNotFoundError):
    """The repository has no main working tree, so it has no ``.ea`` home.

    A missing tree, so every caller that tolerates no tree tolerates this one.
    """


@dataclass(frozen=True)
class RepositoryHome:
    """Where a repository keeps its ``.ea`` and whether the probed tree is linked.

    Attributes:
        main_root: The main working tree, which holds the repository's ``.ea``.
        linked: The probed directory lies in a linked worktree, whose own
            ``.ea`` is a frozen copy and never the repository's state.
    """

    main_root: Path
    linked: bool


def _core_config(common_dir: Path) -> dict[str, str]:
    """Return the ``core.bare`` and ``core.worktree`` values in *common_dir*'s config.

    Args:
        common_dir: The repository's common git directory.

    Returns:
        ``{"core.bare": ..., "core.worktree": ...}`` holding the keys set;
        empty when ``git`` is missing, does not answer in time or neither is set.
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "config",
                "--file",
                str(common_dir / "config"),
                "--get-regexp",
                r"^core\.(bare|worktree)$",
            ],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            **no_window_kwargs(),
        )
    except OSError, subprocess.TimeoutExpired:
        return {}
    values: dict[str, str] = {}
    for row in proc.stdout.splitlines():
        key, _, value = row.partition(" ")
        values[key.lower()] = value
    return values


def repository_home(root: Path) -> RepositoryHome | None:
    """Return the main working tree of the repository *root* is in.

    One ``rev-parse`` names the common and the per-tree git directory; they
    differ only in a linked worktree. A common directory named ``.git`` sits in
    its main checkout; any other (a submodule, a separate git directory) names
    its checkout through ``core.worktree``, relative to that directory, and
    without one the working tree top holding *root* is the main checkout.

    Args:
        root: Any directory, inside a repository or not.

    Returns:
        The repository's home; ``None`` outside a repository, inside a git
        directory itself, or when ``git`` is missing or does not answer in
        time.

    Raises:
        BareRepositoryError: The repository is bare, its common config sets
            ``core.bare`` true, or a linked tree's separate git directory
            names no ``core.worktree``.
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "rev-parse",
                "--is-bare-repository",
                "--path-format=absolute",
                "--git-common-dir",
                "--git-dir",
            ],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            **no_window_kwargs(),
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    lines = proc.stdout.splitlines()
    if proc.returncode != 0 or len(lines) != 3:
        return None
    if lines[0] == "true":
        raise BareRepositoryError(BARE_REPOSITORY_MESSAGE)
    common_dir, git_dir = Path(lines[1]), Path(lines[2])
    linked = common_dir != git_dir
    named = named_main_root(common_dir, git_dir)
    if named is not None:
        return RepositoryHome(main_root=named, linked=linked)
    top = _show_toplevel(root)
    return None if top is None else RepositoryHome(main_root=top, linked=False)


def named_main_root(common_dir: Path, git_dir: Path) -> Path | None:
    """Return the main working tree a repository's git directories name.

    The hook wrapper's probe applies the same rule in bash, so a hook and the
    state it resolves agree on one checkout.

    Args:
        common_dir: The repository's common git directory, absolute.
        git_dir: The probed tree's own git directory, absolute; it differs
            from *common_dir* only in a linked worktree.

    Returns:
        The parent of a common directory named ``.git``, the checkout a
        ``core.worktree`` names, or, for a linked tree of a separate git
        directory, the main checkout a registered linked tree lies in;
        ``None`` for the main tree of a separate git directory with no
        ``core.worktree``, which is the working tree top itself, as
        ``git init --separate-git-dir`` leaves it.

    Raises:
        BareRepositoryError: The common config sets ``core.bare`` true, or a
            linked tree's separate git directory names no ``core.worktree``
            and no registered linked tree lies in its main checkout, since
            git records no main working tree for one.
    """
    linked = common_dir != git_dir
    # The main checkout's own rev-parse already reported bareness; only a
    # linked tree can sit on a common directory configured bare.
    config = _core_config(common_dir) if linked or common_dir.name != ".git" else {}
    if config.get("core.bare", "").lower() in _TRUE_WORDS:
        raise BareRepositoryError(BARE_REPOSITORY_MESSAGE)
    if common_dir.name == ".git":
        return common_dir.parent
    worktree = config.get("core.worktree")
    if worktree:
        return (common_dir / worktree).resolve()
    if linked:
        registered = _registered_main(common_dir)
        if registered is None:
            raise BareRepositoryError(BARE_REPOSITORY_MESSAGE)
        return registered
    return None


def _registered_main(common_dir: Path) -> Path | None:
    """Return the main checkout of a separate git directory its linked trees show.

    git records no main tree for a separate git directory, but a linked tree
    registered inside the main checkout shows it: the nearest directory above
    such a tree whose ``.git`` file names *common_dir* itself; a linked
    tree's ``.git`` file names its own entry under ``worktrees/`` instead.
    The hook wrapper's probe applies the same rule.

    Args:
        common_dir: The separate git directory, absolute.

    Returns:
        That checkout, resolved; ``None`` when no registered tree lies in one.
    """
    common = common_dir.resolve()
    try:
        entries = sorted((common / "worktrees").iterdir())
    except OSError:
        return None
    for entry in entries:
        try:
            recorded = (entry / "gitdir").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not recorded:
            continue
        tree = (entry / recorded).resolve().parent
        for candidate in tree.parents:
            if _gitfile_target(candidate / ".git") == common:
                return candidate
    return None


def _gitfile_target(marker: Path) -> Path | None:
    """Return the git directory the ``.git`` file *marker* names, resolved, or ``None``."""
    try:
        line = marker.read_text(encoding="utf-8").splitlines()[0]
    except OSError, UnicodeDecodeError, IndexError:
        return None
    if not line.startswith("gitdir: "):
        return None
    return (marker.parent / line.removeprefix("gitdir: ")).resolve()


def _show_toplevel(root: Path) -> Path | None:
    """Return the working tree top holding *root*; ``None`` when git names none."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            **no_window_kwargs(),
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    top = proc.stdout.strip()
    return Path(top) if proc.returncode == 0 and top else None


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
        home = repository_home(root)
    except BareRepositoryError:
        return root
    return root if home is None else home.main_root


__all__ = [
    "BARE_REPOSITORY_MESSAGE",
    "BareRepositoryError",
    "RepositoryHome",
    "branch_name_error",
    "branch_task_key",
    "checked_out_branch",
    "main_checkout",
    "named_main_root",
    "repository_home",
]
