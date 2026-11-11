"""The repository context a host hook fires in.

A host fires a hook wherever its session stands: the root of a repository, a
subfolder, a linked worktree, a repository eawf does not manage, or no
repository at all. The wrapper probes this in bash before any interpreter
starts; :func:`resolve_hook_context` repeats the same steps, so the Python
side answers for the same checkout the wrapper chose.

The module imports only the standard library, the console-window kwargs and
the git-directory rule of :func:`~eawf.runtime.vcs.branch_name.named_main_root`
because every hook process loads it.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.runtime.vcs.branch_name import BareRepositoryError, named_main_root

type HookContextKind = Literal["managed", "unmanaged-git", "no-git"]

#: The files whose presence in a main checkout's ``.ea`` marks it managed.
MANAGED_ANCHORS: Final[tuple[Path, ...]] = (
    Path(".ea") / "state.json",
    Path(".ea") / "generations" / "EPOCH2_ACTIVE.json",
)

#: How long the git probe may take; a hook's whole budget is a few seconds.
_GIT_TIMEOUT_S: Final = 2.0


@dataclass(frozen=True, slots=True)
class HookContext:
    """Where a hook fired.

    Attributes:
        kind: ``managed`` when the main checkout holds a managed anchor,
            ``unmanaged-git`` for any other git working tree, ``no-git``
            outside one.
        top: The working tree the hook fired in; the directory itself
            outside git.
        main: The main checkout of that working tree, which a linked worktree
            shares with its repository; equal to ``top`` otherwise. Outside
            git, the first directory upward holding ``.ea/``, else ``top``.
    """

    kind: HookContextKind
    top: Path
    main: Path


def resolve_hook_context(cwd: Path) -> HookContext:
    """Return the repository context of *cwd*.

    Args:
        cwd: The directory the host reported, or the process's own.

    Returns:
        The context. A subfolder or a linked worktree of a managed repository
        resolves ``main`` to the main checkout and so is ``managed``. A
        missing directory, a bare repository, a linked tree whose main
        checkout cannot be named (one of a separate git directory with no
        ``core.worktree`` and no registered linked tree inside its main
        checkout), a missing ``git`` or a probe that does not
        answer in time is ``no-git`` at *cwd*, its ``main``
        the first directory upward holding ``.ea/``, as the state resolver
        walks outside git.

    Raises:
        TypeError: *cwd* is not a :class:`~pathlib.Path`.
    """
    if not isinstance(cwd, Path):
        raise TypeError(f"cwd must be a Path; got {type(cwd).__name__}")
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(cwd),
                "rev-parse",
                "--path-format=absolute",
                "--show-toplevel",
                "--git-common-dir",
                "--git-dir",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
            **no_window_kwargs(),
        )
    except OSError, subprocess.TimeoutExpired:
        return _no_git(cwd)
    lines = completed.stdout.splitlines()
    if completed.returncode != 0 or len(lines) != 3:
        return _no_git(cwd)
    top = Path(lines[0])
    try:
        main = named_main_root(Path(lines[1]), Path(lines[2])) or top
    except BareRepositoryError:
        # No main checkout git can name has an .ea; the state resolver refuses it too.
        return _no_git(cwd)
    managed = any((main / anchor).is_file() for anchor in MANAGED_ANCHORS)
    return HookContext(kind="managed" if managed else "unmanaged-git", top=top, main=main)


def _no_git(cwd: Path) -> HookContext:
    """Return the context of *cwd* outside git, its ``main`` the nearest ``.ea/`` holder."""
    home = next((d for d in (cwd, *cwd.parents) if (d / ".ea").is_dir()), cwd)
    return HookContext(kind="no-git", top=cwd, main=home)


def managed_tree(directory: Path) -> Path | None:
    """Return the root of the managed tree *directory* lies in, or ``None``.

    A managed tree is a git repository whose main checkout holds ``.ea/``, with
    its subfolders and linked worktrees, or a directory outside any git
    repository that holds ``.ea/state.json``, with everything under it. The
    walk goes up to the filesystem root, as the hook wrapper's does: a level
    holding ``.ea/state.json`` is kept, and a level holding ``.git`` asks git
    once for its main checkout. A ``.git`` git does not take, and a
    repository whose main checkout holds no ``.ea/``, are passed, since a
    nested clone or a submodule may lie in a managed checkout further up.

    Args:
        directory: An absolute directory, its symlinks already resolved.

    Returns:
        The first managed main checkout upward, else the nearest directory
        holding ``.ea/state.json``.
    """
    holder: Path | None = None
    for candidate in (directory, *directory.parents):
        if holder is None and (candidate / ".ea" / "state.json").is_file():
            holder = candidate
        if (candidate / ".git").exists():
            context = resolve_hook_context(candidate)
            if context.kind != "no-git" and (context.main / ".ea").is_dir():
                return context.main
    return holder


__all__ = [
    "MANAGED_ANCHORS",
    "HookContext",
    "HookContextKind",
    "managed_tree",
    "resolve_hook_context",
]
