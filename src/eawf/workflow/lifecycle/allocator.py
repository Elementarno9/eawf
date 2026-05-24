"""Auto-allocate zero-padded lifecycle IDs over a typed :class:`State`.

The CLI exposes ``--auto`` on ``phase open``/``iter open`` and a default-allocate
path for ``wave plan`` when no explicit wave id collides; in each case we want
the smallest free zero-padded suffix among the existing entries. The
underlying counter helpers live in :mod:`eawf.kernel.state.ids`; this module is the
state-aware adapter that pulls the existing key set and delegates.

All three allocators are pure: they read ``state`` and return a string. The
caller mounts the allocation result back onto a new :class:`Phase`/:class:`Iter`
/:class:`Wave` record.
"""

from __future__ import annotations

import logging

from eawf.kernel.state.ids import (
    allocate_next_iter_id,
    allocate_next_phase_id,
    allocate_next_wave_id,
)
from eawf.kernel.state.models import State

logger = logging.getLogger(__name__)


def allocate_phase_id(state: State) -> str:
    """Return the smallest free phase id (e.g. ``P01``).

    The candidate set is ``state.phases.keys()``; the helper returns the
    smallest two-digit-padded suffix not already present.

    Raises:
        ValueError: When all 99 suffixes are taken.
    """
    existing = set(state.phases.keys())
    pid = allocate_next_phase_id(existing)
    logger.debug(f"allocate_phase_id existing={len(existing)} allocated={pid}")
    return pid


def allocate_iter_id(state: State, phase_id: str) -> str:
    """Return the smallest free iter id under *phase_id* (e.g. ``P01-I01``).

    Raises:
        ValueError: When ``phase_id`` is not a valid phase id or when all
            99 iter suffixes are taken. The phase-id check is delegated
            to :func:`eawf.kernel.state.ids.allocate_next_iter_id`.
    """
    existing = set(state.iters.keys())
    iid = allocate_next_iter_id(phase_id, existing)
    logger.debug(f"allocate_iter_id phase={phase_id} allocated={iid}")
    return iid


def allocate_wave_id(state: State, iter_id: str) -> str:
    """Return the smallest free wave id under *iter_id* (e.g. ``P01-I01-W01``).

    Raises:
        ValueError: When ``iter_id`` is not a valid iter id or when all
            99 wave suffixes are taken. The iter-id check is delegated
            to :func:`eawf.kernel.state.ids.allocate_next_wave_id`.
    """
    existing = set(state.waves.keys())
    wid = allocate_next_wave_id(iter_id, existing)
    logger.debug(f"allocate_wave_id iter={iter_id} allocated={wid}")
    return wid


_GRANT_ID_PREFIX = "GRANT-"


def allocate_grant_id(state: State) -> str:
    """Return the smallest free MCP grant id (``GRANT-<n>``).

    The candidate set is ``state.mcp_grants`` keys; the helper returns
    ``GRANT-<max+1>``, treating any non-numeric suffix as ignorable so a
    custom ``--grant-id`` override never blocks auto-allocation.
    """
    pool = state.mcp_grants or {}
    next_n = 1
    for existing_id in pool:
        if not existing_id.startswith(_GRANT_ID_PREFIX):
            continue
        try:
            n = int(existing_id.removeprefix(_GRANT_ID_PREFIX))
        except ValueError:
            continue
        next_n = max(next_n, n + 1)
    gid = f"{_GRANT_ID_PREFIX}{next_n}"
    logger.debug(f"allocate_grant_id existing={len(pool)} allocated={gid}")
    return gid
