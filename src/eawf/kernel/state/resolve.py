"""Pure resolver returning the active ``.ea/state.json`` path *and* the reason.

Same precedence as :func:`eawf.surfaces.cli.scope.resolve_state_path` (``EA_STATE`` >
``-w/--workspace`` > the repository's main checkout, upward) but, instead of
opaquely returning a path, also reports *why* the resolver picked that path.
The reason string is part of the public CLI contract emitted by the retired
``state resolve`` verb.

The resolver does **not** raise on a missing pwd-upward state — it returns the
candidate ``<walk start> / .ea / state.json`` with reason ``"pwd_upward"`` so callers
can distinguish "no state on disk" from "lookup failed". CLI commands that
require an existing state must verify ``path.exists()`` themselves.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path

from eawf.kernel.migration.epoch2.canary import GENERATIONS_DIRNAME, MARKER_FILENAME
from eawf.runtime.vcs.branch_name import BareRepositoryError, repository_home

logger = logging.getLogger(__name__)

REASON_ENV: str = "env"
REASON_WORKSPACE_FLAG: str = "workspace_flag"
REASON_PWD_UPWARD: str = "pwd_upward"

#: The ledger a gate harness forbids its gate command to reach. Set together
#: with :data:`GATE_SANDBOX_STATE_ENV` only by the daemon's gate runner. Named
#: outside the ``EAWF_`` family on purpose: a gate may run an older checkout
#: whose config layer reads every unreserved ``EAWF_*`` variable as an override.
GATE_LIVE_STATE_ENV: str = "EA_GATE_LIVE_STATE"

#: The throwaway snapshot of :data:`GATE_LIVE_STATE_ENV` that stands in for it.
GATE_SANDBOX_STATE_ENV: str = "EA_GATE_SANDBOX_STATE"


def fence_live_ledger(path: Path, environ: Mapping[str, str]) -> Path:
    """Return the sandbox ledger when *path* is the live ledger a gate is fenced from.

    A gate harness cannot pin every resolution to its sandbox: a gate that is
    a test suite builds its own workspaces and names them through ``-w``,
    ``EA_STATE`` and the working directory, and must reach exactly those, as
    it would outside the harness. Only a resolution that lands on the live
    ledger is swapped for its snapshot. The live ledger is the fenced path
    itself or the ``.ea`` tree that contains it, because a delivery proof
    fences a claim ledger nested under the repository's ``.ea`` while its
    checkout's upward walk reaches the repository ledger.

    Args:
        path: The ledger the precedence chain resolved.
        environ: The environment carrying the fence bindings.

    Returns:
        The sandbox ledger when *path* is fenced, else *path* unchanged.
    """
    live_raw = environ.get(GATE_LIVE_STATE_ENV)
    sandbox_raw = environ.get(GATE_SANDBOX_STATE_ENV)
    if not live_raw or not sandbox_raw:
        return path
    live = Path(live_raw).resolve()
    candidate = path.resolve()
    ledger_dir = candidate.parent
    fenced = candidate == live or (
        ledger_dir.name == ".ea" and live.parent.is_relative_to(ledger_dir)
    )
    if not fenced:
        return path
    logger.debug(f"fence_live_ledger live={str(candidate)!r} sandbox={sandbox_raw!r}")
    return Path(sandbox_raw)


class LinkedWorktreeStateError(FileNotFoundError):
    """``EA_STATE`` or ``-w`` names a tree inside a linked worktree.

    A missing tree, so every caller that tolerates no tree tolerates this one.
    """


def refuse_linked_worktree(named: Path) -> None:
    """Refuse *named* when it lies inside a linked worktree.

    A linked worktree checks out a frozen copy of ``.ea``; reading or writing
    it would split the repository's state, so an override naming one is an
    error rather than a silent redirect to the main checkout.

    Args:
        named: The ``EA_STATE`` path or ``-w`` directory, which need not exist.

    Raises:
        LinkedWorktreeStateError: *named* lies inside a linked worktree.
    """
    probe = named.absolute()
    while not probe.is_dir() and probe != probe.parent:
        probe = probe.parent
    try:
        home = repository_home(probe)
    except BareRepositoryError:
        # An override exists to name a tree git cannot place; a bare
        # repository has no linked tree to confuse it with.
        return
    if home is not None and home.linked:
        raise LinkedWorktreeStateError(
            f"{named} is a linked worktree; its .ea is never used. "
            "Unset it or point it at the main checkout"
        )


class MissingMainCheckoutError(LinkedWorktreeStateError):
    """A linked worktree's main checkout was moved or deleted.

    git cannot place such a tree, and its own ``.ea`` is a frozen copy, so the
    upward walk must not fall back to it.
    """


def _refuse_orphaned_worktree(cwd: Path) -> None:
    """Refuse *cwd* when the nearest ``.git`` above it is a gitfile naming a missing directory.

    Args:
        cwd: Where the resolution starts; git found no repository there.

    Raises:
        MissingMainCheckoutError: That gitfile's target is gone.
    """
    for directory in (cwd, *cwd.parents):
        marker = directory / ".git"
        if marker.is_dir():
            return
        if not marker.is_file():
            continue
        try:
            text = marker.read_text(encoding="utf-8").strip()
        except OSError, UnicodeDecodeError:
            return
        if not text.startswith("gitdir:"):
            return
        gitdir = Path(text.removeprefix("gitdir:").strip())
        gitdir = gitdir if gitdir.is_absolute() else directory / gitdir
        if gitdir.exists():
            return
        # A linked tree's gitdir is <main>/.git/worktrees/<name>.
        if gitdir.parent.name == "worktrees":
            missing = f"main checkout {gitdir.parent.parent.parent}"
        else:
            missing = f"git directory {gitdir}"
        raise MissingMainCheckoutError(
            f"{directory} is a linked worktree whose {missing} is missing; "
            "its .ea is never used. Restore the main checkout or point -w at it"
        )


def _anchors_tree(directory: Path) -> bool:
    """Return whether *directory* holds an eawf ``.ea`` tree."""
    # An epoch-2 tree need not hold the v1 document at all, so its epoch
    # marker anchors the tree just as the document does.
    ea = directory / ".ea"
    return (ea / "state.json").exists() or (ea / GENERATIONS_DIRNAME / MARKER_FILENAME).exists()


def home_state(cwd: Path) -> tuple[Path, bool]:
    """Return the repository's ``state.json`` for a process started in *cwd*.

    Inside git the walk starts at the main checkout, so a linked worktree and
    any subfolder reach the repository's ``.ea`` and never a worktree's frozen
    copy; outside git it starts at *cwd*. Either way it goes upward to the
    first directory holding a tree.

    Args:
        cwd: The working directory the resolution starts from.

    Returns:
        ``(path, found)``: the first tree's ``state.json`` and ``True``, or the
        walk start's ``.ea/state.json`` and ``False`` when no tree is found.

    Raises:
        BareRepositoryError: *cwd* is in a repository with no main working tree.
        MissingMainCheckoutError: *cwd* is in a linked worktree whose main
            checkout was moved or deleted.
    """
    home = repository_home(cwd)
    if home is None:
        _refuse_orphaned_worktree(cwd.absolute())
    start = cwd.resolve() if home is None else home.main_root.resolve()
    for directory in [start, *start.parents]:
        if _anchors_tree(directory):
            return directory / ".ea" / "state.json", True
    return start / ".ea" / "state.json", False


def resolve_with_reason(
    workspace: Path | None,
    env: os._Environ[str] | None = None,
) -> tuple[Path, str]:
    """Return the resolved state path *and* the reason it was selected.

    Args:
        workspace: Optional workspace root from ``-w/--workspace``. Used only
            when ``EA_STATE`` is unset.
        env: Optional environment mapping. Defaults to :data:`os.environ`.
            Tests inject a custom dict here to avoid global mutation.

    Returns:
        A two-tuple ``(path, reason)`` where ``reason`` is one of:

        - ``"env"`` — ``EA_STATE`` was set; *path* is its value verbatim.
        - ``"workspace_flag"`` — ``EA_STATE`` unset, ``workspace`` provided;
          *path* = ``workspace / .ea / state.json``.
        - ``"pwd_upward"`` — neither override was given; the resolver walked
          upward from the main checkout of the repository holding
          :func:`pathlib.Path.cwd` (from the cwd itself outside git) to the
          first ``.ea`` holding ``state.json`` or the epoch-2 marker, and
          *path* names that tree's ``state.json`` whether or not it exists.
          If no tree is found the resolver still returns the walk start's
          ``.ea/state.json`` so the caller can distinguish "no state file"
          from "lookup error".

        Under a gate harness a path that lands on the fenced live ledger is
        replaced by its sandbox snapshot (:func:`fence_live_ledger`); the
        reason still names the tier that selected it.

    Raises:
        LinkedWorktreeStateError: ``EA_STATE`` or *workspace* lies inside a
            linked worktree.
        BareRepositoryError: No override was given and the cwd is in a
            repository with no main working tree.
    """
    environ = env if env is not None else os.environ
    raw_env = environ.get("EA_STATE")
    if raw_env:
        refuse_linked_worktree(Path(raw_env))
        logger.debug(f"resolve_with_reason source=env path={raw_env}")
        return fence_live_ledger(Path(raw_env), environ), REASON_ENV
    if workspace is not None:
        refuse_linked_worktree(Path(workspace))
        candidate = Path(workspace) / ".ea" / "state.json"
        logger.debug(f"resolve_with_reason source=workspace-flag path={candidate}")
        return fence_live_ledger(candidate, environ), REASON_WORKSPACE_FLAG
    target, found = home_state(Path.cwd())
    logger.debug(f"resolve_with_reason source=pwd-upward found={found} path={target}")
    return fence_live_ledger(target, environ), REASON_PWD_UPWARD
