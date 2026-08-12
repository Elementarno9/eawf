"""Verdict-agreement cost A/B over the metering ledger.

The question this projection exists to answer: can a tiered-by-role routing
config (cheaper model per role -- config B) hold the same verdict quality as
opus-everywhere (config A) while spending less? The honest answer today is
**we do not know yet**, and this module is built so it stays honest: the
``dispatch_cost`` ledger the metering writer
(:func:`eawf.runtime.runtimes.metering.price_spawn_result`) feeds is empty
(a row lands only on a live priced spawn) and the per-wave verdict stores
are empty (zero ``*_report.jsonl`` rows), so the A/B has nothing to compute
and **refuses to compute** under a hard min-N gate -- it never fabricates a
``$`` figure or a flip rate. The projection exists now so it lights up the
instant a live multi-wave run accrues cost + verdict rows; this mirrors the
refuse-to-score pattern in
:func:`eawf.observability.eval.self_eval.summarize_self_eval`.

The three discriminators, computed per ``(agent_role, runtime)`` so the
cross-vendor (codex / opencode) legs are comparable to the claude leg:

1. **verdict-flip rate** -- on the SAME closed waves, how often a cheaper
   tier disagrees with the top (opus-equivalent) tier on the pass/fail
   call. This is the persona-experiment method applied to model tier:
   identical input (the same wave), vary only the model, count the flips.
   Only waves a group ran on BOTH a cheaper AND the top tier contribute.
2. **audit-pass-first-try rate** -- whether the cheaper tier raises re-work:
   the fraction of a cheaper tier's *first-attempt* verdicts that pass.
3. **$/closed-wave** -- the priced spend per distinct closed wave, summed
   from ``dispatch_cost`` rows. Subscription-first caveat (the ratified
   cost-calibration decision, 2026-05-31): both the claude and codex lanes
   are subscription-first and eawf builds NO rate-limit / credit gauge, so
   this ``$`` figure governs ONLY the metered API-key FALLBACK paths -- on
   subscription auth the token tally is informational, not a bill. The
   primary discriminators are therefore the two quality metrics above;
   ``$/closed-wave`` is reported but is meaningful only for fallback-priced
   rows, and a row priced at ``$0`` (``priced is False`` upstream) does not
   inflate the figure.

The **decision rule** (:func:`recommend_tier`) is pure: a role keeps the
cheaper tier iff its verdict-flip rate vs the top tier is at or below the
flip threshold AND its audit-pass-first-try rate does not drop below the
top tier's by more than the regression threshold. It returns a
``keep-cheaper`` / ``bump-to-top`` recommendation only when the group's data
clears the min-N gate; below the gate every group is ``insufficient``.

KISS / YAGNI: this is a pure projection + the decision rule + the honest-
empty gate. There is no live A/B runner, no routing mutation, no new daemon
RPC, and -- per the subscription-first caveat -- no rate-limit window model.

Two layers, kept separate so the gate + math are testable without I/O:

- :func:`summarize_cost_ab` -- the **pure** reducer over typed cost +
  verdict observation tuples. It is the refuse-to-compute gate.
- :func:`compute_cost_ab` -- the thin store-reading entry that pulls the
  ``dispatch_cost`` rows off the metrics store and the verdict rows off the
  role-report JSONL, joins them into observations, and defers to the
  reducer. No metric math lives here.

Join caveat (the W02 spike finding, carried from
:class:`eawf.observability.telemetry.models.TelemetryDispatchCost`): the
``dispatch_cost`` payload carries no ``session_id`` and its per-dispatch
``attempt_id`` UUID never reconciles to the agent-report ``attempt`` int.
So a verdict row cannot be joined to one specific cost row by attempt; the
store layer attaches a model/tier to a verdict by the coarser
``(wave_id, runtime)`` key (the tier(s) the wave was dispatched on for that
runtime). A verdict whose ``(wave_id, runtime)`` matches no priced
dispatch row carries no tier and is dropped from the tier-keyed metrics --
it cannot say which model produced it, so counting it would mis-bucket the
flip rate.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import AgentReportVerdict
from eawf.observability.telemetry.models import TelemetryDispatchCost
from eawf.observability.telemetry.store.base import (
    AbstractMetricsStore,
    StoreBackend,
    metrics_db_path,
    open_store,
)
from eawf.workflow.agent_report.rollup import iter_agent_reports
from eawf.workflow.dispatch.routing import TOP_TIER_INDEX, tier_for_model

logger = logging.getLogger(__name__)

#: Hard minimum number of observations (cost + verdict rows) below which the
#: A/B REFUSES to compute. Below this floor the report is the honest-negative
#: surface: every metric is ``None`` and no ``$`` figure is emitted, so a
#: near-empty ledger can never produce a Goodhartable flip rate or spend
#: number. Sized so a handful of rows is not mistaken for a calibrated signal.
MIN_COST_AB_N: int = 5

#: Default verdict-flip-rate ceiling for the keep-cheaper recommendation. A
#: cheaper tier whose verdicts flip against the top tier at or below this rate
#: is treated as agreeing closely enough to keep. ``0.0`` would demand perfect
#: agreement; a small slack absorbs one-off disagreement without bumping.
DEFAULT_FLIP_THRESHOLD: float = 0.05

#: Default audit-pass-first-try regression the keep-cheaper recommendation
#: tolerates. The cheaper tier may pass first-try at most this much *below*
#: the top tier before the extra re-work argues for bumping back up.
DEFAULT_PASS_REGRESSION_THRESHOLD: float = 0.05

#: Verdicts that count as a "pass" for the flip + first-try metrics.
#: ``PASS_WITH_FOLLOWUPS`` is a pass with bookkeeping, not a failure (same
#: convention as the self-eval surface); ``FAIL`` / ``BLOCKED`` are not.
_PASS_VERDICTS: frozenset[AgentReportVerdict] = frozenset(
    {AgentReportVerdict.PASS, AgentReportVerdict.PASS_WITH_FOLLOWUPS}
)


class CostABStatus(StrEnum):
    """Whether the cost A/B produced real numbers or refused.

    :attr:`INSUFFICIENT_DATA` is the honest-negative surface: the total
    observation count is below :data:`MIN_COST_AB_N`, so no flip rate, no
    pass-first-try rate, and no ``$`` figure are emitted. :attr:`COMPUTED`
    means the cohort cleared the gate and the per-group rows carry real,
    defensible numbers.
    """

    INSUFFICIENT_DATA = "insufficient_data"
    COMPUTED = "computed"


class TierRecommendation(StrEnum):
    """The decision rule's per-group routing recommendation.

    :attr:`KEEP_CHEAPER` -- the cheaper tier agrees with the top tier
    closely enough (flip rate and first-try pass rate both within bounds) to
    stay on the cheaper model. :attr:`BUMP_TO_TOP` -- the cheaper tier flips
    too often or regresses first-try pass rate, so route this role to the top
    tier. :attr:`INSUFFICIENT` -- the group lacks the data (no top-tier
    baseline, or below the gate) to make the call.
    """

    KEEP_CHEAPER = "keep-cheaper"
    BUMP_TO_TOP = "bump-to-top"
    INSUFFICIENT = "insufficient"


class CostObservation(BaseModel):
    """One priced ``dispatch_cost`` fact, classified onto a capability tier.

    The cost half of a cost-A/B observation: a single dispatch's spend, the
    role + runtime it served, the model it was priced against, and the tier
    that model classifies onto. Frozen + ``extra="forbid"`` so a drifted
    field fails at construction rather than skewing a downstream sum.

    Attributes:
        agent_role: The dispatched wave's role (``agent_role`` canonical
            name), e.g. ``"executor"``.
        runtime: Runtime that incurred the cost (``claude`` / ``codex`` /
            ``opencode``) -- the key the cross-vendor leg groups on.
        model: Model id the cost was priced against.
        tier: Capability-tier index (``0`` cheapest, :data:`TOP_TIER_INDEX`
            costliest) the model classifies onto via
            :func:`eawf.workflow.dispatch.routing.tier_for_model`.
        wave_id: Wave the dispatch served, or ``None`` for an interactive
            session (which never contributes to a per-wave $/closed-wave).
        cost_usd: Priced cost in USD (exact :class:`~decimal.Decimal`).
        priced: ``True`` when a pricing row resolved upstream (a real
            token-derived figure, including a genuine ``$0``); ``False`` when
            the ``$0`` is an unpriced fallback. Only ``priced`` rows feed the
            ``$/closed-wave`` figure so an unpriceable spawn does not deflate
            it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_role: str = Field(min_length=1)
    runtime: str = Field(min_length=1)
    model: str = Field(min_length=1)
    tier: int = Field(ge=0)
    wave_id: str | None = None
    cost_usd: Decimal = Field(ge=0)
    priced: bool = True


class VerdictObservation(BaseModel):
    """One agent-report verdict, classified onto a capability tier.

    The verdict half of a cost-A/B observation: a single closed-wave verdict
    with the role + runtime that produced it, the model / tier it was
    dispatched on (joined by ``(wave_id, runtime)`` -- see the module
    docstring's join caveat), the wave, the attempt number, and the verdict
    itself. Frozen + ``extra="forbid"``.

    Attributes:
        agent_role: The verdict's role (``agent_role`` canonical name).
        runtime: Runtime that produced the verdict.
        model: Model id the verdict's wave was dispatched on.
        tier: Capability-tier index the model classifies onto.
        wave_id: Wave the verdict closed (the flip-rate join key).
        attempt: 1-based attempt number; ``attempt == 1`` is the first try
            the pass-first-try rate counts.
        verdict: The recorded :class:`~eawf.kernel.state.enums.AgentReportVerdict`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_role: str = Field(min_length=1)
    runtime: str = Field(min_length=1)
    model: str = Field(min_length=1)
    tier: int = Field(ge=0)
    wave_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    verdict: AgentReportVerdict


class CostABRow(BaseModel):
    """Per-``(agent_role, runtime)`` cost-A/B discriminators + recommendation.

    One row of the computed A/B. Every metric is ``None`` when the group
    lacks the data to compute it (no cheaper-vs-top wave overlap for the flip
    rate, no cheaper-tier verdicts for the first-try rate, no closed waves
    for the $/closed-wave). A ``None`` metric is the honest absence of a
    signal, never a fabricated zero.

    Attributes:
        agent_role: The group's role.
        runtime: The group's runtime.
        flip_rate: Fraction of shared waves where the cheaper tier's pass/fail
            call disagrees with the top tier's, in ``[0.0, 1.0]``; ``None``
            when no wave was seen by both a cheaper and the top tier.
        shared_wave_count: Number of waves a cheaper AND the top tier both
            produced a verdict for (the flip-rate denominator).
        cheaper_pass_first_try: Fraction of the cheaper tier's first-attempt
            verdicts that passed, in ``[0.0, 1.0]``; ``None`` when the
            cheaper tier produced no first-attempt verdict.
        top_pass_first_try: The same fraction for the top tier; ``None`` when
            the top tier produced no first-attempt verdict.
        cost_per_closed_wave_cheaper: Priced ``$`` per distinct closed wave on
            the cheaper tier(s); ``None`` when no priced cheaper-tier cost row
            joins a closed wave. Governs the metered fallback path only.
        cost_per_closed_wave_top: The same ``$/closed-wave`` for the top tier;
            ``None`` when absent.
        recommendation: The decision rule's call for this group.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_role: str
    runtime: str
    flip_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    shared_wave_count: int = Field(default=0, ge=0)
    cheaper_pass_first_try: float | None = Field(default=None, ge=0.0, le=1.0)
    top_pass_first_try: float | None = Field(default=None, ge=0.0, le=1.0)
    cost_per_closed_wave_cheaper: Decimal | None = Field(default=None, ge=0)
    cost_per_closed_wave_top: Decimal | None = Field(default=None, ge=0)
    recommendation: TierRecommendation


class CostABReport(BaseModel):
    """Honest-negative verdict-agreement cost A/B over the metering ledger.

    Frozen + ``extra="forbid"`` so a drifted field surfaces as a
    :class:`pydantic.ValidationError` at construction rather than silently
    skewing a render.

    The :attr:`rows` list is empty exactly when :attr:`status` is
    :attr:`CostABStatus.INSUFFICIENT_DATA` -- the type makes the
    refuse-to-compute contract unmissable: below the min-N gate there are no
    per-group numbers to read, and in particular no ``$`` figure.

    Attributes:
        status: :attr:`CostABStatus.COMPUTED` when the cohort cleared
            :attr:`min_n`, else :attr:`CostABStatus.INSUFFICIENT_DATA`.
        observation_count: Total cost + verdict observations seen (``>= 0``).
        min_n: The hard minimum-N gate applied. Echoed so a render can state
            the bar.
        rows: Per-``(agent_role, runtime)`` discriminator rows, sorted by
            ``(agent_role, runtime)``; empty below the gate.
        note: One operator-facing line explaining the status -- why the A/B
            refused, or what the rows cover (including the subscription-first
            ``$``-governs-fallback caveat).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: CostABStatus
    observation_count: int = Field(ge=0)
    min_n: int = Field(ge=1)
    rows: list[CostABRow] = Field(default_factory=list)
    note: str


def recommend_tier(
    *,
    flip_rate: float | None,
    cheaper_pass_first_try: float | None,
    top_pass_first_try: float | None,
    flip_threshold: float = DEFAULT_FLIP_THRESHOLD,
    pass_regression_threshold: float = DEFAULT_PASS_REGRESSION_THRESHOLD,
) -> TierRecommendation:
    """Apply the keep-cheaper-vs-bump decision rule to one group's metrics.

    Pure function -- no I/O, no hidden state. The rule: keep the cheaper tier
    iff its verdict-flip rate against the top tier is at or below
    *flip_threshold* AND its first-try pass rate is not more than
    *pass_regression_threshold* below the top tier's. A missing flip rate
    (no cheaper-vs-top wave overlap) means there is no baseline to compare,
    so the call is :attr:`TierRecommendation.INSUFFICIENT` -- the rule never
    recommends keeping a cheaper tier it has not actually compared.

    Args:
        flip_rate: The group's verdict-flip rate vs the top tier, or ``None``
            when no shared wave was seen by both tiers.
        cheaper_pass_first_try: The cheaper tier's first-try pass rate, or
            ``None``.
        top_pass_first_try: The top tier's first-try pass rate, or ``None``.
        flip_threshold: Flip-rate ceiling for keep-cheaper. Defaults to
            :data:`DEFAULT_FLIP_THRESHOLD`.
        pass_regression_threshold: Tolerated first-try pass-rate drop.
            Defaults to :data:`DEFAULT_PASS_REGRESSION_THRESHOLD`.

    Returns:
        The :class:`TierRecommendation` for the group.

    Raises:
        ValueError: When either threshold is outside ``[0.0, 1.0]`` -- a
            threshold off the rate scale cannot gate a rate.
    """
    if not 0.0 <= flip_threshold <= 1.0:
        raise ValueError(f"flip_threshold must be in [0.0, 1.0]: {flip_threshold!r}")
    if not 0.0 <= pass_regression_threshold <= 1.0:
        raise ValueError(
            f"pass_regression_threshold must be in [0.0, 1.0]: {pass_regression_threshold!r}"
        )

    if flip_rate is None:
        return TierRecommendation.INSUFFICIENT
    if flip_rate > flip_threshold:
        return TierRecommendation.BUMP_TO_TOP
    if (
        cheaper_pass_first_try is not None
        and top_pass_first_try is not None
        and top_pass_first_try - cheaper_pass_first_try > pass_regression_threshold
    ):
        return TierRecommendation.BUMP_TO_TOP
    return TierRecommendation.KEEP_CHEAPER


def _pass_first_try_rate(
    verdicts: list[VerdictObservation],
    *,
    top: bool,
) -> float | None:
    """Return the first-attempt pass rate for the cheaper or top tier subset.

    Args:
        verdicts: All verdict observations for one group.
        top: When ``True`` count only top-tier verdicts, else only cheaper
            (below-top) tier verdicts.

    Returns:
        The fraction of first-attempt verdicts in the selected tier subset
        that passed, or ``None`` when the subset has no first-attempt
        verdict.
    """
    subset = [obs for obs in verdicts if obs.attempt == 1 and ((obs.tier >= TOP_TIER_INDEX) == top)]
    if not subset:
        return None
    passed = sum(1 for obs in subset if obs.verdict in _PASS_VERDICTS)
    return passed / len(subset)


def _wave_pass_by_tier_class(
    verdicts: list[VerdictObservation],
) -> tuple[dict[str, bool], dict[str, bool]]:
    """Reduce verdicts to a per-wave pass/fail call for cheaper and top tiers.

    A wave's call for a tier class is "pass" iff any verdict that wave
    produced on that tier class passed (a single pass clears the wave; the
    flip metric asks whether the *outcome* agrees, not every attempt). Waves
    are kept separate per tier class so the flip comparison joins them.

    Args:
        verdicts: All verdict observations for one group.

    Returns:
        A ``(cheaper_by_wave, top_by_wave)`` pair mapping wave id to its
        pass/fail call on the cheaper and top tier classes respectively.
    """
    cheaper: dict[str, bool] = {}
    top: dict[str, bool] = {}
    for obs in verdicts:
        target = top if obs.tier >= TOP_TIER_INDEX else cheaper
        passed = obs.verdict in _PASS_VERDICTS
        target[obs.wave_id] = target.get(obs.wave_id, False) or passed
    return cheaper, top


def _flip_rate(
    verdicts: list[VerdictObservation],
) -> tuple[float | None, int]:
    """Return the cheaper-vs-top verdict-flip rate and the shared-wave count.

    On waves seen by BOTH a cheaper tier and the top tier, count the waves
    whose cheaper-tier pass/fail call disagrees with the top-tier call. The
    flip rate is that disagreement count over the shared-wave count.

    Args:
        verdicts: All verdict observations for one group.

    Returns:
        A ``(flip_rate, shared_wave_count)`` pair; ``flip_rate`` is ``None``
        when no wave was seen by both tiers (``shared_wave_count == 0``).
    """
    cheaper_by_wave, top_by_wave = _wave_pass_by_tier_class(verdicts)
    shared = sorted(set(cheaper_by_wave) & set(top_by_wave))
    if not shared:
        return None, 0
    flips = sum(1 for wave_id in shared if cheaper_by_wave[wave_id] != top_by_wave[wave_id])
    return flips / len(shared), len(shared)


def _cost_per_closed_wave(
    costs: list[CostObservation],
    *,
    top: bool,
) -> Decimal | None:
    """Return priced $/distinct-closed-wave for the cheaper or top tier subset.

    Only ``priced`` cost rows with a non-``None`` ``wave_id`` contribute, so
    an unpriceable spawn (``priced is False``) or an interactive session does
    not distort the per-wave spend. Per the subscription-first caveat this
    figure governs the metered fallback path only.

    Args:
        costs: All cost observations for one group.
        top: When ``True`` sum only top-tier rows, else only cheaper tier.

    Returns:
        The summed priced cost over the count of distinct closed waves, or
        ``None`` when the selected subset has no priced wave-scoped row.
    """
    subset = [
        obs
        for obs in costs
        if obs.priced and obs.wave_id is not None and ((obs.tier >= TOP_TIER_INDEX) == top)
    ]
    if not subset:
        return None
    waves = {obs.wave_id for obs in subset}
    total = sum((obs.cost_usd for obs in subset), start=Decimal("0"))
    return total / Decimal(len(waves))


def _summarize_group(
    agent_role: str,
    runtime: str,
    costs: list[CostObservation],
    verdicts: list[VerdictObservation],
    *,
    flip_threshold: float,
    pass_regression_threshold: float,
) -> CostABRow:
    """Fold one group's cost + verdict observations into a :class:`CostABRow`."""
    flip_rate, shared_wave_count = _flip_rate(verdicts)
    cheaper_first_try = _pass_first_try_rate(verdicts, top=False)
    top_first_try = _pass_first_try_rate(verdicts, top=True)
    recommendation = recommend_tier(
        flip_rate=flip_rate,
        cheaper_pass_first_try=cheaper_first_try,
        top_pass_first_try=top_first_try,
        flip_threshold=flip_threshold,
        pass_regression_threshold=pass_regression_threshold,
    )
    return CostABRow(
        agent_role=agent_role,
        runtime=runtime,
        flip_rate=flip_rate,
        shared_wave_count=shared_wave_count,
        cheaper_pass_first_try=cheaper_first_try,
        top_pass_first_try=top_first_try,
        cost_per_closed_wave_cheaper=_cost_per_closed_wave(costs, top=False),
        cost_per_closed_wave_top=_cost_per_closed_wave(costs, top=True),
        recommendation=recommendation,
    )


def summarize_cost_ab(
    costs: tuple[CostObservation, ...],
    verdicts: tuple[VerdictObservation, ...],
    *,
    min_n: int = MIN_COST_AB_N,
    flip_threshold: float = DEFAULT_FLIP_THRESHOLD,
    pass_regression_threshold: float = DEFAULT_PASS_REGRESSION_THRESHOLD,
) -> CostABReport:
    """Reduce cost + verdict observations into a min-N-gated cost A/B report.

    This is the refuse-to-compute gate. The total observation count is the
    number of cost rows plus verdict rows; below *min_n* the report is
    :attr:`CostABStatus.INSUFFICIENT_DATA` with no per-group rows and -- the
    load-bearing guarantee -- no ``$`` figure anywhere. At or above *min_n*
    the report groups the observations by ``(agent_role, runtime)`` and
    computes the three discriminators + the decision rule per group.

    Pure: no I/O, no store access. :func:`compute_cost_ab` reads the rows off
    disk and defers here.

    Args:
        costs: The cost observations (priced ``dispatch_cost`` facts).
        verdicts: The verdict observations (closed-wave agent-report
            verdicts joined to a tier).
        min_n: Hard minimum total observation count to clear before any
            metric is emitted. Defaults to :data:`MIN_COST_AB_N`.
        flip_threshold: Flip-rate ceiling for the keep-cheaper rule.
            Defaults to :data:`DEFAULT_FLIP_THRESHOLD`.
        pass_regression_threshold: Tolerated first-try pass-rate drop.
            Defaults to :data:`DEFAULT_PASS_REGRESSION_THRESHOLD`.

    Returns:
        A :class:`CostABReport`; honest-negative (no rows, no ``$``) when the
        observation count is below *min_n*.

    Raises:
        ValueError: When *min_n* is less than one -- a zero or negative gate
            would defeat the refuse-to-compute guarantee.
    """
    if min_n < 1:
        raise ValueError(f"min_n must be >= 1 to gate the cost A/B: {min_n!r}")

    observation_count = len(costs) + len(verdicts)
    if observation_count < min_n:
        logger.debug(f"summarize_cost_ab refuse observations={observation_count} min_n={min_n}")
        return CostABReport(
            status=CostABStatus.INSUFFICIENT_DATA,
            observation_count=observation_count,
            min_n=min_n,
            rows=[],
            note=(
                f"insufficient data: {observation_count} cost+verdict observations "
                f"below minimum N={min_n}; refusing to compute the cost A/B "
                "(the dispatch_cost ledger and verdict stores light up on a live "
                "priced multi-wave run)"
            ),
        )

    costs_by_group: dict[tuple[str, str], list[CostObservation]] = defaultdict(list)
    for cost in costs:
        costs_by_group[(cost.agent_role, cost.runtime)].append(cost)
    verdicts_by_group: dict[tuple[str, str], list[VerdictObservation]] = defaultdict(list)
    for verdict in verdicts:
        verdicts_by_group[(verdict.agent_role, verdict.runtime)].append(verdict)

    groups = sorted(set(costs_by_group) | set(verdicts_by_group))
    rows = [
        _summarize_group(
            agent_role,
            runtime,
            costs_by_group.get((agent_role, runtime), []),
            verdicts_by_group.get((agent_role, runtime), []),
            flip_threshold=flip_threshold,
            pass_regression_threshold=pass_regression_threshold,
        )
        for agent_role, runtime in groups
    ]
    logger.debug(f"summarize_cost_ab computed observations={observation_count} groups={len(rows)}")
    return CostABReport(
        status=CostABStatus.COMPUTED,
        observation_count=observation_count,
        min_n=min_n,
        rows=rows,
        note=(
            f"computed over {observation_count} cost+verdict observations "
            f"(>= minimum N={min_n}); flip rate + audit-pass-first-try are the "
            "primary discriminators, $/closed-wave governs the metered fallback "
            "path only (subscription auth is informational, not billed)"
        ),
    )


def _load_cost_observations(store: AbstractMetricsStore) -> list[CostObservation]:
    """Read ``dispatch_cost`` rows off the metrics store into observations.

    The ``dispatch_cost`` row keys spend to ``(wave_id, runtime, model)`` but
    carries no ``agent_role`` column; the per-role grouping the A/B needs is
    recovered by joining each cost row to the verdict rows for its
    ``(wave_id, runtime)`` in :func:`_join_observations`. This loader returns
    one untyped-role cost observation per priced row; the join stamps the
    role.

    A store with no ``telemetry_dispatch_costs`` rows (today's reality)
    yields an empty list -- the honest-empty path.

    Args:
        store: An initialised metrics store.

    Returns:
        The cost rows projected to tiered observations (role stamped later).
    """
    rows = store.fetch_all("telemetry_dispatch_costs", TelemetryDispatchCost)
    out: list[CostObservation] = []
    for row in rows:
        assert isinstance(row, TelemetryDispatchCost)
        tier = tier_for_model(row.model)
        if tier is None:
            logger.debug(
                f"_load_cost_observations skip envelope={row.envelope_id!r} "
                f"model={row.model!r} off-ladder"
            )
            continue
        # Role is unknown on the cost row; stamped during the verdict join.
        out.append(
            CostObservation(
                agent_role="?",
                runtime=row.runtime,
                model=row.model,
                tier=tier,
                wave_id=row.wave_id,
                cost_usd=row.cost_usd,
                priced=row.cost_usd > 0,
            )
        )
    return out


def _join_observations(
    state_path: Path,
    cost_rows: list[CostObservation],
) -> tuple[list[CostObservation], list[VerdictObservation]]:
    """Join verdict rows to cost rows on ``(wave_id, runtime)`` into observations.

    The role + tier a verdict was produced under are recovered from the
    ``dispatch_cost`` rows that share the verdict's ``(wave_id, runtime)``:
    the verdict's role is the cost row's role (the verdict header carries the
    role directly), and the verdict's tier is each tier that ``(wave_id,
    runtime)`` was dispatched on. A wave dispatched on two tiers yields the
    verdict at both tiers -- exactly the cheaper-vs-top overlap the flip rate
    needs. A verdict whose ``(wave_id, runtime)`` matches no priced cost row
    carries no tier and is dropped (per the module docstring's join caveat).

    Cost rows have their ``agent_role`` stamped from the joined verdict's
    role so the spend metric groups per role too; a cost row whose
    ``(wave_id, runtime)`` matches no verdict keeps role ``"?"`` and lands in
    its own group (the spend is still attributable to a runtime).

    Args:
        state_path: Path to ``state.json``; the role-report stores resolve
            under its sibling ``store/`` directory.
        cost_rows: Cost observations from :func:`_load_cost_observations`
            (role not yet stamped).

    Returns:
        A ``(costs, verdicts)`` pair of fully-typed observation lists.
    """
    tiers_by_key: dict[tuple[str | None, str], set[int]] = defaultdict(set)
    role_by_key: dict[tuple[str | None, str], str] = {}
    for cost in cost_rows:
        tiers_by_key[(cost.wave_id, cost.runtime)].add(cost.tier)

    verdict_obs: list[VerdictObservation] = []
    for report in iter_agent_reports(state_path):
        header = report.payload.header
        key = (header.base_id, header.runtime)
        tiers = tiers_by_key.get(key)
        if not tiers:
            continue
        role_by_key[key] = header.role.value
        for tier in sorted(tiers):
            verdict_obs.append(
                VerdictObservation(
                    agent_role=header.role.value,
                    runtime=header.runtime,
                    model=_model_for_tier(cost_rows, key, tier),
                    tier=tier,
                    wave_id=header.base_id,
                    attempt=header.attempt,
                    verdict=report.payload.body.verdict,
                )
            )

    costs: list[CostObservation] = []
    for cost in cost_rows:
        role = role_by_key.get((cost.wave_id, cost.runtime), cost.agent_role)
        costs.append(cost.model_copy(update={"agent_role": role}))
    return costs, verdict_obs


def _model_for_tier(
    cost_rows: list[CostObservation],
    key: tuple[str | None, str],
    tier: int,
) -> str:
    """Return a model id for ``key`` at ``tier`` from the cost rows.

    The verdict observation needs a concrete model id for its (non-empty)
    ``model`` field; any cost row matching the ``(wave_id, runtime)`` key at
    the tier supplies it. The caller only ever passes a ``(key, tier)`` that
    a cost row produced, so a match always exists.
    """
    for cost in cost_rows:
        if (cost.wave_id, cost.runtime) == key and cost.tier == tier:
            return cost.model
    # Unreachable on the real call path (tier came from a cost row for this
    # key); a defensive non-empty placeholder keeps the model field valid.
    return "unknown"


def compute_cost_ab(
    state_path: Path,
    *,
    db_kind: StoreBackend = "sqlite",
    min_n: int = MIN_COST_AB_N,
) -> CostABReport:
    """Read the ledger + verdict cohort off disk and compute the cost A/B.

    Thin store-reading entry: ``dispatch_cost`` rows are pulled off the
    metrics store and joined to the role-report verdict rows on
    ``(wave_id, runtime)`` (see :func:`_join_observations`), then handed to
    the pure :func:`summarize_cost_ab` reducer. No metric math lives here --
    the gate and the discriminator math stay in the reducer so they are
    testable without touching disk.

    Today this returns the honest-negative surface: the metrics store has no
    ``dispatch_cost`` rows (the metering emit fires only on a live priced
    spawn) and the verdict stores are empty, so the observation count is
    zero and the report is :attr:`CostABStatus.INSUFFICIENT_DATA`. A missing
    metrics DB is treated as an empty store (no rows), not an error.

    Args:
        state_path: Path to ``state.json``; the metrics DB and report stores
            resolve as its siblings.
        db_kind: Metrics-store backend (``"sqlite"`` default, ``"duckdb"``
            opt-in with SQLite fallback).
        min_n: Hard minimum observation count. Defaults to
            :data:`MIN_COST_AB_N`.

    Returns:
        A :class:`CostABReport`; honest-negative when the on-disk ledger +
        verdict cohort is below *min_n*.
    """
    db_path = metrics_db_path(state_path)
    if not db_path.exists():
        logger.debug(f"compute_cost_ab metrics_db={str(db_path)!r} present=false empty ledger")
        return summarize_cost_ab((), (), min_n=min_n)

    store = open_store(db_kind, db_path)
    try:
        store.init_schema()
        cost_rows = _load_cost_observations(store)
    finally:
        store.close()
    costs, verdicts = _join_observations(state_path, cost_rows)
    return summarize_cost_ab(tuple(costs), tuple(verdicts), min_n=min_n)


__all__ = [
    "DEFAULT_FLIP_THRESHOLD",
    "DEFAULT_PASS_REGRESSION_THRESHOLD",
    "MIN_COST_AB_N",
    "CostABReport",
    "CostABRow",
    "CostABStatus",
    "CostObservation",
    "TierRecommendation",
    "VerdictObservation",
    "compute_cost_ab",
    "recommend_tier",
    "summarize_cost_ab",
]
