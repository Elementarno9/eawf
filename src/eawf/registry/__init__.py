"""Read-only registry helpers for ``~/.eawf/registry.json``.

The user-scope registry is the index of repos the operator has
explicitly initialised or registered. Per the project memory note
``feedback_explicit_registry_only`` the registry grows ONLY through
explicit ``eawf init`` / ``eawf repo add`` writes; there is no scan,
walk, or import-from-discovery path. This package ships:

- :mod:`eawf.registry.models`: Pydantic models, default-path
  resolver, JSON loader, and the explicit-growth guard
  (:func:`reject_implicit_growth`).
- :mod:`eawf.registry.staleness`: the 14-day OR-chain
  (:func:`is_stale`) plus the mtime / state-load helpers that feed
  it.

The mutator side lives in :mod:`eawf.surfaces.cli.commands.repo` (which
dispatches to the daemon's ``registry.update`` RPC by default per
D-SUP-01); nothing under :mod:`eawf.registry` ever writes.

The single-module surface this package shipped through P20-W05 is
re-exported here so the 20+ existing import sites in CLI, TUI, and
daemon code keep working without a sweep.
"""

from __future__ import annotations

from eawf.registry.models import (
    EXPLICIT_GROWTH_SURFACES,
    FORBIDDEN_GROWTH_PATHS,
    ImplicitRegistryGrowthError,
    Registry,
    RegistryReadError,
    RegistryRepoEntry,
    default_registry_path,
    read_registry,
    reject_implicit_growth,
)
from eawf.registry.staleness import (
    STALE_AFTER,
    is_stale,
    read_repo_state,
    registry_mtime,
    repo_state_mtime,
)

__all__ = [
    "EXPLICIT_GROWTH_SURFACES",
    "FORBIDDEN_GROWTH_PATHS",
    "STALE_AFTER",
    "ImplicitRegistryGrowthError",
    "Registry",
    "RegistryReadError",
    "RegistryRepoEntry",
    "default_registry_path",
    "is_stale",
    "read_registry",
    "read_repo_state",
    "registry_mtime",
    "reject_implicit_growth",
    "repo_state_mtime",
]
