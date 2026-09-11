"""Read-only registry helpers for ``~/.eawf/registry.json``.

The user-scope registry is the index of repos the operator has
explicitly initialised or registered. Per the project memory note
``feedback_explicit_registry_only`` the registry grows ONLY through
explicit ``eawf init`` / ``eawf repo add`` writes; there is no scan,
walk, or import-from-discovery path. This package ships:

- :mod:`eawf.platform.registry.models`: Pydantic models, default-path
  resolver, JSON loader, and the explicit-growth guard
  (:func:`reject_implicit_growth`).
- :mod:`eawf.platform.registry.staleness`: the 14-day OR-chain
  (:func:`is_stale`) plus the mtime / state-load helpers that feed
  it.
- :mod:`eawf.platform.registry.workspace`: the workspace resolution
  ladder (:func:`resolve_workspace`) and the membership algebra the
  daemon mutator and its daemonless fallback share.

The mutator side lives in :mod:`eawf.surfaces.cli.commands.repo` (which
dispatches to the daemon's ``registry.update`` RPC by default per
D-SUP-01); nothing under :mod:`eawf.platform.registry` ever writes.

The single-module surface this package shipped through P20-W05 is
re-exported here so the 20+ existing import sites in CLI, TUI, and
daemon code keep working without a sweep.
"""

from __future__ import annotations

from eawf.platform.registry.models import (
    EXPLICIT_GROWTH_SURFACES,
    FORBIDDEN_GROWTH_PATHS,
    ImplicitRegistryGrowthError,
    Registry,
    RegistryReadError,
    RegistryRepoEntry,
    WorkspaceRecord,
    default_registry_path,
    read_registry,
    reject_implicit_growth,
)
from eawf.platform.registry.staleness import (
    STALE_AFTER,
    is_stale,
    read_repo_state,
    registry_mtime,
    repo_state_mtime,
)
from eawf.platform.registry.workspace import (
    WORKSPACE_ALREADY_REGISTERED,
    WORKSPACE_AMBIGUOUS,
    WORKSPACE_NOT_REGISTERED,
    WORKSPACE_REVISION_CONFLICT,
    WorkspaceMutationError,
    WorkspaceResolution,
    WorkspaceResolutionError,
    WorkspaceSource,
    create_workspace,
    get_workspace,
    list_workspaces,
    project_codes_at_root,
    resolve_workspace,
    update_membership,
)

__all__ = [
    "EXPLICIT_GROWTH_SURFACES",
    "FORBIDDEN_GROWTH_PATHS",
    "STALE_AFTER",
    "WORKSPACE_ALREADY_REGISTERED",
    "WORKSPACE_AMBIGUOUS",
    "WORKSPACE_NOT_REGISTERED",
    "WORKSPACE_REVISION_CONFLICT",
    "ImplicitRegistryGrowthError",
    "Registry",
    "RegistryReadError",
    "RegistryRepoEntry",
    "WorkspaceMutationError",
    "WorkspaceRecord",
    "WorkspaceResolution",
    "WorkspaceResolutionError",
    "WorkspaceSource",
    "create_workspace",
    "default_registry_path",
    "get_workspace",
    "is_stale",
    "list_workspaces",
    "project_codes_at_root",
    "read_registry",
    "read_repo_state",
    "registry_mtime",
    "reject_implicit_growth",
    "repo_state_mtime",
    "resolve_workspace",
    "update_membership",
]
