"""Verdict-to-outcome projection (P29-I05-W01).

The reputation/Brier scorer (a later wave) cannot score a per-wave verdict
until that verdict has a *realized outcome* to score against: a verdict is a
prediction ("this wave is done / correct"), and the outcome is whether that
prediction was borne out. This module builds the missing data substrate -- a
:class:`VerdictOutcome` per per-wave verdict row, each carrying whether the
verdict ``held`` and the state-observable signal that settled it.

The per-wave verdict producer (:mod:`eawf.workflow.dispatch.verdict`) writes a
fresh-context AUDITOR report at ``base_id=wave_id`` for every wave it judges.
:func:`build_verdict_outcomes` joins those AUDITOR rows back to the wave
(``wave.id == AgentReportHeader.base_id``, the same join key the phase-retro
digest uses) and observes the wave's realized outcome **from state alone**:

- a *reopen* of the wave's phase (CLOSED -> ACTIVE) refutes the verdict;
- a strictly-later ``reactive`` iter under the same phase (repair / mid-flight
  scope add) refutes it;
- otherwise, once the wave and its iter are CLOSED, the verdict held clean.

Honest-negative by construction. The per-wave report store is empty today
(zero AUDITOR rows on disk), so :func:`build_verdict_outcomes` returns ``[]``
right now -- and that empty list IS the deliverable. The projection exists so
it consumes verdict rows the moment live dispatch starts accruing them; it
never fabricates an outcome or invents a fallback. This mirrors the
refuse-to-score posture of :mod:`eawf.observability.eval.self_eval`.

The reducer is pure: no mutation, no git, no daemon. Fix-commit attribution
(scanning ``git log`` for a repair commit that names the wave) is a deferred
follow-up -- a pure state reducer does not shell out to git, so the
``"fix_commit"`` outcome source named in the reputation-engine design is left
for a later wave that has a git surface.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    Confidence,
    IterStatus,
    IterTrigger,
    PhaseStatus,
    WaveStatus,
)
from eawf.kernel.state.ids import natural_key
from eawf.kernel.state.models import Iter, State, Wave
from eawf.workflow.agent_report.rollup import iter_agent_reports

logger = logging.getLogger(__name__)

#: The report role whose rows are per-wave verdicts. The verdict producer
#: (:mod:`eawf.workflow.dispatch.verdict`) writes a fresh-context AUDITOR
#: report at ``base_id=wave_id`` for every wave it judges, so the outcome loop
#: reads exactly the AUDITOR cohort.
_VERDICT_ROLE: AgentSessionRole = AgentSessionRole.AUDITOR

#: Confidence-enum -> probability mapping (ratified A1 mapping). The
#: reputation/Brier scorer needs a numeric prediction to score; the report
#: body only carries a coarse :class:`~eawf.kernel.state.enums.Confidence`
#: bucket, so this is the single canonical translation from bucket to ``p``.
_CONFIDENCE_TO_FLOAT: dict[Confidence, float] = {
    Confidence.HIGH: 0.9,
    Confidence.MEDIUM: 0.7,
    Confidence.LOW: 0.55,
}

#: Outcome-source label set this reducer can emit. ``"fix_commit"`` (from the
#: design) is intentionally absent: attributing a repair to a git commit needs
#: a git surface the pure reducer does not have, so it is deferred.
_CLEAN = "clean"
_REOPEN = "reopen"
_REACTIVE = "reactive"


class VerdictOutcome(BaseModel):
    """One per-wave verdict joined to its realized, state-observable outcome.

    The reputation/Brier scorer reads a stream of these: each row pairs a
    verdict (the prediction, with a numeric :attr:`confidence`) with whether
    the prediction was borne out (:attr:`held`) and the signal that settled it
    (:attr:`outcome_source`). ``extra="forbid"`` so a drifted field surfaces as
    a :class:`pydantic.ValidationError` at construction rather than silently
    skewing a downstream score.

    The tri-state :attr:`held` keeps the not-yet-observable case honest: a wave
    still in flight has no settled outcome, so its verdict's ``held`` is
    ``None`` (and :attr:`outcome_source` is ``None``) rather than a guessed
    ``True`` / ``False`` -- the scorer skips it instead of scoring a fabricated
    outcome.

    Attributes:
        base_id: The wave id the verdict was about (the report ``base_id``).
        agent_role: Role of the agent that authored the verdict (the per-wave
            verdict producer writes AUDITOR rows).
        runtime: Runtime adapter id that produced the verdict report.
        verdict: The recorded :class:`~eawf.kernel.state.enums.AgentReportVerdict`.
        confidence: Numeric prediction in ``[0.0, 1.0]`` mapped from the
            report's :class:`~eawf.kernel.state.enums.Confidence` bucket via
            :func:`confidence_to_float`.
        held: ``True`` when the verdict was borne out (no repair / reopen /
            reactive iter followed), ``False`` when it was refuted, ``None``
            when the outcome is not yet observable.
        outcome_source: The state signal that settled the outcome -- one of
            ``"clean"`` / ``"reopen"`` / ``"reactive"`` -- or ``None`` when
            the outcome is not yet observable. (The design also names
            ``"fix_commit"``; git-log attribution is a deferred follow-up and
            is never emitted by this pure reducer.)
    """

    model_config = ConfigDict(extra="forbid")

    base_id: str
    agent_role: AgentSessionRole
    runtime: str
    verdict: AgentReportVerdict
    confidence: float = Field(ge=0.0, le=1.0)
    held: bool | None = None
    outcome_source: str | None = None


def confidence_to_float(confidence: Confidence) -> float:
    """Map a report confidence bucket to its ratified probability.

    The reputation/Brier scorer needs a numeric prediction, but the report
    body only records a coarse bucket. This is the single canonical
    translation: ``high -> 0.9``, ``medium -> 0.7``, ``low -> 0.55``.

    Args:
        confidence: The report's confidence bucket.

    Returns:
        The probability in ``[0.0, 1.0]`` for *confidence*.

    Raises:
        KeyError: When *confidence* is not a known
            :class:`~eawf.kernel.state.enums.Confidence` member (cannot happen
            for a valid enum value).
    """
    return _CONFIDENCE_TO_FLOAT[confidence]


def _iter_is_settled(iter_row: Iter | None) -> bool:
    """Return whether *iter_row* has reached a terminal (settled) status.

    A wave's outcome is only observable once its iter is settled -- an
    ACTIVE / PLANNED iter is still in flight, so any "clean" reading would be
    premature. ``None`` (no iter row for the wave) counts as unsettled: there
    is nothing to anchor an outcome on.
    """
    if iter_row is None:
        return False
    return iter_row.status in {IterStatus.CLOSED, IterStatus.ABANDONED}


def _phase_was_reopened(state: State, *, phase_id: str | None) -> bool:
    """Return whether *phase_id* shows the state-observable reopen tell.

    ``reopen_phase`` flips a phase CLOSED -> ACTIVE and clears ``closed_at``,
    but preserves ``audit_id`` so the original close evidence stays
    reconstructible. So the only reopen signal surviving in state alone is an
    ACTIVE phase that already carries a close ``audit_id`` -- a phase that was
    closed once (earning an audit) and is active again. A never-closed ACTIVE
    phase has ``audit_id is None`` and is not a reopen.
    """
    if phase_id is None:
        return False
    phase = state.phases.get(phase_id)
    if phase is None:
        return False
    return phase.status is PhaseStatus.ACTIVE and phase.audit_id is not None


def _has_later_reactive_iter(state: State, *, phase_id: str | None, wave_iter_id: str) -> bool:
    """Return whether a strictly-later ``reactive`` iter repairs the wave.

    A repair / mid-flight scope add opens a later iter under the same phase
    tagged :attr:`~eawf.kernel.state.enums.IterTrigger.REACTIVE`. "Later" is
    the natural-key ordering of the iter id, so ``P01-I02`` is later than
    ``P01-I01``. The presence of any such iter refutes the wave's verdict --
    the wave's scope needed follow-up work.
    """
    if phase_id is None:
        return False
    wave_key = natural_key(wave_iter_id)
    for iter_row in state.iters.values():
        if iter_row.phase_id != phase_id:
            continue
        if iter_row.trigger is not IterTrigger.REACTIVE:
            continue
        if natural_key(iter_row.id) > wave_key:
            return True
    return False


def _observe_outcome(state: State, wave: Wave) -> tuple[bool | None, str | None]:
    """Observe one wave's realized outcome from state alone.

    Returns ``(held, outcome_source)`` per the state-observable signals:

    - ``(False, "reopen")`` when the wave's phase was reopened after close;
    - ``(False, "reactive")`` when a strictly-later reactive iter repairs it;
    - ``(True, "clean")`` when the wave and its iter are settled with neither
      repair signal present;
    - ``(None, None)`` when the outcome is not yet observable (the wave or its
      iter is still open).

    The reopen / reactive refutations are checked before the settled gate so a
    refuting signal still registers even on an in-flight (reopened) phase.
    """
    iter_row = state.iters.get(wave.iter_id)
    phase_id = iter_row.phase_id if iter_row is not None else None

    if _phase_was_reopened(state, phase_id=phase_id):
        return False, _REOPEN
    if _has_later_reactive_iter(state, phase_id=phase_id, wave_iter_id=wave.iter_id):
        return False, _REACTIVE
    if wave.status is WaveStatus.CLOSED and _iter_is_settled(iter_row):
        return True, _CLEAN
    return None, None


def build_verdict_outcomes(
    state: State,
    state_path: Path,
    *,
    iter_id: str | None = None,
) -> list[VerdictOutcome]:
    """Project per-wave verdicts into realized outcomes -- the outcome loop.

    A pure reducer: it reads the AUDITOR per-wave verdict rows off disk (via
    :func:`eawf.workflow.agent_report.rollup.iter_agent_reports`), joins each
    back to its wave (``wave.id == base_id``), and observes the wave's realized
    outcome from *state* alone (see :func:`_observe_outcome`). No mutation, no
    git, no daemon.

    Honest-empty: the per-wave report store is empty today, so this returns
    ``[]`` -- the correct result, not a bug. The projection never fabricates an
    outcome; it simply has no verdict rows to project yet.

    Args:
        state: Loaded, validated :class:`~eawf.kernel.state.models.State`
            supplying the phase / iter / wave tree the outcomes are observed
            against.
        state_path: Path to ``state.json``; the AUDITOR report store resolves
            under its sibling ``store/`` directory.
        iter_id: Optional filter -- restrict the projection to verdicts whose
            wave belongs to this iter. ``None`` projects every wave with a
            verdict row.

    Returns:
        One :class:`VerdictOutcome` per AUDITOR verdict row whose wave is known
        to *state* (and, when *iter_id* is given, belongs to that iter),
        ordered by ``(created_at, report id)``. Verdict rows whose ``base_id``
        names no wave in *state* are skipped -- there is no wave to observe an
        outcome against.
    """
    rows = iter_agent_reports(state_path, role=_VERDICT_ROLE)
    outcomes: list[VerdictOutcome] = []
    for row in rows:
        base_id = row.payload.header.base_id
        wave = state.waves.get(base_id)
        if wave is None:
            continue
        if iter_id is not None and wave.iter_id != iter_id:
            continue
        held, outcome_source = _observe_outcome(state, wave)
        outcomes.append(
            VerdictOutcome(
                base_id=base_id,
                agent_role=row.payload.header.role,
                runtime=row.payload.header.runtime,
                verdict=row.payload.body.verdict,
                confidence=confidence_to_float(row.payload.body.confidence),
                held=held,
                outcome_source=outcome_source,
            )
        )
    logger.debug(
        f"build_verdict_outcomes rows={len(rows)} outcomes={len(outcomes)} iter={iter_id!r}"
    )
    return outcomes


__all__ = [
    "VerdictOutcome",
    "build_verdict_outcomes",
    "confidence_to_float",
]
