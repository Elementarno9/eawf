"""Canonical JSONL store path resolution.

Every JSONL store record lands at::

    <state_dir>/store/<StoreKind.value>.jsonl

This module is the single source of truth. Any caller that hand-rolls a
JSONL path (``state_path.parent / "events.jsonl"``, etc.) is wrong - fix
the caller, not this module.

Layout summary:

* Subdirectory: ``store/`` (singular).
* Filename: ``<StoreKind.value>.jsonl`` (singular: ``event``, ``audit``,
  ``decision``, ``incident``, ``estimate``, ``actual``, ``memory``,
  plus reserved ``research``, ``flow``).

The corresponding sibling lockfile is at
``<state_dir>/store/<kind>.jsonl.lock`` (handled by
:mod:`eawf.runtime.lock.sibling`).
"""

from __future__ import annotations

from pathlib import Path

from eawf.kernel.state.enums import StoreKind


def store_dir(state_path: Path) -> Path:
    """Return ``<state_dir>/store/`` (does not create the directory)."""
    return state_path.parent / "store"


def store_path(state_path: Path, kind: StoreKind) -> Path:
    """Return the JSONL path for *kind* under ``<state_dir>/store/``."""
    return store_dir(state_path) / f"{kind.value}.jsonl"


def store_paths(state_path: Path) -> dict[StoreKind, Path]:
    """Return a ``StoreKind -> Path`` map for every kind."""
    return {kind: store_path(state_path, kind) for kind in StoreKind}
