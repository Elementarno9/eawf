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
import math
from collections import defaultdict
from enum import StrEnum
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


# --- reliability scoring layer (P29-I05-W02) ------------------------------
#
# The scoring layer turns a stream of observed :class:`VerdictOutcome` rows
# into one conservative reliability estimate per ``(agent_role, runtime)``.
# Three numbers per role-runtime group:
#
# - a Brier score (with its Murphy reliability / resolution components) that
#   measures how well the verdict's numeric confidence tracked the realized
#   outcome;
# - a Wilson / Beta-posterior LOWER bound on the held-rate (the conservative
#   DISPLAY score -- it never overstates a role that got lucky on a tiny
#   sample); and
# - an optimistic upper-bound ``routing_score`` (a Thompson-style seed) kept
#   SEPARATE from the display LB so exploration of under-observed roles can
#   prefer them without the display tier rewarding small-N luck.
#
# Honest-negative by construction: below ``min_n`` observed outcomes, OR while
# the posterior credible interval is wider than ``ci_width_gate``, the group
# refuses to score -- ``status = INSUFFICIENT`` and every numeric field stays
# ``None``. The substrate is empty today, so :func:`compute_role_reliability`
# returns ``[]`` right now; the scorer lights up the instant real verdict rows
# accrue. This mirrors the refuse-to-score posture of
# :mod:`eawf.observability.eval.self_eval`.
#
# Closed-form only: no scipy / numpy. The Beta posterior mean + variance and
# the normal-approximation credible interval come from the ``math`` stdlib.

#: Number of equal-width bins the Murphy decomposition partitions forecast
#: probabilities into. The decomposition only needs enough bins to separate
#: the coarse confidence buckets (0.55 / 0.7 / 0.9); ten keeps each bucket in
#: its own bin without splitting a single forecast value across two.
_MURPHY_BINS: int = 10

#: Pseudo-count (concentration) of the empirical-Bayes Beta prior. The
#: sibling held-rate is folded in as ``_PRIOR_STRENGTH`` synthetic trials, so
#: a group with few observed outcomes is shrunk hard toward its sibling prior
#: and only earns its own held-rate as real trials accrue.
_PRIOR_STRENGTH: float = 2.0

#: Held-rate assumed for a role with no sibling-prior entry. A neutral 0.5
#: prior neither rewards nor penalises an unseen role before evidence arrives.
_DEFAULT_PRIOR: float = 0.5

#: z multiplier for the normal-approximation credible interval (~95%, the
#: 0.975 standard-normal quantile). Used for both the conservative lower bound
#: (display) and the optimistic upper bound (routing).
_Z_95: float = 1.959963984540054


class ReliabilityStatus(StrEnum):
    """Whether a role-runtime group produced a reliability score or refused.

    :attr:`INSUFFICIENT` is the honest-negative surface: the group is below
    the min-N floor or its posterior credible interval is too wide, so every
    numeric field on the :class:`RoleReliability` projection stays ``None``.
    :attr:`SCORED` means the group cleared both gates and its numbers are real.
    """

    INSUFFICIENT = "insufficient"
    SCORED = "scored"


class ReputationConfig(BaseModel):
    """The ``trust.*`` config leaf governing the reliability scorer.

    ``extra="forbid"`` so a drifted config key surfaces as a
    :class:`pydantic.ValidationError` at load rather than silently changing a
    gate.

    Attributes:
        min_n: Honesty-gate floor -- a role-runtime group with fewer observed
            outcomes refuses to score (``>= 1``; a zero floor would defeat the
            refuse-to-score guarantee).
        ci_width_gate: Suppress the estimate while the posterior credible
            interval is wider than this. A wide interval means the held-rate
            is not yet pinned down, so the display LB would be noise.
        tier_thresholds: Display-LB -> tier label ("C" / "B" / "A") mapping.
            Held here for the NEXT wave (W03) that adds the tier enum + the
            ``RoleReliability.tier`` field; this wave only carries the field.
        loss_weight: Bounded (~3x) asymmetric demote weight used by the next
            wave's tier transitions -- a demotion costs more than a promotion
            so a regressed role drops fast. Held here, unused this wave.
    """

    model_config = ConfigDict(extra="forbid")

    min_n: int = Field(default=20, ge=1)
    ci_width_gate: float = Field(default=0.3)
    tier_thresholds: dict[str, float] = Field(default_factory=dict)
    loss_weight: float = Field(default=3.0, ge=1.0, le=5.0)


class RoleReliability(BaseModel):
    """Per ``(agent_role, runtime)`` reliability -- a pure projection.

    ``extra="forbid"`` so a drifted field surfaces as a
    :class:`pydantic.ValidationError` at construction. Every numeric field is
    ``None`` exactly when :attr:`status` is
    :attr:`ReliabilityStatus.INSUFFICIENT`, so the refuse-to-score contract is
    unmissable in the type: a caller cannot read a number out of an under-N or
    wide-CI group because there is no number to read.

    Attributes:
        agent_role: The role the reliability is scored for.
        runtime: The runtime adapter id the reliability is scored for.
        n: Number of observed outcomes (``held is not None``) in the group
            (``>= 0``).
        status: :attr:`ReliabilityStatus.SCORED` when the group cleared the
            min-N floor AND the CI-width gate, else
            :attr:`ReliabilityStatus.INSUFFICIENT`.
        brier: Mean Brier score in ``[0.0, 1.0]`` (lower is better), or
            ``None`` when the group refused to score.
        reliability: Murphy REL (calibration) component -- how far the binned
            forecast probabilities sit from their observed frequencies (lower
            is better). ``None`` when the group refused to score.
        resolution: Murphy RES (discrimination) component -- how far the binned
            observed frequencies spread from the base rate (higher is better).
            ``None`` when the group refused to score.
        posterior_lower_bound: Conservative DISPLAY score -- the Wilson /
            Beta-posterior lower bound on the held-rate after empirical-Bayes
            shrink toward the sibling prior, in ``[0.0, 1.0]``. ``None`` when
            the group refused to score.
        routing_score: Optimistic upper bound (a Thompson-style exploration
            seed) on the held-rate, in ``[0.0, 1.0]``, kept SEPARATE from the
            display LB so under-observed roles can still be explored. ``None``
            when the group refused to score.
    """

    model_config = ConfigDict(extra="forbid")

    agent_role: AgentSessionRole
    runtime: str
    n: int = Field(ge=0)
    status: ReliabilityStatus
    brier: float | None = None
    reliability: float | None = None
    resolution: float | None = None
    posterior_lower_bound: float | None = Field(default=None, ge=0.0, le=1.0)
    routing_score: float | None = Field(default=None, ge=0.0, le=1.0)


def _forecast_probability(outcome: VerdictOutcome) -> float:
    """Return the verdict's forecast probability that the wave *held*.

    The verdict is a prediction; its numeric :attr:`VerdictOutcome.confidence`
    is the strength of that prediction in the verdict's OWN direction. A
    ``pass`` verdict forecasts the wave holds with probability ``confidence``;
    a non-``pass`` verdict forecasts it holds with probability
    ``1 - confidence`` (the confidence was in the *fail*). Folding both into a
    single p(hold) lets the Brier score live on one axis.
    """
    if outcome.verdict is AgentReportVerdict.PASS:
        return outcome.confidence
    return 1.0 - outcome.confidence


def _murphy_decomposition(
    forecasts: list[float],
    outcomes: list[float],
) -> tuple[float, float, float]:
    """Return ``(brier, reliability, resolution)`` via the Murphy decomposition.

    Murphy (1973) splits the Brier score into three additive terms over a
    partition of the forecast probabilities into bins::

        brier = reliability - resolution + uncertainty

    where, with ``N`` total forecasts, bin ``k`` holding ``n_k`` forecasts with
    mean forecast ``f_k`` and observed frequency ``o_k``, and the overall base
    rate ``o_bar``:

    - reliability (REL) = ``(1/N) * sum_k n_k * (f_k - o_k)**2`` -- calibration
      error; zero when every bin's forecast matches its observed frequency
      (lower is better);
    - resolution (RES) = ``(1/N) * sum_k n_k * (o_k - o_bar)**2`` --
      discrimination; how far the bins' observed frequencies spread from the
      base rate (higher is better);
    - uncertainty (UNC) = ``o_bar * (1 - o_bar)`` -- irreducible base-rate
      variance.

    The returned ``brier`` is computed directly as the mean squared error (it
    equals ``REL - RES + UNC`` up to floating-point rounding) so the headline
    number is exact rather than reassembled from the components.

    Args:
        forecasts: Forecast probabilities in ``[0.0, 1.0]``, one per outcome.
            Must be non-empty and the same length as *outcomes*.
        outcomes: Realized outcomes, each ``0.0`` or ``1.0``, aligned to
            *forecasts*.

    Returns:
        The ``(brier, reliability, resolution)`` triple.
    """
    n = len(forecasts)
    brier = math.fsum((f - o) ** 2 for f, o in zip(forecasts, outcomes, strict=True)) / n
    base_rate = math.fsum(outcomes) / n

    bin_forecasts: dict[int, list[float]] = defaultdict(list)
    bin_outcomes: dict[int, list[float]] = defaultdict(list)
    for forecast, outcome in zip(forecasts, outcomes, strict=True):
        # Right-closed bin index in [0, _MURPHY_BINS - 1]; a forecast of
        # exactly 1.0 lands in the top bin rather than overflowing.
        index = min(int(forecast * _MURPHY_BINS), _MURPHY_BINS - 1)
        bin_forecasts[index].append(forecast)
        bin_outcomes[index].append(outcome)

    reliability = 0.0
    resolution = 0.0
    for index, members in bin_forecasts.items():
        n_k = len(members)
        mean_forecast = math.fsum(members) / n_k
        mean_outcome = math.fsum(bin_outcomes[index]) / n_k
        reliability += n_k * (mean_forecast - mean_outcome) ** 2
        resolution += n_k * (mean_outcome - base_rate) ** 2
    reliability /= n
    resolution /= n
    return brier, reliability, resolution


def _beta_posterior_interval(
    *,
    held_count: int,
    n: int,
    prior_rate: float,
) -> tuple[float, float, float]:
    """Return ``(lower, upper, ci_width)`` of the EB-Beta posterior held-rate.

    Empirical-Bayes shrink: the sibling *prior_rate* is folded in as
    :data:`_PRIOR_STRENGTH` synthetic trials, giving a Beta posterior::

        alpha = prior_rate * _PRIOR_STRENGTH + held_count
        beta  = (1 - prior_rate) * _PRIOR_STRENGTH + (n - held_count)

    A group with few observed trials is therefore pulled hard toward its
    sibling prior; it only earns its own held-rate as real trials accrue. The
    posterior mean and variance are closed-form::

        mean = alpha / (alpha + beta)
        var  = alpha * beta / ((alpha + beta)**2 * (alpha + beta + 1))

    The credible interval is the normal approximation ``mean +/- z * sd``
    clamped to ``[0.0, 1.0]``. The conservative LOWER bound is the display
    score; the optimistic UPPER bound seeds routing exploration. ``ci_width``
    is the UNCLAMPED interval width ``2 * z * sd`` -- the honest measure of how
    pinned-down the held-rate is, used to gate the estimate (clamping at the
    boundaries would otherwise hide a still-wide posterior).

    Args:
        held_count: Number of observed outcomes that held (``0 <= held_count
            <= n``).
        n: Number of observed outcomes (``>= 1``).
        prior_rate: Sibling-prior held-rate in ``[0.0, 1.0]`` to shrink toward.

    Returns:
        The ``(lower, upper, ci_width)`` triple, with *lower* and *upper*
        clamped to ``[0.0, 1.0]`` and *ci_width* the unclamped width.
    """
    alpha = prior_rate * _PRIOR_STRENGTH + held_count
    beta = (1.0 - prior_rate) * _PRIOR_STRENGTH + (n - held_count)
    total = alpha + beta
    mean = alpha / total
    variance = (alpha * beta) / (total * total * (total + 1.0))
    sd = math.sqrt(variance)
    margin = _Z_95 * sd
    lower = max(0.0, mean - margin)
    upper = min(1.0, mean + margin)
    ci_width = 2.0 * margin
    return lower, upper, ci_width


def _insufficient(
    *,
    agent_role: AgentSessionRole,
    runtime: str,
    n: int,
) -> RoleReliability:
    """Return the honest-negative :class:`RoleReliability` for a refused group.

    Every numeric field stays ``None`` -- the refuse-to-score surface for a
    group below the min-N floor or wider than the CI-width gate.
    """
    return RoleReliability(
        agent_role=agent_role,
        runtime=runtime,
        n=n,
        status=ReliabilityStatus.INSUFFICIENT,
    )


def compute_role_reliability(
    outcomes: list[VerdictOutcome],
    config: ReputationConfig,
    sibling_prior: dict[AgentSessionRole, float],
) -> list[RoleReliability]:
    """Reduce observed verdict outcomes into per-role-runtime reliability.

    One pure reducer (no mutation, no IO, no git). For each
    ``(agent_role, runtime)`` group of *outcomes* whose ``held is not None``
    (observed -- in-flight verdicts are skipped), it computes:

    - the Brier score and its Murphy reliability / resolution components over
      the verdict's forecast p(hold) (see :func:`_murphy_decomposition`);
    - the empirical-Bayes Beta posterior on the held-rate, shrunk toward the
      group's *sibling_prior*, and its credible interval (see
      :func:`_beta_posterior_interval`); the conservative LOWER bound is the
      display ``posterior_lower_bound`` and the optimistic UPPER bound is the
      ``routing_score`` exploration seed.

    Honesty gate: a group with fewer than ``config.min_n`` observed outcomes,
    OR whose posterior credible interval is wider than ``config.ci_width_gate``,
    refuses to score -- :attr:`ReliabilityStatus.INSUFFICIENT` with every
    numeric field ``None``. The substrate is empty today, so this returns
    ``[]`` right now and lights up only as real verdict rows accrue.

    Args:
        outcomes: Observed and not-yet-observable verdict outcomes. The
            not-yet-observable rows (``held is None``) are dropped from every
            group before scoring. May be empty.
        config: The :class:`ReputationConfig` supplying the min-N floor and the
            CI-width gate.
        sibling_prior: Per-role held-rate in ``[0.0, 1.0]`` to shrink each
            group toward (empirical Bayes). A role absent from the mapping
            falls back to a neutral :data:`_DEFAULT_PRIOR`.

    Returns:
        One :class:`RoleReliability` per observed ``(agent_role, runtime)``
        group, ordered by ``(agent_role, runtime)``. Empty when *outcomes* is
        empty or holds no observed (``held is not None``) rows.
    """
    groups: dict[tuple[AgentSessionRole, str], list[VerdictOutcome]] = defaultdict(list)
    for outcome in outcomes:
        if outcome.held is None:
            continue
        groups[(outcome.agent_role, outcome.runtime)].append(outcome)

    reliabilities: list[RoleReliability] = []
    for (agent_role, runtime), group in sorted(groups.items()):
        n = len(group)
        if n < config.min_n:
            reliabilities.append(_insufficient(agent_role=agent_role, runtime=runtime, n=n))
            continue

        forecasts = [_forecast_probability(outcome) for outcome in group]
        observed = [1.0 if outcome.held else 0.0 for outcome in group]
        held_count = sum(1 for outcome in group if outcome.held)

        brier, reliability, resolution = _murphy_decomposition(forecasts, observed)
        prior_rate = sibling_prior.get(agent_role, _DEFAULT_PRIOR)
        lower, upper, ci_width = _beta_posterior_interval(
            held_count=held_count, n=n, prior_rate=prior_rate
        )

        if ci_width > config.ci_width_gate:
            reliabilities.append(_insufficient(agent_role=agent_role, runtime=runtime, n=n))
            continue

        reliabilities.append(
            RoleReliability(
                agent_role=agent_role,
                runtime=runtime,
                n=n,
                status=ReliabilityStatus.SCORED,
                brier=brier,
                reliability=reliability,
                resolution=resolution,
                posterior_lower_bound=lower,
                routing_score=upper,
            )
        )

    logger.debug(
        f"compute_role_reliability outcomes={len(outcomes)} groups={len(groups)} "
        f"scored={sum(1 for r in reliabilities if r.status is ReliabilityStatus.SCORED)}"
    )
    return reliabilities


__all__ = [
    "ReliabilityStatus",
    "ReputationConfig",
    "RoleReliability",
    "VerdictOutcome",
    "build_verdict_outcomes",
    "compute_role_reliability",
    "confidence_to_float",
]
