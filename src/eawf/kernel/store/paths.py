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

The epoch-2 tiers are siblings of ``store/`` rather than members of it::

    <state_dir>/state.json                        document
    <state_dir>/ledger/<collection>.jsonl         ledger
    <state_dir>/indexes/<collection>.index.json   derived
    <state_dir>/local/<collection>                local store
    <state_dir>/store/event.jsonl                 firehose

Only the two tiers this module is asked to locate today -- the ledger and
its derived index -- have resolvers; the local store gains one when
something writes it.

``ledger/`` is a separate directory because the epoch-1 compactor
rewrites a whole file in place, and an append-only ledger must be
unreachable to it: the directory is the fence
(:func:`is_epoch2_ledger_path`). The firehose keeps its epoch-1 location
because it is not a ledger and never compacts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.tiers import Epoch2Collection, StorageTier, tier_for

#: The reserved directory name that fences append-only ledgers off from
#: any writer that rewrites a file in place.
LEDGER_DIRNAME: Final = "ledger"

#: The reserved directory name for regenerable projections.
INDEX_DIRNAME: Final = "indexes"


def store_dir(state_path: Path) -> Path:
    """Return ``<state_dir>/store/`` (does not create the directory)."""
    return state_path.parent / "store"


def store_path(state_path: Path, kind: StoreKind) -> Path:
    """Return the JSONL path for *kind* under ``<state_dir>/store/``."""
    return store_dir(state_path) / f"{kind.value}.jsonl"


def store_paths(state_path: Path) -> dict[StoreKind, Path]:
    """Return a ``StoreKind -> Path`` map for every kind."""
    return {kind: store_path(state_path, kind) for kind in StoreKind}


def ledger_dir(state_path: Path) -> Path:
    """Return ``<state_dir>/ledger/`` (does not create the directory)."""
    return state_path.parent / LEDGER_DIRNAME


def ledger_path(state_path: Path, collection: Epoch2Collection) -> Path:
    """Return the append-only JSONL path for *collection*.

    Args:
        state_path: Path to the tree's ``state.json``.
        collection: The collection to locate.

    Returns:
        ``<state_dir>/ledger/<collection>.jsonl``.

    Raises:
        ValueError: The collection is not declared at the ledger tier, so
            it has no append-only file.
    """
    if tier_for(collection) is not StorageTier.LEDGER:
        raise ValueError(
            f"{collection.value!r} is declared at the {tier_for(collection).value} tier, not ledger"
        )
    return ledger_dir(state_path) / f"{collection.value}.jsonl"


def index_dir(state_path: Path) -> Path:
    """Return ``<state_dir>/indexes/`` (does not create the directory)."""
    return state_path.parent / INDEX_DIRNAME


def index_path(state_path: Path, collection: Epoch2Collection) -> Path:
    """Return the derived offset-index path for a ledger *collection*.

    Args:
        state_path: Path to the tree's ``state.json``.
        collection: The ledger collection the index is taken over.

    Returns:
        ``<state_dir>/indexes/<collection>.index.json``.

    Raises:
        ValueError: The collection is not declared at the ledger tier, so
            there are no ledger offsets to index.
    """
    if tier_for(collection) is not StorageTier.LEDGER:
        raise ValueError(
            f"{collection.value!r} is declared at the "
            f"{tier_for(collection).value} tier, so it has no offset index"
        )
    return index_dir(state_path) / f"{collection.value}.index.json"


def is_epoch2_ledger_path(path: Path) -> bool:
    """Report whether *path* is inside the reserved append-only directory.

    Args:
        path: The file a writer is about to open.

    Returns:
        ``True`` when the file sits directly under a ``ledger/``
        directory, which no in-place rewriter may touch.
    """
    return path.parent.name == LEDGER_DIRNAME
