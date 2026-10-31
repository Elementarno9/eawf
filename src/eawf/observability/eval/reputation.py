"""Verdict-to-outcome projection.

The reputation/Brier scorer (a later wave) cannot score a per-wave verdict
until that verdict has a *realized outcome* to score against: a verdict is a
prediction ("this wave is done / correct"), and the outcome is whether that
prediction was borne out. This module builds the missing data substrate -- a
:class:`VerdictOutcome` per per-wave verdict row, each carrying whether the
verdict ``held`` and the state-observable signal that settled it.

This module holds the verdict and outcome row models and the pure scorers
over them: no mutation, no git, no daemon.
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
    ReportSource,
)
from eawf.workflow.agent_report.rollup import iter_agent_reports

logger = logging.getLogger(__name__)

#: The report role whose rows are per-wave verdicts. The verdict producer
#: (:mod:`eawf.workflow.dispatch.verdict`) writes a fresh-context AUDITOR
#: report at ``base_id=wave_id`` for every wave it judges, so the outcome loop
#: reads exactly the AUDITOR cohort.
_VERDICT_ROLE: AgentSessionRole = AgentSessionRole.AUDITOR


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
            report's :class:`~eawf.kernel.state.enums.Confidence` bucket.
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


# --- fleet verdict rollup -----------------------------------
#
# The fleet rollup is the DISPLAY-side companion to the outcome loop above: it
# lists each wave's LATEST auditor verdict across the whole fleet so the
# agent-watch TUI can paint a per-wave verdict pane tinted by outcome. This
# helper surfaces the raw verdict per wave (the operator's at-a-glance fleet
# view) from the AUDITOR store, and keeps only the most-recent verdict per
# wave so a re-audited wave shows its current verdict and not a shadowed
# earlier one.
#
# Honest-empty by construction: the per-wave report store is empty today, so
# :func:`fleet_verdict_rollup` returns ``[]`` right now -- and the empty list
# is the deliverable, never a fabricated row. The pane renders its honest-empty
# line off that empty list rather than implying a fleet rollup exists.


class FleetVerdictRow(BaseModel):
    """One wave's latest auditor verdict in the fleet rollup -- a pure row.

    ``extra="forbid"`` so a drifted field surfaces as a
    :class:`pydantic.ValidationError` at construction rather than skewing the
    rendered pane. Carries exactly the fields the agent-watch verdict pane
    paints: the wave the verdict is about, the recorded
    :class:`~eawf.kernel.state.enums.AgentReportVerdict` (the pane tints each
    row by this outcome), and the runtime that produced it.

    Attributes:
        wave_id: The wave the verdict was about (the report ``base_id``).
        verdict: The recorded
            :class:`~eawf.kernel.state.enums.AgentReportVerdict` -- the outcome
            the pane tints the row by (pass / pass-with-followups / fail /
            blocked).
        runtime: Runtime adapter id that produced the verdict report.
        report_source: Whether the report was authored by the agent or
            synthesized by the daemon on assist-loop exhaustion; the pane flags
            a synthesized row so a degrade never reads as an authored verdict.
            Defaults to :attr:`~eawf.kernel.state.enums.ReportSource.AUTHORED`
            so a pre-marker replayed envelope reads as authored.
    """

    model_config = ConfigDict(extra="forbid")

    wave_id: str
    verdict: AgentReportVerdict
    runtime: str
    report_source: ReportSource = ReportSource.AUTHORED


def fleet_verdict_rollup(state_path: Path) -> list[FleetVerdictRow]:
    """List each wave's latest auditor verdict across the fleet -- a pure read.

    Reads the AUDITOR per-wave verdict rows off disk (via
    :func:`eawf.workflow.agent_report.rollup.iter_agent_reports`, which returns
    rows oldest-first), keeps only the LATEST row per wave (``base_id``) so a
    re-audited wave shows its current verdict and not a shadowed earlier one,
    and returns one :class:`FleetVerdictRow` per wave ordered by wave id for a
    stable pane layout. No state join, no mutation, no daemon -- a pure store
    read.

    Honest-empty: the per-wave report store is empty today, so this returns
    ``[]`` right now -- the correct result, not a bug. It never fabricates a
    verdict row; it simply has no auditor rows to roll up yet.

    Args:
        state_path: Path to ``state.json``; the AUDITOR report store resolves
            under its sibling ``store/`` directory.

    Returns:
        One :class:`FleetVerdictRow` per wave with an auditor verdict, carrying
        that wave's latest verdict + producing runtime, ordered by wave id.
        Empty when no auditor verdict row exists.
    """
    latest_by_wave: dict[str, FleetVerdictRow] = {}
    for row in iter_agent_reports(state_path, role=_VERDICT_ROLE):
        # Rows arrive oldest-first, so a later assignment overwrites an earlier
        # one and the most-recent verdict per wave wins.
        latest_by_wave[row.payload.header.base_id] = FleetVerdictRow(
            wave_id=row.payload.header.base_id,
            verdict=row.payload.body.verdict,
            runtime=row.payload.header.runtime,
            report_source=row.payload.body.report_source,
        )
    rollup = [latest_by_wave[wave_id] for wave_id in sorted(latest_by_wave)]
    logger.debug(f"fleet_verdict_rollup waves={len(rollup)}")
    return rollup


# --- reliability scoring layer ------------------------------
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
# ``None``. This mirrors the refuse-to-score posture of
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
        tier_thresholds: Display-LB floor per tier label ("C" / "B" / "A")
            override. When non-empty it overrides :data:`DEFAULT_TIER_THRESHOLDS`
            in :func:`map_reliability_to_tier`; when empty (the default) the
            static defaults apply.
        loss_weight: Bounded (~3x) asymmetric demote weight RESERVED for the
            deferred stateful graduate / demote transition -- a demotion costs
            more than a promotion so a regressed role drops fast. NOT applied
            by the static :func:`map_reliability_to_tier` mapping; held here so
            a future wave wires the hysteresis in without a config change.
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
        tier: The earned-autonomy :class:`ReputationTier` (C / B / A) the
            display :attr:`posterior_lower_bound` maps onto, set only on a
            SCORED row. ``None`` exactly when :attr:`status` is
            :attr:`ReliabilityStatus.INSUFFICIENT` -- the honest-empty surface,
            since an unscored group has no display LB to ladder.
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
    tier: ReputationTier | None = None


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


def expected_calibration_error(
    forecasts: list[float],
    outcomes: list[float],
    *,
    bins: int = _MURPHY_BINS,
) -> float:
    """Return the bucketed expected calibration error (ECE) of a forecast set.

    ECE partitions the forecast probabilities into *bins* equal-width buckets,
    and within each non-empty bucket measures the gap between the bucket's mean
    forecast (the confidence the jury claimed) and the bucket's observed
    frequency (the accuracy it actually achieved). The per-bucket gaps are then
    averaged weighted by bucket population::

        ece = sum_k (n_k / N) * |mean_forecast_k - observed_freq_k|

    A perfectly calibrated forecaster -- one whose claimed confidence matches
    its realized accuracy in every bucket -- scores ``0.0``. The metric is in
    ``[0.0, 1.0]`` and lower is better. Unlike the Murphy reliability term
    (which squares the gap), ECE keeps the gap linear, so it reads directly as
    "on average, how far off was the stated confidence".

    The bucketing mirrors :func:`_murphy_decomposition`: a right-closed index in
    ``[0, bins - 1]`` so a forecast of exactly ``1.0`` lands in the top bucket
    rather than overflowing.

    Args:
        forecasts: Forecast probabilities in ``[0.0, 1.0]``, one per outcome.
            Must be non-empty and the same length as *outcomes*.
        outcomes: Realized outcomes, each ``0.0`` or ``1.0``, aligned to
            *forecasts*.
        bins: Number of equal-width buckets to partition the forecasts into
            (``>= 1``). Defaults to :data:`_MURPHY_BINS`.

    Returns:
        The expected calibration error in ``[0.0, 1.0]``.

    Raises:
        ValueError: When *forecasts* is empty, when *forecasts* and *outcomes*
            differ in length, or when *bins* is below 1.
    """
    if not forecasts:
        raise ValueError("cannot compute ece over an empty forecast set")
    if len(forecasts) != len(outcomes):
        raise ValueError(f"forecast/outcome length mismatch: {len(forecasts)} vs {len(outcomes)}")
    if bins < 1:
        raise ValueError(f"ece needs at least 1 bin: {bins!r}")

    n = len(forecasts)
    bin_forecasts: dict[int, list[float]] = defaultdict(list)
    bin_outcomes: dict[int, list[float]] = defaultdict(list)
    for forecast, outcome in zip(forecasts, outcomes, strict=True):
        index = min(int(forecast * bins), bins - 1)
        bin_forecasts[index].append(forecast)
        bin_outcomes[index].append(outcome)

    ece = 0.0
    for index, members in bin_forecasts.items():
        n_k = len(members)
        mean_forecast = math.fsum(members) / n_k
        observed_freq = math.fsum(bin_outcomes[index]) / n_k
        ece += (n_k / n) * abs(mean_forecast - observed_freq)
    return ece


# --- trust-tier ladder --------------------------------------
#
# The ladder maps the conservative DISPLAY score (the Wilson / Beta-posterior
# ``posterior_lower_bound`` the scoring layer computes) onto a C -> B -> A
# earned-autonomy tier. It is a pure, static threshold lookup: a SCORED row
# gets the highest tier whose display-LB floor it meets, an INSUFFICIENT row
# gets no tier at all.
#
# Honest-negative by construction: no RoleReliability reaches SCORED today (the
# verdict-outcome substrate is empty, so every group refuses to score), so the
# tier is ``None`` for every role right now. The ladder is correct but idle
# until scored rows accrue -- no graduate / demote transition logic ships
# before rows exist. The stateful asymmetric graduate/demote transition (where
# a demotion costs more than a promotion, weighted by
# :attr:`ReputationConfig.loss_weight`) is a deferred follow-up; this wave
# ships only the static LB -> tier mapping so the field is wired the instant a
# group scores.

#: Display-LB floor per tier, used when :attr:`ReputationConfig.tier_thresholds`
#: is empty. The floors are deliberately conservative: an A tier
#: (earned-autonomy) needs a held-rate lower bound of at least 0.85, a B tier
#: (trusted for routine) at least 0.6, and C (probationary / unproven) is the
#: floorless catch-all at 0.0 so every scored role lands on at least C. These
#: are starting points an operator overrides via the ``trust.tier_thresholds``
#: config leaf once live held-rates inform a better cut.
DEFAULT_TIER_THRESHOLDS: dict[str, float] = {"C": 0.0, "B": 0.6, "A": 0.85}


class ReputationTier(StrEnum):
    """Earned-autonomy tier a role's display score maps onto: C -> B -> A.

    This ladder answers "has this agent earned autonomy?" (a property of an
    agent's track record), never whether one deliverable was verified.

    The order is ascending trust:

    :attr:`C`
        Probationary / unproven -- the floor every scored role lands on before
        its held-rate lower bound clears a higher tier.
    :attr:`B`
        Trusted for routine work -- the display LB cleared the mid floor.
    :attr:`A`
        Earned autonomy -- the display LB cleared the top floor.
    """

    C = "C"
    B = "B"
    A = "A"


def map_reliability_to_tier(
    posterior_lower_bound: float,
    config: ReputationConfig,
) -> ReputationTier:
    """Map a conservative display-LB score onto its earned-autonomy tier.

    A pure, static threshold lookup: *posterior_lower_bound* (the Wilson /
    Beta-posterior lower bound the scoring layer computes -- the conservative
    DISPLAY score) is mapped to the highest tier whose floor it meets. The
    floors come from *config*'s :attr:`~ReputationConfig.tier_thresholds` when
    that mapping is non-empty, else from :data:`DEFAULT_TIER_THRESHOLDS`.

    The comparison is inclusive (``>=``): a score sitting exactly on a tier's
    floor earns that tier. With the default floors ``A=0.85`` / ``B=0.6`` /
    ``C=0.0`` an LB of exactly ``0.85`` maps to :attr:`ReputationTier.A` and
    exactly ``0.6`` to :attr:`ReputationTier.B`.

    :attr:`ReputationConfig.loss_weight` is RESERVED for the deferred stateful
    asymmetric graduate / demote transition (a demotion costs more than a
    promotion so a regressed role drops fast) and is deliberately NOT applied
    in this static mapping -- a future wave wires that hysteresis in without an
    API change to this function.

    Args:
        posterior_lower_bound: The conservative display-LB held-rate score in
            ``[0.0, 1.0]`` (a SCORED row's
            :attr:`RoleReliability.posterior_lower_bound`).
        config: The :class:`ReputationConfig` whose
            :attr:`~ReputationConfig.tier_thresholds` override the default
            floors when non-empty.

    Returns:
        The highest :class:`ReputationTier` whose floor *posterior_lower_bound*
        meets. Always at least :attr:`ReputationTier.C` (its default floor is
        ``0.0``).
    """
    thresholds = config.tier_thresholds or DEFAULT_TIER_THRESHOLDS
    if posterior_lower_bound >= thresholds[ReputationTier.A.value]:
        return ReputationTier.A
    if posterior_lower_bound >= thresholds[ReputationTier.B.value]:
        return ReputationTier.B
    return ReputationTier.C


__all__ = [
    "DEFAULT_TIER_THRESHOLDS",
    "FleetVerdictRow",
    "ReliabilityStatus",
    "ReputationConfig",
    "ReputationTier",
    "RoleReliability",
    "VerdictOutcome",
    "expected_calibration_error",
    "fleet_verdict_rollup",
    "map_reliability_to_tier",
]
