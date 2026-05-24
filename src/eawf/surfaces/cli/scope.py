"""Scope resolution for CLI handlers.

Two entry points:

1. :func:`resolve_state_path` — single-repo precedence chain for the
   active ``.ea/state.json`` path (``EA_STATE`` env > ``-w`` flag >
   pwd-upward walk). Used by every per-repo CLI command (~10 call
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
model: ``cd <repo>; eawf wave claim`` operates on the repo, not on
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

from eawf.registry import (
    Registry,
    RegistryReadError,
    RegistryRepoEntry,
    default_registry_path,
    read_registry,
)

logger = logging.getLogger(__name__)


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
    3. Pwd-upward walk through parents.

    Args:
        workspace: Optional workspace root from ``-w / --workspace``.
            When set and ``EA_STATE`` is unset the resolver appends
            ``.ea/state.json`` without checking existence; callers
            that need a hard existence check must perform it
            themselves.

    Returns:
        The resolved state path; may not exist on disk.

    Raises:
        FileNotFoundError: When no candidate is found via the
            three-tier precedence chain.
    """
    env = os.environ.get("EA_STATE")
    if env:
        logger.debug(f"resolve_state_path env-hit path={env}")
        return Path(env)
    if workspace is not None:
        candidate = Path(workspace) / ".ea" / "state.json"
        logger.debug(f"resolve_state_path workspace-flag path={candidate}")
        return candidate
    cur = Path.cwd().resolve()
    for directory in [cur, *cur.parents]:
        target = directory / ".ea" / "state.json"
        if target.exists():
            logger.debug(f"resolve_state_path pwd-upward-hit path={target}")
            return target
    raise FileNotFoundError(
        "No .ea/state.json found upward from cwd; pass -w or set EA_STATE",
    )


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
    "ScopeResolution",
    "ScopeTier",
    "resolve_scope_tier",
    "resolve_state_path",
]
