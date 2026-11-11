"""Scope resolution for CLI handlers.

Two entry points:

1. :func:`resolve_state_path` — single-repo precedence chain for the
   active ``.ea/state.json`` path (``EA_STATE`` env > ``-w`` flag >
   upward walk from the repository's main checkout). Used by every per-repo CLI command (~10 call
   sites: lifecycle, roadmap, metrics, hook, ...).

2. :func:`resolve_scope_tier` — multi-repo dispatch ladder
   (cwd → workspace > repo > user) for the workspace dashboard and
   any future portfolio surface. The ladder maps the invocation cwd to
   one of three tiers:

   - **repo**: cwd resolves to a registered repo (an entry in
     ``~/.eawf/registry.json`` whose ``path`` is an ancestor of cwd
     or equal to it). Operate on that repo's per-repo state.
   - **workspace**: cwd does not match any registered repo but a
     non-empty registry exists. Render the workspace dashboard
     (active_code + repos list).
   - **user**: registry is missing or empty. User-scope surfaces
     only; no per-repo state. Fall back to ``~/.eawfrc`` /
     ``~/.eawf/config.yaml`` when reads need a config.

Per the brief the ladder is "first match wins" so a repo-match
short-circuits the workspace tier even when an active workspace
state document also exists; this matches the operator's mental
model: ``cd <repo>; eawf task claim`` operates on the repo, not on
its parent workspace.

The ladder is **read-only**. Per ``feedback_explicit_registry_only``
nothing under this module ever grows the registry; a cwd that does
not resolve to a registered repo falls through to workspace/user
tiers rather than auto-registering. Manual backfill via
``eawf repo add <path>`` remains the supported bootstrap.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from eawf.kernel.state.resolve import fence_live_ledger, home_state, refuse_linked_worktree
from eawf.platform.registry import (
    Registry,
    RegistryReadError,
    RegistryRepoEntry,
    default_registry_path,
    read_registry,
)
from eawf.surfaces.cli.errors import UserError

logger = logging.getLogger(__name__)

#: The refusal of a project verb run where no eawf-managed repository resolves.
#: It equals ``eawf.workflow.skills.catalog.NOT_MANAGED_REFUSAL``, restated
#: because the catalog loads run-control models the statusline must not pay for.
NOT_MANAGED_MESSAGE: Final = "not an eawf-managed repository; run eawf init to manage it"


class NotManaged(UserError, FileNotFoundError):  # noqa: N818 - the CLI error kind is "NotManaged"
    """No eawf-managed repository resolves for a verb that needs one.

    It is a :class:`FileNotFoundError` as well, so a caller that handles a
    missing state file keeps handling it.
    """

    def __init__(self) -> None:
        super().__init__(NOT_MANAGED_MESSAGE)


class ScopeTier(StrEnum):
    """One of the three resolution tiers.

    Values:
        REPO: cwd resolves to a registered repo's working tree.
        WORKSPACE: registry is populated but cwd is outside every
            registered repo path.
        USER: registry is missing, empty, or unreadable.
    """

    REPO = "repo"
    WORKSPACE = "workspace"
    USER = "user"


@dataclass(frozen=True)
class ScopeResolution:
    """The result of running :func:`resolve_scope_tier`.

    Attributes:
        tier: Which tier the cwd matched.
        repo_entry: The matching :class:`RegistryRepoEntry` when
            *tier* is :attr:`ScopeTier.REPO`; ``None`` otherwise.
        registry_path: The on-disk path used to read the registry
            (the default ``~/.eawf/registry.json`` or the test
            override).
        registry: The decoded :class:`Registry` when the file was
            readable; ``None`` for the USER tier when no registry
            exists.
    """

    tier: ScopeTier
    repo_entry: RegistryRepoEntry | None
    registry_path: Path
    registry: Registry | None


def resolve_state_path(workspace: Path | None) -> Path:
    """Return the resolved ``.ea/state.json`` path for the current invocation.

    Precedence (highest first):

    1. ``EA_STATE`` environment variable.
    2. ``-w / --workspace`` flag.
    3. Upward walk from the main checkout of the repository holding the
       cwd, or from the cwd itself outside git
       (:func:`~eawf.kernel.state.resolve.home_state`).

    Args:
        workspace: Optional workspace root from ``-w / --workspace``.
            When set and ``EA_STATE`` is unset the resolver appends
            ``.ea/state.json`` without checking existence; callers
            that need a hard existence check must perform it
            themselves.

    Returns:
        The resolved state path; may not exist on disk. Under a gate harness
        a path landing on the fenced live ledger is its sandbox snapshot
        instead (:func:`~eawf.kernel.state.resolve.fence_live_ledger`).

    Raises:
        NotManaged: No candidate is found via the three-tier precedence
            chain: no eawf-managed repository holds the cwd.
        LinkedWorktreeStateError: ``EA_STATE`` or *workspace* lies inside a
            linked worktree.
        BareRepositoryError: No override was given and the cwd is in a
            repository with no main working tree.
    """
    env = os.environ.get("EA_STATE")
    if env:
        refuse_linked_worktree(Path(env))
        logger.debug(f"resolve_state_path env-hit path={env}")
        return fence_live_ledger(Path(env), os.environ)
    if workspace is not None:
        refuse_linked_worktree(Path(workspace))
        candidate = Path(workspace) / ".ea" / "state.json"
        logger.debug(f"resolve_state_path workspace-flag path={candidate}")
        return fence_live_ledger(candidate, os.environ)
    target, found = home_state(Path.cwd())
    if found:
        logger.debug(f"resolve_state_path pwd-upward-hit path={target}")
        return fence_live_ledger(target, os.environ)
    raise NotManaged


def _is_ancestor_or_equal(candidate: Path, descendant: Path) -> bool:
    """Return ``True`` when *candidate* equals or contains *descendant*.

    Uses :meth:`pathlib.Path.is_relative_to` so the comparison is
    purely path-based (no filesystem access beyond what the caller
    has already resolved). Both paths are expected to be absolute
    and ``resolve()``-d by the caller.
    """
    try:
        return descendant == candidate or descendant.is_relative_to(candidate)
    except ValueError, OSError:
        return False


def _match_repo_entry(registry: Registry, cwd: Path) -> RegistryRepoEntry | None:
    """Return the registry entry whose ``path`` covers *cwd*, or ``None``.

    Walks the registry once, picking the deepest ancestor match so
    nested-repo layouts (rare, but allowed) resolve to the closest
    registered repo rather than its parent. *cwd* is expected to be
    absolute and ``resolve()``-d by the caller.
    """
    best: RegistryRepoEntry | None = None
    best_depth = -1
    for entry in registry.repos.values():
        entry_path = Path(entry.path).resolve()
        if not _is_ancestor_or_equal(entry_path, cwd):
            continue
        depth = len(entry_path.parts)
        if depth > best_depth:
            best = entry
            best_depth = depth
    return best


def resolve_scope_tier(
    cwd: Path | None = None,
    *,
    registry_path: Path | None = None,
    home: Path | None = None,
) -> ScopeResolution:
    """Resolve the active scope tier per the cwd-driven ladder.

    Algorithm (first match wins):

    1. Read the registry at *registry_path* (or the user default).
       When the file is missing or unreadable, return
       :attr:`ScopeTier.USER` with ``registry=None``.
    2. Look up the *cwd* (or :func:`Path.cwd` when ``None``) against
       every registered repo path. The deepest ancestor match wins.
       On hit return :attr:`ScopeTier.REPO` with the matching entry.
    3. No repo match but the registry has at least one entry: return
       :attr:`ScopeTier.WORKSPACE` with the loaded registry.
    4. Empty registry: return :attr:`ScopeTier.USER`.

    Args:
        cwd: Working directory to dispatch from. Defaults to
            :func:`Path.cwd`. Tests pass an explicit ``tmp_path``
            so the resolution stays hermetic.
        registry_path: Explicit registry path. When ``None``, falls
            back to :func:`default_registry_path`. Tests pass a
            ``tmp_path``-rooted path so no real registry is touched.
        home: Test seam for the default-path branch. Ignored when
            *registry_path* is supplied directly.

    Returns:
        The :class:`ScopeResolution` describing the resolved tier,
        the matching repo entry (when applicable), and the registry
        that was consulted.
    """
    resolved_registry_path = (
        registry_path if registry_path is not None else default_registry_path(home=home)
    )
    resolved_cwd = (cwd or Path.cwd()).resolve()

    try:
        registry = read_registry(path=resolved_registry_path)
    except RegistryReadError as exc:
        logger.debug(f"resolve_scope_tier registry-unreadable error={exc}")
        return ScopeResolution(
            tier=ScopeTier.USER,
            repo_entry=None,
            registry_path=resolved_registry_path,
            registry=None,
        )

    entry = _match_repo_entry(registry, resolved_cwd)
    if entry is not None:
        logger.debug(f"resolve_scope_tier tier=repo cwd={resolved_cwd!r} code={entry.code!r}")
        return ScopeResolution(
            tier=ScopeTier.REPO,
            repo_entry=entry,
            registry_path=resolved_registry_path,
            registry=registry,
        )

    if registry.repos:
        logger.debug(f"resolve_scope_tier tier=workspace cwd={resolved_cwd!r}")
        return ScopeResolution(
            tier=ScopeTier.WORKSPACE,
            repo_entry=None,
            registry_path=resolved_registry_path,
            registry=registry,
        )

    logger.debug(f"resolve_scope_tier tier=user cwd={resolved_cwd!r} reason=empty-registry")
    return ScopeResolution(
        tier=ScopeTier.USER,
        repo_entry=None,
        registry_path=resolved_registry_path,
        registry=registry,
    )


__all__ = [
    "NOT_MANAGED_MESSAGE",
    "NotManaged",
    "ScopeResolution",
    "ScopeTier",
    "resolve_scope_tier",
    "resolve_state_path",
]
