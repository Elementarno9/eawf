"""Auto-allocate zero-padded lifecycle IDs over a typed :class:`State`.

The CLI exposes ``--auto`` on ``phase open``/``iter open`` and a default-allocate
path for ``wave plan`` when no explicit wave id collides; in each case we want
the smallest free zero-padded suffix among the existing entries. The
underlying counter helpers live in :mod:`eawf.state.ids`; this module is the
state-aware adapter that pulls the existing key set and delegates.

All three allocators are pure: they read ``state`` and return a string. The
caller mounts the allocation result back onto a new :class:`Phase`/:class:`Iter`
/:class:`Wave` record.
"""

from __future__ import annotations

import logging

from eawf.state.ids import (
    allocate_next_iter_id,
    allocate_next_phase_id,
    allocate_next_wave_id,
    is_iter_id,
    is_phase_id,
)
from eawf.state.models import State

logger = logging.getLogger(__name__)


def allocate_phase_id(state: State) -> str:
    """Return the smallest free phase id (e.g. ``P01``).

    The candidate set is ``state.phases.keys()``; the helper returns the
    smallest two-digit-padded suffix not already present. Raises
    :class:`ValueError` when all 99 suffixes are taken.
    """
    existing = set(state.phases.keys())
    pid = allocate_next_phase_id(existing)
    logger.debug(f"allocate_phase_id existing={len(existing)} → {pid}")
    return pid


def allocate_iter_id(state: State, phase_id: str) -> str:
    """Return the smallest free iter id under *phase_id* (e.g. ``P01-I01``).

    Raises :class:`ValueError` when ``phase_id`` is not a valid phase id or
    when all 99 iter suffixes are taken.
    """
    if not is_phase_id(phase_id):
        raise ValueError(f"invalid phase id: {phase_id!r}")
    existing = set(state.iters.keys())
    iid = allocate_next_iter_id(phase_id, existing)
    logger.debug(f"allocate_iter_id phase={phase_id} → {iid}")
    return iid


def allocate_wave_id(state: State, iter_id: str) -> str:
    """Return the smallest free wave id under *iter_id* (e.g. ``P01-I01-W01``).

    Raises :class:`ValueError` when ``iter_id`` is not a valid iter id or
    when all 99 wave suffixes are taken.
    """
    if not is_iter_id(iter_id):
        raise ValueError(f"invalid iter id: {iter_id!r}")
    existing = set(state.waves.keys())
    wid = allocate_next_wave_id(iter_id, existing)
    logger.debug(f"allocate_wave_id iter={iter_id} → {wid}")
    return wid
