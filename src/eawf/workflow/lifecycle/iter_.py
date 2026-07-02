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
from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal

from eawf.kernel.spec.common import CriterionSpec
from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.state.enums import (
    Confidence,
    IterStatus,
    MemoryStatus,
    MemoryTier,
    PhaseStatus,
    WaveStatus,
)
from eawf.kernel.state.ids import natural_key
from eawf.kernel.state.models import Iter, State, Wave
from eawf.kernel.state.mutations import Mutation, MutationKind, apply_memory_add
from eawf.observability.metrics.odr import (
    DEFAULT_ODR_FLOOR,
    DriftPulseReport,
    WavePlanRow,
    drift_budget_pulse,
    iter_odr_advisory,
    pulse_refuses_dispatch,
)
from eawf.workflow.estimation.buckets import wave_estimate_eu
from eawf.workflow.lifecycle._errors import LifecycleError, check_title_clarity
from eawf.workflow.lifecycle.spec import ITER_TRANSITIONS, validate_transition

if TYPE_CHECKING:
    from eawf.platform.profiles.models import CheckpointBlock

logger = logging.getLogger(__name__)

#: Default drift-budget pulse cadence, mirroring the
#: :class:`~eawf.platform.profiles.models.CheckpointBlock` field defaults
#: (``checkpoint_mode="optimistic"``, ``drift_budget_waves=3``,
#: ``drift_budget_eu=3.5``). Held here so :func:`close_iter` can size a pulse
#: with no profile in hand -- the same no-profile fallback the ODR advisory
#: uses with :data:`~eawf.observability.metrics.odr.DEFAULT_ODR_FLOOR` -- and
#: the lifecycle layer never imports the profiles layer at runtime (which
#: would re-introduce the audit-DSL import cycle).
_DEFAULT_DRIFT_BUDGET_WAVES: Final[int] = 3
_DEFAULT_DRIFT_BUDGET_EU: Final[float] = 3.5
_DEFAULT_CHECKPOINT_MODE: Final[Literal["optimistic", "barrier"]] = "optimistic"


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
    check_title_clarity(title, entity_kind="iter", entity_id=iter_id)
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


def close_iter(
    state: State,
    *,
    iter_id: str,
    audit_id: str,
    checkpoint: CheckpointBlock | None = None,
    odr_floor: float = DEFAULT_ODR_FLOOR,
    odr_blocking: bool = False,
) -> Iter:
    """Close an active iter. Rejects when child waves are still open.

    Args:
        state: State to mutate in place.
        iter_id: Canonical iter id (e.g. ``P03-I02``).
        audit_id: Audit row id ratifying the close.
        checkpoint: Optional drift-cadence dial. Defaults to the
            :class:`~eawf.platform.profiles.models.CheckpointBlock` default
            (optimistic, K=3 / D=3.5) when ``None`` -- the same
            no-profile-in-hand fallback the ODR advisory uses. In ``barrier``
            mode a drift pulse that detects a thin wave refuses the close so
            the next dispatch cannot proceed against unreconciled drift; in
            ``optimistic`` mode the pulse is advisory and never stalls.
        odr_floor: The Oracle-Determinism-Ratio floor read from
            :attr:`~eawf.platform.profiles.models.VerifyBlock.odr_floor`.
            Defaults to :data:`~eawf.observability.metrics.odr.DEFAULT_ODR_FLOOR`
            -- the same no-profile-in-hand fallback the drift pulse uses.
        odr_blocking: When ``True`` a below-*odr_floor* ODR refuses the close
            (raises before any state mutation), mirroring the ``barrier``-mode
            drift pulse; when ``False`` (the default) a sub-floor ratio is
            advisory only (logged, never blocks). Read from
            :attr:`~eawf.platform.profiles.models.VerifyBlock.odr_blocking`.

    Raises:
        LifecycleError: when the iter is unknown, the close edge is illegal,
            child waves are still open, a ``barrier``-mode drift pulse detects
            criteria-vs-plan drift over the iter's closed waves, or the closed
            waves' ODR is below *odr_floor* while *odr_blocking* is set.
    """
    it = state.iters.get(iter_id)
    if it is None:
        raise LifecycleError(f"unknown iter {iter_id!r}")
    # planned/active -> closed are the only legal close edges; the table has no
    # terminal -> closed edge, so a terminal source raises the legacy message.
    validate_transition(
        ITER_TRANSITIONS,
        it.status,
        IterStatus.CLOSED,
        illegal_message=f"iter {iter_id!r} has status {it.status.value!r}; cannot close",
    )
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
    if checkpoint is not None:
        budget_waves = checkpoint.drift_budget_waves
        budget_eu = checkpoint.drift_budget_eu
        checkpoint_mode = checkpoint.checkpoint_mode
    else:
        budget_waves = _DEFAULT_DRIFT_BUDGET_WAVES
        budget_eu = _DEFAULT_DRIFT_BUDGET_EU
        checkpoint_mode = _DEFAULT_CHECKPOINT_MODE
    pulse = _emit_iter_drift_pulse(
        state,
        iter_id=iter_id,
        budget_waves=budget_waves,
        budget_eu=budget_eu,
        checkpoint_mode=checkpoint_mode,
    )
    if pulse is not None and pulse_refuses_dispatch(pulse):
        raise LifecycleError(
            f"iter {iter_id!r} drift pulse refuses next dispatch (barrier mode): "
            f"thin waves {pulse.thin_wave_ids}"
        )
    _emit_iter_odr_advisory(state, iter_id=iter_id, floor=odr_floor, blocking=odr_blocking)
    closed_at = datetime.now(UTC)
    it.status = IterStatus.CLOSED
    it.closed_at = closed_at
    it.audit_id = audit_id
    _record_iter_close_memory(state, it=it, audit_id=audit_id, closed_at=closed_at)
    if state.current.iter_id == iter_id:
        state.current.iter_id = None
        state.current.active_wave_ids = []
    logger.info(f"close_iter id={iter_id} audit={audit_id}")
    return it


def _aggregate_closed_wave_criteria(state: State, *, iter_id: str) -> list[CriterionSpec]:
    """Collect the typed success criteria of an iter's CLOSED waves.

    Walks every wave under *iter_id* whose status is CLOSED and flattens
    their :attr:`Wave.success_criteria` into one list. The ODR is computed
    over the close-gating criteria, so only CLOSED waves contribute -- an
    abandoned or never-claimed wave never gated the iter's verdict. The
    result is the input to :func:`iter_odr_advisory`.

    Args:
        state: The state holding the wave rows.
        iter_id: Canonical iter id whose closed waves are aggregated.

    Returns:
        The flattened criterion rows; an empty list when the iter has no
        closed wave with typed criteria.
    """
    criteria: list[CriterionSpec] = []
    for wave in state.waves.values():
        if wave.iter_id == iter_id and wave.status is WaveStatus.CLOSED:
            criteria.extend(wave.success_criteria)
    return criteria


def _emit_iter_odr_advisory(
    state: State,
    *,
    iter_id: str,
    floor: float = DEFAULT_ODR_FLOOR,
    blocking: bool = False,
) -> None:
    """Score the closing iter's criteria and surface an ODR finding.

    Aggregates the iter's CLOSED-wave criteria and delegates the floor
    decision to :func:`iter_odr_advisory`, which logs the WARNING line when
    the Oracle-Determinism-Ratio falls below *floor*. When a finding is
    returned this emits a structured finding line so the bit and ratio surface
    at close time, then -- only when *blocking* is set -- raises so the close
    is refused before any state mutation (mirroring the ``barrier``-mode drift
    pulse). An empty / all-deterministic criteria set takes the sentinel path
    (``iter_odr_advisory`` returns ``None``) and nothing is logged or raised;
    a sub-floor set with *blocking* false logs the advisory but never blocks.

    Args:
        state: The state holding the wave rows.
        iter_id: Canonical iter id being closed.
        floor: The advisory / blocking ODR floor to score against.
        blocking: When ``True`` a sub-floor ratio raises instead of logging
            advisory-only.

    Raises:
        LifecycleError: when the ODR is below *floor* and *blocking* is set.
    """
    criteria = _aggregate_closed_wave_criteria(state, iter_id=iter_id)
    advisory = iter_odr_advisory(criteria, scope_id=iter_id, floor=floor)
    if advisory is None:
        return
    severity = "blocking" if blocking else "advisory"
    logger.warning(
        f"emit_iter_odr_advisory iter={iter_id} odr={advisory.odr:.4f} "
        f"floor={advisory.floor:.4f} required={advisory.required} "
        f"finding=odr_below_floor severity={severity}"
    )
    if blocking:
        raise LifecycleError(
            f"iter {iter_id!r} odr {advisory.odr:.4f} below floor "
            f"{advisory.floor:.4f} and odr_blocking is set; refusing close"
        )


def _closed_wave_plan_rows(state: State, *, iter_id: str) -> list[WavePlanRow]:
    """Build the delivered-vs-planned criteria rows for an iter's closed waves.

    Walks the iter's CLOSED waves in id order and pairs, per wave, the
    criteria the planner intended (the *plan row*) against the criteria the
    executor delivered. The plan-row count is the wave's
    :attr:`eawf.kernel.spec.intent.IntentBrief.planned_steps` count when the
    wave carries a typed intent; a wave with no intent has no recorded plan,
    so its plan-row count is taken as the delivered count and the wave can
    never read as thin (the conservative reading -- a missing plan is not
    evidence of drift). The delivered count is
    ``len(wave.success_criteria)``; the per-wave EU comes from
    :func:`eawf.workflow.estimation.buckets.wave_estimate_eu` so the pulse can
    size its window in EU as well as wave count.

    Args:
        state: The state holding the wave rows.
        iter_id: Canonical iter id whose closed waves are scored.

    Returns:
        One :class:`WavePlanRow` per CLOSED wave under *iter_id*, in id order.
    """
    rows: list[WavePlanRow] = []
    closed = sorted(
        [
            wave
            for wave in state.waves.values()
            if wave.iter_id == iter_id and wave.status is WaveStatus.CLOSED
        ],
        key=lambda wave: natural_key(wave.id),
    )
    for wave in closed:
        delivered = len(wave.success_criteria)
        planned = len(wave.intent.planned_steps) if wave.intent is not None else delivered
        rows.append(
            WavePlanRow(
                wave_id=wave.id,
                planned_criteria=planned,
                delivered_criteria=delivered,
                eu=wave_estimate_eu(wave),
            )
        )
    return rows


def _emit_iter_drift_pulse(
    state: State,
    *,
    iter_id: str,
    budget_waves: int,
    budget_eu: float,
    checkpoint_mode: Literal["optimistic", "barrier"],
) -> DriftPulseReport | None:
    """Fire the drift-budget pulse over the closing iter's closed waves.

    Aggregates the iter's CLOSED-wave plan rows and delegates to
    :func:`eawf.observability.metrics.odr.drift_budget_pulse`, which fires one
    typed :class:`DriftPulseReport` once the closed-wave window reaches the
    budget (``K`` waves or ``D`` EU). Fewer than ``K`` closed waves (and below
    ``D`` EU) yields no pulse and ``None`` is returned. A clean pulse (no thin
    wave) is advisory in either cadence shape and never stalls the frontier; a
    pulse that detects criteria-vs-plan drift logs the finding and -- only in
    ``barrier`` mode -- is surfaced by :func:`close_iter` as a refused
    dispatch.

    Args:
        state: The state holding the wave rows.
        iter_id: Canonical iter id being closed.
        budget_waves: The ``K`` wave-count budget for the pulse window.
        budget_eu: The ``D`` EU budget for the pulse window.
        checkpoint_mode: The cadence shape (``optimistic`` / ``barrier``).

    Returns:
        The :class:`DriftPulseReport` when the budget boundary is reached;
        ``None`` when the budget has not yet fired.
    """
    rows = _closed_wave_plan_rows(state, iter_id=iter_id)
    pulse = drift_budget_pulse(
        rows,
        budget_waves=budget_waves,
        budget_eu=budget_eu,
        checkpoint_mode=checkpoint_mode,
    )
    if pulse is None:
        return None
    logger.info(
        f"emit_iter_drift_pulse iter={iter_id} mode={pulse.checkpoint_mode} "
        f"drift_detected={str(pulse.drift_detected).lower()} thin={pulse.thin_wave_ids}"
    )
    return pulse


def _record_iter_close_memory(
    state: State,
    *,
    it: Iter,
    audit_id: str,
    closed_at: datetime,
) -> None:
    """Mirror an iter-close summary into ``state.memory_index``."""
    wave_rows = sorted(
        [wave for wave in state.waves.values() if wave.iter_id == it.id],
        key=lambda wave: natural_key(wave.id),
    )
    if not wave_rows:
        return
    status_counts = Counter(wave.status.value for wave in wave_rows)
    status_text = ", ".join(f"{status}={count}" for status, count in sorted(status_counts.items()))
    outcome_text = _iter_close_outcome_text(wave_rows)
    summary = f"iter {it.id} closed with audit {audit_id}; waves {status_text}"
    if outcome_text:
        summary = f"{summary}; outcomes {outcome_text}"
    memory_id = _iter_close_memory_id(state, iter_id=it.id, audit_id=audit_id)
    apply_memory_add(
        state,
        Mutation(
            kind=MutationKind.MEMORY_ADD,
            scope_id=it.id,
            mutation_id=f"{memory_id}-mutation",
            params={
                "id": memory_id,
                "scope_id": it.id,
                "summary": summary,
                "confidence": Confidence.HIGH.value,
                "status": MemoryStatus.ACTIVE.value,
                "store_record_id": memory_id,
                "review_due": closed_at,
                "tier": MemoryTier.WORKING.value,
            },
        ),
    )
    logger.info(f"record_iter_close_memory iter={it.id} memory={memory_id}")


def _iter_close_memory_id(state: State, *, iter_id: str, audit_id: str) -> str:
    """Return a non-conflicting iter-close memory id."""
    base = f"MEM-{iter_id}-close-{audit_id}"
    index = state.memory_index or {}
    if base not in index:
        return base
    suffix = 2
    while f"{base}-{suffix}" in index:
        suffix += 1
    return f"{base}-{suffix}"


def _iter_close_outcome_text(wave_rows: list[Wave]) -> str:
    """Return compact wave outcome text for the iter-close memory row."""
    parts: list[str] = []
    for wave in wave_rows:
        outcome = wave.outcome
        if not outcome:
            continue
        text = " ".join(str(outcome).split())
        if len(text) > 96:
            text = f"{text[:93]}..."
        parts.append(f"{wave.id}: {text}")
        if len(parts) == 3:
            break
    return " | ".join(parts)


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
    check_title_clarity(title, entity_kind="iter", entity_id=iter_id)
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
    intent_problem = repr(intent.problem) if intent is not None else None
    logger.info(
        f"edit_iter_plan id={iter_id} title={title!r} description={description!r} "
        f"intent_problem={intent_problem}"
    )
    return it


def set_iter_candidate_tag(state: State, *, iter_id: str, tag: str) -> Iter:
    """Set an iter's proposed release tag in place. Status-agnostic.

    Stamps :attr:`Iter.candidate_tag` with a ``vMAJOR.MINOR.PATCH``
    release tag an operator pencils onto the iter ahead of the
    phase-close release pre-flight. The tag is routed through the model's
    assignment validator so the ``ReleaseStr`` pattern is re-checked; an
    invalid tag raises :class:`pydantic.ValidationError`. The set is
    purely cosmetic metadata -- no lifecycle transition fires -- so the
    helper deliberately does NOT gate on iter status (PLANNED, ACTIVE, and
    CLOSED iters can all be tagged), mirroring :func:`edit_iter_plan`.

    Args:
        state: State to mutate in place.
        iter_id: Canonical iter id (e.g. ``P03-I02``).
        tag: Proposed release tag (``vMAJOR.MINOR.PATCH``, e.g.
            ``v0.5.0``).

    Returns:
        The mutated :class:`Iter`.

    Raises:
        LifecycleError: when *iter_id* is unknown.
        pydantic.ValidationError: when *tag* does not match the
            ``ReleaseStr`` pattern.
    """
    it = state.iters.get(iter_id)
    if it is None:
        raise LifecycleError(f"unknown iter: {iter_id!r}")
    it.__pydantic_validator__.validate_assignment(it, "candidate_tag", tag)
    logger.info(f"set_iter_candidate_tag id={iter_id} tag={tag!r}")
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
    # planned -> active is the only legal activate edge; the table has no
    # active/terminal -> active edge, so a non-planned source raises the
    # legacy "only planned iters can activate" message.
    validate_transition(
        ITER_TRANSITIONS,
        it.status,
        IterStatus.ACTIVE,
        illegal_message=(
            f"iter {iter_id!r} has status {it.status.value!r}; only planned iters can activate"
        ),
    )
    it.status = IterStatus.ACTIVE
    state.current.phase_id = it.phase_id
    state.current.iter_id = iter_id
    state.current.active_wave_ids = []
    logger.info(f"activate_iter id={iter_id} phase={it.phase_id}")
    return it
