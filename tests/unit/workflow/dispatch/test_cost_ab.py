"""Verdict-agreement cost A/B tests.

The cost A/B is an *honest-negative* projection: with an empty
``dispatch_cost`` ledger and empty verdict stores it refuses to compute
rather than fabricate a flip rate or a ``$`` figure. These tests pin both
layers:

- the pure :func:`summarize_cost_ab` reducer + the :func:`recommend_tier`
  decision rule across the min-N boundary, the keep-cheaper / bump-to-top
  branches, the per-``(agent_role, runtime)`` $/closed-wave sum (claude vs
  codex grouped apart), the fabricated-free guard, and the validation /
  error paths; and
- the store-reading :func:`compute_cost_ab` entry, which must surface the
  honest-negative refusal when the on-disk ledger + verdict cohort is empty
  (today's real result) and group spend per role + runtime once real
  ``dispatch_cost`` + verdict rows land.

The store tests build ``dispatch_cost`` rows through the canonical
``DispatchCostPayload`` -> ``Envelope`` -> projector path and verdict rows
through the agent-report store, never hand-rolled JSON.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    Confidence,
    StoreKind,
)
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportHeader,
    AgentReportPayload,
    ExecutorReportBody,
    report_record_id,
    store_kind_for_role,
)
from eawf.kernel.store.kinds.events.dispatch_cost import DispatchCostPayload
from eawf.kernel.store.paths import store_path
from eawf.observability.telemetry.projector import RebuildMode, SourceSpec, rebuild
from eawf.observability.telemetry.sources import DispatchCostSessionSource
from eawf.observability.telemetry.store.base import metrics_db_path, open_store
from eawf.workflow.dispatch import (
    DEFAULT_FLIP_THRESHOLD,
    MIN_COST_AB_N,
    CostABReport,
    CostABRow,
    CostABStatus,
    CostObservation,
    TierRecommendation,
    VerdictObservation,
    compute_cost_ab,
    recommend_tier,
    summarize_cost_ab,
    tier_for_model,
)
from eawf.workflow.dispatch.routing import TOP_TIER_INDEX

_MODEL_OPUS = "claude-opus-4-8"
_MODEL_HAIKU = "claude-haiku-4-5"
_MODEL_CODEX_TOP = "gpt-5.5"
_MODEL_CODEX_CHEAP = "gpt-5.3-codex-spark"


# --------------------------------------------------------------------------- #
# Observation builders (pure-reducer fixtures).
# --------------------------------------------------------------------------- #


def _cost(
    *,
    agent_role: str = "executor",
    runtime: str = "claude",
    model: str = _MODEL_OPUS,
    tier: int = TOP_TIER_INDEX,
    wave_id: str | None = "P00-W01",
    cost_usd: str = "0.10",
    priced: bool = True,
) -> CostObservation:
    """Build one cost observation with sensible defaults."""
    return CostObservation(
        agent_role=agent_role,
        runtime=runtime,
        model=model,
        tier=tier,
        wave_id=wave_id,
        cost_usd=Decimal(cost_usd),
        priced=priced,
    )


def _verdict(
    *,
    agent_role: str = "executor",
    runtime: str = "claude",
    model: str = _MODEL_OPUS,
    tier: int = TOP_TIER_INDEX,
    wave_id: str = "P00-W01",
    attempt: int = 1,
    verdict: AgentReportVerdict = AgentReportVerdict.PASS,
) -> VerdictObservation:
    """Build one verdict observation with sensible defaults."""
    return VerdictObservation(
        agent_role=agent_role,
        runtime=runtime,
        model=model,
        tier=tier,
        wave_id=wave_id,
        attempt=attempt,
        verdict=verdict,
    )


def _min_n_filler() -> tuple[CostObservation, ...]:
    """Return enough off-topic cost rows to clear the min-N gate.

    These rows belong to an unrelated role/runtime group so they pad the
    observation count without colouring the group-under-test's metrics.
    """
    return tuple(
        _cost(agent_role="filler", wave_id=f"P99-W{i:02d}", cost_usd="0.01")
        for i in range(MIN_COST_AB_N)
    )


# --------------------------------------------------------------------------- #
# tier_for_model — the classifier the A/B keys cheaper-vs-top on.
# --------------------------------------------------------------------------- #


def test_tier_for_model_exact_claude_tiers() -> None:
    """Each claude ladder id classifies onto its tier index."""
    assert tier_for_model(_MODEL_HAIKU) == 0
    assert tier_for_model("claude-sonnet-4-6") == 1
    assert tier_for_model(_MODEL_OPUS) == TOP_TIER_INDEX


def test_tier_for_model_codex_tiers_separate_scale() -> None:
    """The codex ladder maps onto the same 0..top scale as claude."""
    assert tier_for_model(_MODEL_CODEX_CHEAP) == 0
    assert tier_for_model(_MODEL_CODEX_TOP) == TOP_TIER_INDEX


def test_tier_for_model_longest_prefix_for_dated_variant() -> None:
    """A dated / suffixed variant classifies via its longest-prefix tier id."""
    assert tier_for_model("gpt-5.5-2026") == TOP_TIER_INDEX
    assert tier_for_model("claude-opus-4-8-20260601") == TOP_TIER_INDEX


def test_tier_for_model_unknown_returns_none() -> None:
    """An off-ladder model id classifies to None (so it is dropped, not mis-bucketed)."""
    assert tier_for_model("some-unrouted-model") is None


# --------------------------------------------------------------------------- #
# summarize_cost_ab — the refuse-to-compute gate (honest-empty path).
# --------------------------------------------------------------------------- #


def test_summarize_cost_ab_empty_refuses_to_compute() -> None:
    """Empty cost + verdict observations yield INSUFFICIENT, no rows, no $.

    This is the honest-empty path -- today's real result over a zero-row
    ledger and zero-row verdict stores.
    """
    report = summarize_cost_ab((), ())

    assert report.status is CostABStatus.INSUFFICIENT_DATA
    assert report.observation_count == 0
    assert report.rows == []
    assert "insufficient data" in report.note
    assert f"N={MIN_COST_AB_N}" in report.note


def test_summarize_cost_ab_below_min_n_refuses() -> None:
    """A cohort one short of the gate still refuses -- no fabricated metric."""
    costs = tuple(_cost(wave_id=f"P00-W{i:02d}") for i in range(MIN_COST_AB_N - 1))
    report = summarize_cost_ab(costs, ())

    assert report.status is CostABStatus.INSUFFICIENT_DATA
    assert report.observation_count == MIN_COST_AB_N - 1
    assert report.rows == []


def test_summarize_cost_ab_below_min_n_never_emits_dollar_figure() -> None:
    """The fabricated-free guard: below min-N no $/closed-wave is ever emitted.

    Even with real priced cost rows present, an under-N cohort must surface
    zero rows so no caller can read a $ number out of it.
    """
    costs = (
        _cost(wave_id="P00-W01", cost_usd="9.99"),
        _cost(wave_id="P00-W02", cost_usd="9.99"),
    )
    report = summarize_cost_ab(costs, ())

    assert report.status is CostABStatus.INSUFFICIENT_DATA
    assert report.rows == []
    # No row -> no cost_per_closed_wave_* field anywhere to read.
    assert all(not isinstance(r, CostABRow) for r in report.rows)


# --------------------------------------------------------------------------- #
# summarize_cost_ab — the decision rule over a constructed above-N cohort.
# --------------------------------------------------------------------------- #


def test_summarize_cost_ab_zero_flips_steady_pass_keeps_cheaper() -> None:
    """A cohort with zero flips + steady first-try pass recommends keep-cheaper.

    Each wave is seen by both the cheaper (haiku) and top (opus) tier with the
    same passing outcome -> flip rate 0.0; first-try pass holds at 1.0 on both
    tiers -> the cheaper tier is kept.
    """
    verdicts: list[VerdictObservation] = []
    for i in range(MIN_COST_AB_N):
        wave = f"P00-W{i:02d}"
        verdicts.append(_verdict(wave_id=wave, tier=0, model=_MODEL_HAIKU))
        verdicts.append(_verdict(wave_id=wave, tier=TOP_TIER_INDEX, model=_MODEL_OPUS))

    report = summarize_cost_ab((), tuple(verdicts))

    assert report.status is CostABStatus.COMPUTED
    row = next(r for r in report.rows if r.agent_role == "executor")
    assert row.flip_rate == pytest.approx(0.0)
    assert row.shared_wave_count == MIN_COST_AB_N
    assert row.cheaper_pass_first_try == pytest.approx(1.0)
    assert row.top_pass_first_try == pytest.approx(1.0)
    assert row.recommendation is TierRecommendation.KEEP_CHEAPER


def test_summarize_cost_ab_flips_above_threshold_bumps_to_top() -> None:
    """A cohort whose cheaper tier flips often vs opus recommends bump-to-top.

    Every shared wave: the cheaper tier FAILS where the top tier PASSES ->
    flip rate 1.0, far above the threshold -> bump back to the top tier.
    """
    verdicts: list[VerdictObservation] = []
    for i in range(MIN_COST_AB_N):
        wave = f"P00-W{i:02d}"
        verdicts.append(
            _verdict(wave_id=wave, tier=0, model=_MODEL_HAIKU, verdict=AgentReportVerdict.FAIL)
        )
        verdicts.append(
            _verdict(
                wave_id=wave,
                tier=TOP_TIER_INDEX,
                model=_MODEL_OPUS,
                verdict=AgentReportVerdict.PASS,
            )
        )

    report = summarize_cost_ab((), tuple(verdicts))

    assert report.status is CostABStatus.COMPUTED
    row = next(r for r in report.rows if r.agent_role == "executor")
    assert row.flip_rate == pytest.approx(1.0)
    assert row.recommendation is TierRecommendation.BUMP_TO_TOP


def test_summarize_cost_ab_no_top_baseline_is_insufficient_recommendation() -> None:
    """A group with cheaper-tier verdicts but no top-tier baseline cannot decide.

    The decision rule never recommends keeping a cheaper tier it has not
    actually compared against the top tier; with no shared wave the flip rate
    is None and the recommendation is INSUFFICIENT even above the gate.
    """
    verdicts = tuple(
        _verdict(wave_id=f"P00-W{i:02d}", tier=0, model=_MODEL_HAIKU) for i in range(MIN_COST_AB_N)
    )
    report = summarize_cost_ab((), verdicts)

    assert report.status is CostABStatus.COMPUTED
    row = next(r for r in report.rows if r.agent_role == "executor")
    assert row.flip_rate is None
    assert row.shared_wave_count == 0
    assert row.recommendation is TierRecommendation.INSUFFICIENT


def test_summarize_cost_ab_first_try_regression_bumps_to_top() -> None:
    """A cheaper tier that agrees on outcome but regresses first-try bumps up.

    The cheaper tier passes the wave (no flip) but only on a retry: its
    first-attempt verdict fails while the top tier passes first try, so the
    extra re-work pushes the recommendation to bump-to-top.
    """
    verdicts: list[VerdictObservation] = []
    for i in range(MIN_COST_AB_N):
        wave = f"P00-W{i:02d}"
        # Cheaper tier: attempt 1 fails, attempt 2 passes (outcome = pass, but
        # first-try pass rate collapses to 0.0).
        verdicts.append(
            _verdict(
                wave_id=wave,
                tier=0,
                model=_MODEL_HAIKU,
                attempt=1,
                verdict=AgentReportVerdict.FAIL,
            )
        )
        verdicts.append(
            _verdict(
                wave_id=wave,
                tier=0,
                model=_MODEL_HAIKU,
                attempt=2,
                verdict=AgentReportVerdict.PASS,
            )
        )
        # Top tier: passes first try.
        verdicts.append(_verdict(wave_id=wave, tier=TOP_TIER_INDEX, model=_MODEL_OPUS, attempt=1))

    report = summarize_cost_ab((), tuple(verdicts))

    row = next(r for r in report.rows if r.agent_role == "executor")
    assert row.flip_rate == pytest.approx(0.0)
    assert row.cheaper_pass_first_try == pytest.approx(0.0)
    assert row.top_pass_first_try == pytest.approx(1.0)
    assert row.recommendation is TierRecommendation.BUMP_TO_TOP


# --------------------------------------------------------------------------- #
# summarize_cost_ab — $/closed-wave per role + per runtime (the codex leg).
# --------------------------------------------------------------------------- #


def test_summarize_cost_ab_cost_per_closed_wave_sums_per_role() -> None:
    """$/closed-wave divides summed priced spend by distinct closed waves.

    Two opus dispatches on wave W01 ($0.10 + $0.20) plus one on W02 ($0.30)
    sum to $0.60 over 2 distinct waves -> $0.30/closed-wave on the top tier.
    """
    costs = (
        _cost(wave_id="P00-W01", cost_usd="0.10"),
        _cost(wave_id="P00-W01", cost_usd="0.20"),
        _cost(wave_id="P00-W02", cost_usd="0.30"),
        *_min_n_filler(),
    )
    report = summarize_cost_ab(costs, ())

    row = next(r for r in report.rows if r.agent_role == "executor")
    assert row.cost_per_closed_wave_top == Decimal("0.30")
    assert row.cost_per_closed_wave_cheaper is None


def test_summarize_cost_ab_codex_runtime_groups_separately_from_claude() -> None:
    """The codex leg groups apart from claude even at the same role.

    Same role (executor) but two runtimes: the claude rows and the codex
    rows land in distinct ``(agent_role, runtime)`` groups, each with its own
    $/closed-wave.
    """
    costs = (
        _cost(runtime="claude", model=_MODEL_OPUS, wave_id="P00-W01", cost_usd="0.40"),
        _cost(
            runtime="codex",
            model=_MODEL_CODEX_TOP,
            wave_id="P00-W01",
            cost_usd="0.10",
        ),
        *_min_n_filler(),
    )
    report = summarize_cost_ab(costs, ())

    claude_row = next(
        r for r in report.rows if r.agent_role == "executor" and r.runtime == "claude"
    )
    codex_row = next(r for r in report.rows if r.agent_role == "executor" and r.runtime == "codex")
    assert claude_row.cost_per_closed_wave_top == Decimal("0.40")
    assert codex_row.cost_per_closed_wave_top == Decimal("0.10")


def test_summarize_cost_ab_unpriced_row_excluded_from_dollar_figure() -> None:
    """An unpriceable spawn (priced is False) does not deflate $/closed-wave.

    The $0 fallback row for W02 is excluded, so the figure is the single
    priced W01 row's $0.50 over 1 wave -- not $0.25 over 2.
    """
    costs = (
        _cost(wave_id="P00-W01", cost_usd="0.50", priced=True),
        _cost(wave_id="P00-W02", cost_usd="0", priced=False),
        *_min_n_filler(),
    )
    report = summarize_cost_ab(costs, ())

    row = next(r for r in report.rows if r.agent_role == "executor")
    assert row.cost_per_closed_wave_top == Decimal("0.50")


def test_summarize_cost_ab_interactive_cost_excluded_from_per_wave() -> None:
    """A wave-less (interactive) cost row never contributes to $/closed-wave."""
    costs = (
        _cost(wave_id="P00-W01", cost_usd="0.20"),
        _cost(wave_id=None, cost_usd="5.00"),
        *_min_n_filler(),
    )
    report = summarize_cost_ab(costs, ())

    row = next(r for r in report.rows if r.agent_role == "executor")
    assert row.cost_per_closed_wave_top == Decimal("0.20")


# --------------------------------------------------------------------------- #
# recommend_tier — the pure decision rule directly.
# --------------------------------------------------------------------------- #


def test_recommend_tier_none_flip_rate_is_insufficient() -> None:
    """No flip rate (no baseline) -> never keep, the call is insufficient."""
    assert (
        recommend_tier(
            flip_rate=None,
            cheaper_pass_first_try=1.0,
            top_pass_first_try=1.0,
        )
        is TierRecommendation.INSUFFICIENT
    )


def test_recommend_tier_flip_at_threshold_keeps_cheaper() -> None:
    """A flip rate exactly at the threshold is still within bounds -> keep."""
    assert (
        recommend_tier(
            flip_rate=DEFAULT_FLIP_THRESHOLD,
            cheaper_pass_first_try=1.0,
            top_pass_first_try=1.0,
        )
        is TierRecommendation.KEEP_CHEAPER
    )


def test_recommend_tier_missing_first_try_rates_keeps_on_low_flip() -> None:
    """With no first-try data but a low flip rate, the rule keeps the cheaper tier.

    The first-try regression check is skipped when either side's rate is None;
    a low flip rate alone clears keep-cheaper.
    """
    assert (
        recommend_tier(
            flip_rate=0.0,
            cheaper_pass_first_try=None,
            top_pass_first_try=None,
        )
        is TierRecommendation.KEEP_CHEAPER
    )


def test_recommend_tier_rejects_out_of_range_flip_threshold() -> None:
    """A flip threshold off the [0,1] rate scale cannot gate a rate."""
    with pytest.raises(ValueError, match="flip_threshold"):
        recommend_tier(
            flip_rate=0.0,
            cheaper_pass_first_try=None,
            top_pass_first_try=None,
            flip_threshold=1.5,
        )


def test_recommend_tier_rejects_out_of_range_pass_regression_threshold() -> None:
    """A pass-regression threshold off the [0,1] scale is rejected."""
    with pytest.raises(ValueError, match="pass_regression_threshold"):
        recommend_tier(
            flip_rate=0.0,
            cheaper_pass_first_try=None,
            top_pass_first_try=None,
            pass_regression_threshold=-0.1,
        )


# --------------------------------------------------------------------------- #
# summarize_cost_ab — error paths + model validation.
# --------------------------------------------------------------------------- #


def test_summarize_cost_ab_rejects_non_positive_min_n() -> None:
    """A zero/negative gate would defeat the refuse-to-compute guarantee."""
    with pytest.raises(ValueError, match="min_n must be >= 1"):
        summarize_cost_ab((), (), min_n=0)


def test_cost_observation_forbids_extra_fields() -> None:
    """CostObservation is extra='forbid' -- a drifted field fails fast."""
    with pytest.raises(ValueError):
        CostObservation.model_validate(
            {
                "agent_role": "executor",
                "runtime": "claude",
                "model": _MODEL_OPUS,
                "tier": TOP_TIER_INDEX,
                "wave_id": "P00-W01",
                "cost_usd": "0.10",
                "priced": True,
                "unexpected": "x",
            }
        )


def test_verdict_observation_rejects_zero_attempt() -> None:
    """The attempt number is 1-based; zero fails validation."""
    with pytest.raises(ValueError):
        VerdictObservation(
            agent_role="executor",
            runtime="claude",
            model=_MODEL_OPUS,
            tier=TOP_TIER_INDEX,
            wave_id="P00-W01",
            attempt=0,
            verdict=AgentReportVerdict.PASS,
        )


def test_cost_ab_report_forbids_negative_observation_count() -> None:
    """The report's observation count is non-negative."""
    with pytest.raises(ValueError):
        CostABReport(
            status=CostABStatus.INSUFFICIENT_DATA,
            observation_count=-1,
            min_n=MIN_COST_AB_N,
            rows=[],
            note="x",
        )


# --------------------------------------------------------------------------- #
# compute_cost_ab — store-reading entry (honest-empty + populated).
# --------------------------------------------------------------------------- #


def _dispatch_cost_envelope(
    envelope_id: str,
    *,
    wave_id: str,
    runtime: str,
    model: str,
    cost_usd: str,
    created_at: datetime,
) -> Envelope:
    """Wrap a canonical ``DispatchCostPayload`` in an event envelope."""
    payload = DispatchCostPayload(
        timestamp=created_at,
        wave_id=wave_id,
        attempt_id=f"{envelope_id}-att",
        runtime=runtime,  # type: ignore[arg-type]
        model=model,
        input_tokens=1000,
        output_tokens=200,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        cost_usd=Decimal(cost_usd),
        pricing_version="2026-05-01",
    )
    return Envelope(
        id=envelope_id,
        kind=StoreKind.EVENT,
        scope_id=wave_id,
        created_at=created_at,
        summary=f"dispatch_cost wave={wave_id}",
        payload=payload.model_dump(mode="json"),
    )


def _write_dispatch_cost(state_path: Path, envelope: Envelope) -> None:
    """Append a ``dispatch_cost`` event to the canonical event store."""
    append_envelope(store_path(state_path, StoreKind.EVENT), envelope)


def _project_metrics(state_path: Path) -> None:
    """Build the metrics DB from the event store's dispatch_cost rows."""
    store = open_store("sqlite", metrics_db_path(state_path))
    try:
        store.init_schema()
        rebuild(
            store,
            [
                SourceSpec(
                    source=DispatchCostSessionSource(),
                    root=state_path,
                    project_id="P00",
                )
            ],
            mode=RebuildMode.FULL,
        )
    finally:
        store.close()


def _write_executor_verdict(
    state_path: Path,
    *,
    wave_id: str,
    runtime: str,
    verdict: AgentReportVerdict,
    index: int,
) -> None:
    """Append one executor-report verdict to the on-disk store."""
    role = AgentSessionRole.EXECUTOR
    report_id = report_record_id(role=role, base_id=wave_id, attempt=1)
    moment = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index)
    body = ExecutorReportBody(
        verdict=verdict,
        confidence=Confidence.HIGH,
        summary="recorded executor verdict",
        wave_id=wave_id,
        outcome="implementation outcome",
    )
    header = AgentReportHeader(
        report_id=report_id,
        role=role,
        session_id=f"S{index:02d}",
        scope_id=wave_id,
        base_id=wave_id,
        attempt=1,
        runtime=runtime,
        generated_at=moment,
        summary="recorded executor verdict",
    )
    payload = AgentReportPayload(header=header, body=body)
    envelope = Envelope(
        id=report_id,
        kind=store_kind_for_role(role),
        scope_id=wave_id,
        created_at=moment,
        updated_at=None,
        summary="recorded executor verdict",
        payload=payload.model_dump(mode="json"),
    )
    append_envelope(store_path(state_path, store_kind_for_role(role)), envelope)


def test_compute_cost_ab_empty_stores_refuses_to_compute(tmp_path: Path) -> None:
    """Empty ledger + empty verdict stores yield the honest-negative surface.

    This is the load-bearing criterion: the ``dispatch_cost`` ledger and the
    verdict stores both start empty (the metering emit fires only on a live
    priced spawn), so the A/B must refuse rather than fabricate a number.
    """
    state_path = tmp_path / "state.json"
    report = compute_cost_ab(state_path)

    assert report.status is CostABStatus.INSUFFICIENT_DATA
    assert report.observation_count == 0
    assert report.rows == []


def test_compute_cost_ab_missing_metrics_db_is_empty_not_error(tmp_path: Path) -> None:
    """A wholly absent metrics DB is treated as an empty ledger, not an error."""
    state_path = tmp_path / "state.json"
    assert not metrics_db_path(state_path).exists()

    report = compute_cost_ab(state_path)

    assert report.status is CostABStatus.INSUFFICIENT_DATA


def test_compute_cost_ab_populated_groups_spend_per_role_and_runtime(
    tmp_path: Path,
) -> None:
    """Real dispatch_cost + verdict rows compute spend per role + runtime.

    Wave W01 is dispatched on claude-opus and codex-gpt5-codex; an executor
    verdict closes it on each runtime. The A/B clears the gate and reports a
    $/closed-wave on the top tier for both the claude and codex groups, which
    group separately -- the cross-vendor leg.
    """
    state_path = tmp_path / "state.json"
    base = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)

    cost_specs = [
        ("EV-1", "P00-W01", "claude", _MODEL_OPUS, "0.40"),
        ("EV-2", "P00-W02", "claude", _MODEL_OPUS, "0.20"),
        ("EV-3", "P00-W01", "codex", _MODEL_CODEX_TOP, "0.10"),
    ]
    for i, (eid, wave, runtime, model, cost) in enumerate(cost_specs):
        _write_dispatch_cost(
            state_path,
            _dispatch_cost_envelope(
                eid,
                wave_id=wave,
                runtime=runtime,
                model=model,
                cost_usd=cost,
                created_at=base + timedelta(minutes=i),
            ),
        )
    _project_metrics(state_path)

    _write_executor_verdict(
        state_path, wave_id="P00-W01", runtime="claude", verdict=AgentReportVerdict.PASS, index=1
    )
    _write_executor_verdict(
        state_path, wave_id="P00-W02", runtime="claude", verdict=AgentReportVerdict.PASS, index=2
    )
    _write_executor_verdict(
        state_path, wave_id="P00-W01", runtime="codex", verdict=AgentReportVerdict.PASS, index=3
    )

    report = compute_cost_ab(state_path)

    assert report.status is CostABStatus.COMPUTED
    claude_row = next(
        r for r in report.rows if r.agent_role == "executor" and r.runtime == "claude"
    )
    codex_row = next(r for r in report.rows if r.agent_role == "executor" and r.runtime == "codex")
    # claude top-tier: $0.40 + $0.20 over 2 waves = $0.30.
    assert claude_row.cost_per_closed_wave_top == Decimal("0.30")
    # codex top-tier: $0.10 over 1 wave.
    assert codex_row.cost_per_closed_wave_top == Decimal("0.10")


def test_compute_cost_ab_drops_verdict_without_matching_cost_row(tmp_path: Path) -> None:
    """A verdict whose (wave, runtime) matches no priced cost row is dropped.

    Only the join-able verdict (W01/claude) yields a tier-keyed observation;
    the orphan verdict (W02/claude, no cost row) carries no tier and so does
    not appear -- it cannot say which model produced it. Below the gate the
    report still refuses, proving the orphan did not pad the count.
    """
    state_path = tmp_path / "state.json"
    _write_dispatch_cost(
        state_path,
        _dispatch_cost_envelope(
            "EV-1",
            wave_id="P00-W01",
            runtime="claude",
            model=_MODEL_OPUS,
            cost_usd="0.40",
            created_at=datetime(2026, 5, 30, 12, 0, tzinfo=UTC),
        ),
    )
    _project_metrics(state_path)
    _write_executor_verdict(
        state_path, wave_id="P00-W01", runtime="claude", verdict=AgentReportVerdict.PASS, index=1
    )
    _write_executor_verdict(
        state_path, wave_id="P00-W02", runtime="claude", verdict=AgentReportVerdict.PASS, index=2
    )

    report = compute_cost_ab(state_path)

    # 1 cost obs + 1 join-able verdict obs = 2 observations, below the gate.
    assert report.observation_count == 2
    assert report.status is CostABStatus.INSUFFICIENT_DATA
