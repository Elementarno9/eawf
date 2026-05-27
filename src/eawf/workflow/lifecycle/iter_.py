"""Pure-functional iter lifecycle transitions.

Open / close / plan / activate for :class:`Iter`. Every helper mutates the
supplied :class:`State` in place. See :mod:`eawf.workflow.lifecycle.transitions` for
the shared design rules and the re-export surface that keeps
``eawf.workflow.lifecycle.transitions`` import paths working after the per-entity
split.

The module is named ``iter_`` (trailing underscore) to avoid shadowing the
``iter`` builtin; it is reached only through the
:mod:`eawf.workflow.lifecycle.transitions` re-export, never imported by module path.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.state.enums import IterStatus, PhaseStatus, WaveStatus
from eawf.kernel.state.ids import natural_key
from eawf.kernel.state.models import Iter, State
from eawf.workflow.lifecycle._errors import LifecycleError

logger = logging.getLogger(__name__)


def open_iter(
    state: State,
    *,
    iter_id: str,
    phase_id: str,
    title: str,
    description: str | None = None,
    intent: IntentBrief | None = None,
) -> Iter:
    """Insert a new iter under *phase_id* with status ``active``.

    Args:
        state: State to mutate in place.
        iter_id: Canonical iter id (e.g. ``P03-I02``).
        phase_id: Parent phase id (must exist and be open).
        title: Bounded ≤72-char iter title.
        description: Optional bounded ≤500-char long-form description;
            persisted on :attr:`Iter.description` for downstream renderers.
        intent: Optional typed :class:`IntentBrief`; persisted on
            :attr:`Iter.intent` for downstream renderers. Additive +
            replay-safe so on-disk iter rows without it re-validate.

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
        description=description,
        status=IterStatus.ACTIVE,
        wave_ids=[],
        estimate_id=None,
        audit_id=None,
        opened_at=datetime.now(UTC),
        closed_at=None,
        intent=intent,
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
        raise LifecycleError(
            f"iter {iter_id!r} has open waves: {sorted(open_waves, key=natural_key)}"
        )
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
    description: str | None = None,
    intent: IntentBrief | None = None,
) -> Iter:
    """Insert a new iter under *phase_id* with status ``planned``.

    Companion of :func:`eawf.workflow.lifecycle.phase.plan_phase`. The iter sits in
    PLANNED until :func:`activate_iter` (or
    :func:`eawf.workflow.lifecycle.phase.activate_phase` when the parent activates
    and the iter is the sole open child).

    Args:
        state: State to mutate in place.
        iter_id: Canonical iter id (e.g. ``P03-I02``).
        phase_id: Parent phase id (must exist and be PLANNED or ACTIVE).
        title: Bounded ≤72-char iter title.
        description: Optional bounded ≤500-char long-form description;
            persisted on :attr:`Iter.description` for downstream renderers.

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
        description=description,
        status=IterStatus.PLANNED,
        wave_ids=[],
        estimate_id=None,
        audit_id=None,
        opened_at=datetime.now(UTC),
        closed_at=None,
        intent=intent,
    )
    state.iters[iter_id] = it
    if iter_id not in phase.iter_ids:
        phase.iter_ids.append(iter_id)
    logger.info(f"plan_iter id={iter_id} phase={phase_id} title={title!r}")
    return it


def edit_iter_plan(
    state: State,
    *,
    iter_id: str,
    title: str | None = None,
    description: str | None = None,
    intent: IntentBrief | None = None,
) -> Iter:
    """Rewrite an iter's ``title`` / ``description`` in place. Status-agnostic.

    Retitle / re-describe is purely cosmetic metadata: the iter id is
    preserved and no lifecycle transition fires, so this helper deliberately
    does NOT gate on iter status — PLANNED, ACTIVE, and CLOSED iters can all
    be renormalised. Each supplied field is routed through the model's
    assignment validator so the title 1-72 character bound and the
    description ≤500-character bound are re-checked; an over-cap value
    raises :class:`pydantic.ValidationError`. Pass ``None`` to leave a
    field untouched.

    Args:
        state: State to mutate in place.
        iter_id: Canonical iter id (e.g. ``P03-I02``).
        title: Optional replacement title; ``None`` leaves it untouched.
        description: Optional replacement description (≤500 chars);
            ``None`` leaves the existing value untouched. The model
            already permits ``None`` as the "no description" sentinel,
            so callers cannot clear a description through this helper —
            that intentional asymmetry mirrors the wave / phase API.

    Raises:
        LifecycleError: when *iter_id* is unknown.
        pydantic.ValidationError: when *title* / *description* violates
            its bound.
    """
    it = state.iters.get(iter_id)
    if it is None:
        raise LifecycleError(f"unknown iter: {iter_id!r}")
    if title is not None:
        it.__pydantic_validator__.validate_assignment(it, "title", title)
    if description is not None:
        it.__pydantic_validator__.validate_assignment(it, "description", description)
    if intent is not None:
        it.__pydantic_validator__.validate_assignment(it, "intent", intent)
    intent_goal = repr(intent.goal) if intent is not None else None
    logger.info(
        f"edit_iter_plan id={iter_id} title={title!r} description={description!r} "
        f"intent_goal={intent_goal}"
    )
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
