"""Pure-functional iter lifecycle transitions.

Open / close / plan / activate for :class:`Iter`. Every helper mutates the
supplied :class:`State` in place. See :mod:`eawf.lifecycle.transitions` for
the shared design rules and the re-export surface that keeps
``eawf.lifecycle.transitions`` import paths working after the per-entity
split.

The module is named ``iter_`` (trailing underscore) to avoid shadowing the
``iter`` builtin; it is reached only through the
:mod:`eawf.lifecycle.transitions` re-export, never imported by module path.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from eawf.lifecycle._errors import LifecycleError
from eawf.state.enums import IterStatus, PhaseStatus, WaveStatus
from eawf.state.models import Iter, State

logger = logging.getLogger(__name__)


def open_iter(
    state: State,
    *,
    iter_id: str,
    phase_id: str,
    title: str,
) -> Iter:
    """Insert a new iter under *phase_id* with status ``active``.

    Raises:
        LifecycleError: if the phase is missing or not open, or if *iter_id*
            already exists.
    """
    phase = state.phases.get(phase_id)
    if phase is None:
        raise LifecycleError(f"unknown phase {phase_id!r}")
    if phase.status not in {PhaseStatus.PLANNED, PhaseStatus.ACTIVE}:
        raise LifecycleError(f"phase {phase_id!r} is not open (status={phase.status.value!r})")
    if iter_id in state.iters:
        raise LifecycleError(f"iter {iter_id!r} already exists")
    it = Iter(
        id=iter_id,
        phase_id=phase_id,
        title=title,
        status=IterStatus.ACTIVE,
        wave_ids=[],
        estimate_id=None,
        audit_id=None,
        opened_at=datetime.now(UTC),
        closed_at=None,
    )
    state.iters[iter_id] = it
    if iter_id not in phase.iter_ids:
        phase.iter_ids.append(iter_id)
    state.current.phase_id = phase_id
    state.current.iter_id = iter_id
    state.current.active_wave_ids = []
    logger.info(f"open_iter id={iter_id} phase={phase_id} title={title!r}")
    return it


def close_iter(state: State, *, iter_id: str, audit_id: str) -> Iter:
    """Close an active iter. Rejects when child waves are still open."""
    it = state.iters.get(iter_id)
    if it is None:
        raise LifecycleError(f"unknown iter {iter_id!r}")
    if it.status not in {IterStatus.PLANNED, IterStatus.ACTIVE}:
        raise LifecycleError(f"iter {iter_id!r} has status {it.status.value!r}; cannot close")
    open_waves = [
        wid
        for wid, w in state.waves.items()
        if w.iter_id == iter_id
        and w.status in {WaveStatus.PENDING, WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}
    ]
    if open_waves:
        raise LifecycleError(f"iter {iter_id!r} has open waves: {sorted(open_waves)}")
    it.status = IterStatus.CLOSED
    it.closed_at = datetime.now(UTC)
    it.audit_id = audit_id
    if state.current.iter_id == iter_id:
        state.current.iter_id = None
        state.current.active_wave_ids = []
    logger.info(f"close_iter id={iter_id} audit={audit_id}")
    return it


def plan_iter(
    state: State,
    *,
    iter_id: str,
    phase_id: str,
    title: str,
) -> Iter:
    """Insert a new iter under *phase_id* with status ``planned``.

    Companion of :func:`eawf.lifecycle.phase.plan_phase`. The iter sits in
    PLANNED until :func:`activate_iter` (or
    :func:`eawf.lifecycle.phase.activate_phase` when the parent activates
    and the iter is the sole open child).

    Raises:
        LifecycleError: when the phase is missing, the phase is not
            PLANNED or ACTIVE, or *iter_id* already exists.
    """
    phase = state.phases.get(phase_id)
    if phase is None:
        raise LifecycleError(f"unknown phase {phase_id!r}")
    if phase.status not in {PhaseStatus.PLANNED, PhaseStatus.ACTIVE}:
        raise LifecycleError(f"phase {phase_id!r} is not open (status={phase.status.value!r})")
    if iter_id in state.iters:
        raise LifecycleError(f"iter {iter_id!r} already exists")
    it = Iter(
        id=iter_id,
        phase_id=phase_id,
        title=title,
        status=IterStatus.PLANNED,
        wave_ids=[],
        estimate_id=None,
        audit_id=None,
        opened_at=datetime.now(UTC),
        closed_at=None,
    )
    state.iters[iter_id] = it
    if iter_id not in phase.iter_ids:
        phase.iter_ids.append(iter_id)
    logger.info(f"plan_iter id={iter_id} phase={phase_id} title={title!r}")
    return it


def activate_iter(state: State, *, iter_id: str) -> Iter:
    """Flip a planned iter to active.

    Updates ``current.iter_id``. The parent phase must already be ACTIVE
    (or PLANNED but in the middle of a coordinated activate sequence —
    callers responsible for ordering).

    Raises:
        LifecycleError: when *iter_id* is unknown or not in PLANNED state.
    """
    it = state.iters.get(iter_id)
    if it is None:
        raise LifecycleError(f"unknown iter {iter_id!r}")
    if it.status != IterStatus.PLANNED:
        raise LifecycleError(
            f"iter {iter_id!r} has status {it.status.value!r}; only planned iters can activate"
        )
    it.status = IterStatus.ACTIVE
    state.current.phase_id = it.phase_id
    state.current.iter_id = iter_id
    state.current.active_wave_ids = []
    logger.info(f"activate_iter id={iter_id} phase={it.phase_id}")
    return it
