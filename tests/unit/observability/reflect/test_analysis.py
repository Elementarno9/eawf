"""Personal baselines, the finding ladder, noise and usage over synthetic history."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from eawf.observability.reflect.analysis import (
    ANALYSIS_PREFIX,
    COHORT_LABELS,
    METRIC_NOUNS,
    MIN_BASELINE,
    Baseline,
    CohortRef,
    CohortRung,
    HistorySession,
    IndexedHistory,
    Metric,
    NoiseReason,
    SessionClass,
    SessionTitle,
    Severity,
    StructuralIssue,
    analyze_sessions,
    assess_metric,
    history_sessions,
    load_indexed_history,
    prior_baseline,
    write_analysis,
)
from eawf.observability.reflect.generations import OpaqueBlobError
from eawf.observability.reflect.preamble import InstructionOrigin
from eawf.observability.reflect.rates import PricingQuality
from eawf.observability.reflect.titles import TitleSource
from eawf.observability.telemetry.models import (
    DurationKind,
    ObservedDuration,
    ObservedRequest,
    ObservedSession,
    PriceSourceKind,
)
from eawf.observability.telemetry.sources.session_history import SessionDetail
from eawf.observability.telemetry.store import SqliteMetricsStore

_T0 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
_NOW = datetime(2026, 11, 1, tzinfo=UTC)


def _ref(index: int) -> str:
    return f"vsid-{index:032x}"


def _session(
    index: int,
    *,
    total: float = 1_000.0,
    project: str = "cwd-a",
    runtime: str = "claude",
    model: str = "claude-opus-4-7",
    start: datetime | None = None,
    hours: float = 1.0,
    steps: float = 10.0,
    tools: float | None = 5.0,
    extra: dict[Metric, float] | None = None,
    **fields: Any,
) -> HistorySession:
    started = start if start is not None else _T0 + timedelta(days=index)
    metrics: dict[Metric, float] = {Metric.TOTAL_TOKENS: total, Metric.MODEL_STEPS: steps}
    if tools is not None:
        metrics[Metric.TOOL_CALLS] = tools
    metrics.update(extra or {})
    values: dict[str, Any] = {
        "session_ref": _ref(index),
        "runtime": runtime,
        "project_key": project,
        "project_label": "SYN",
        "started_at": started,
        "ended_at": started + timedelta(hours=hours),
        "model": model,
        "model_family": "claude-opus",
        "orchestrated": False,
        "metrics": metrics,
        "cost_usd": None,
        "pricing_quality": PricingQuality.VERIFIED,
    }
    values.update(fields)
    return HistorySession.model_validate(values)


def _types(row: Any) -> set[str]:
    return {finding.finding_type for finding in row.findings}


def _row(analysis: Any, index: int) -> Any:
    return next(row for row in analysis.sessions if row.session_ref == _ref(index))


# ---- CR-741: prior-only baselines and the cohort ladder -------------------------------


def test_prior_only_baseline() -> None:
    prior = [_session(index, total=1_000.0 + index) for index in range(10)]
    current = _session(10, total=50_000.0)
    # Started before the current session but still running when it started.
    overlapping = _session(
        11, total=9_000_000.0, start=current.started_at - timedelta(hours=1), hours=5
    )
    later = [_session(12 + index, total=9_000_000.0) for index in range(5)]
    population = [current, overlapping, *later, *prior]

    baseline = prior_baseline(current, population, Metric.TOTAL_TOKENS)

    assert baseline is not None
    assert baseline.members == tuple(_ref(index) for index in range(10))
    assert current.session_ref not in baseline.members
    assert overlapping.session_ref not in baseline.members
    assert not set(baseline.members) & {row.session_ref for row in later}
    assert baseline.cohort.rung is CohortRung.PROJECT_EXACT
    assert baseline.cohort.prior_only is True
    assessment = assess_metric(Metric.TOTAL_TOKENS, 50_000.0, baseline)
    assert assessment.median == pytest.approx(1_004.5)
    assert assessment.mad > 0
    assert assessment.robust_method == "mad"

    analysis = analyze_sessions(population, requests=(), now=_NOW)
    flagged = _row(analysis, 10)
    anomaly = next(f for f in flagged.findings if f.finding_type == "total_tokens_anomaly")
    assert anomaly.severity is Severity.ANOMALY
    assert anomaly.cohort is not None
    assert anomaly.cohort.rung is CohortRung.PROJECT_EXACT
    assert anomaly.cohort.sample_count == 10

    # Seven prior sessions: notices only, whatever the value.
    cold = [_session(index, total=1_000.0 + index) for index in range(7)]
    spike = _session(7, total=10_000_000.0)
    cold_row = _row(analyze_sessions([*cold, spike], requests=(), now=_NOW), 7)
    assert {f.severity for f in cold_row.findings} == {Severity.NOTICE}
    assert "baseline_cold_start" in _types(cold_row)
    assert cold_row.anomalies == ()

    # A sparse project falls back to the user's personal history, and says so.
    elsewhere = [
        _session(index, total=1_000.0 + index, project=f"cwd-{index}") for index in range(10)
    ]
    newcomer = _session(10, total=50_000.0, project="cwd-new")
    fallback = prior_baseline(newcomer, [*elsewhere, newcomer], Metric.TOTAL_TOKENS)
    assert fallback is not None
    assert fallback.cohort.rung is CohortRung.PERSONAL_EXACT
    finding = next(
        f
        for f in _row(analyze_sessions([*elsewhere, newcomer], requests=(), now=_NOW), 10).findings
        if f.finding_type == "total_tokens_anomaly"
    )
    assert finding.cohort is not None
    assert finding.cohort.rung is CohortRung.PERSONAL_EXACT
    assert finding.cohort.label == COHORT_LABELS[CohortRung.PERSONAL_EXACT]


def test_prior_baseline_walks_the_ladder_to_user_wide() -> None:
    others = [
        _session(index, total=1_000.0 + index, project=f"cwd-{index}", runtime="codex", model="gpt")
        for index in range(9)
    ]
    current = _session(9, total=50_000.0, project="cwd-z")

    baseline = prior_baseline(current, [*others, current], Metric.TOTAL_TOKENS)

    assert baseline is not None
    assert baseline.cohort.rung is CohortRung.PERSONAL_WORKFLOW
    assert baseline.cohort.sample_count == 9


def test_prior_baseline_prefers_a_populated_narrow_rung() -> None:
    project = [_session(index, total=1_000.0) for index in range(20)]
    wider = [_session(20 + index, total=1_000.0, project="cwd-b") for index in range(30)]
    current = _session(60, total=1_000.0)

    baseline = prior_baseline(current, [*project, *wider, current], Metric.TOTAL_TOKENS)

    assert baseline is not None
    assert baseline.cohort.rung is CohortRung.PROJECT_EXACT
    assert baseline.cohort.sample_count == 20


def test_prior_baseline_without_a_start_time_is_none() -> None:
    current = _session(1, start=None, started_at=None, ended_at=None)

    assert prior_baseline(current, [_session(0)], Metric.TOTAL_TOKENS) is None
    row = _row(analyze_sessions([current], requests=(), now=_NOW), 1)
    assert "baseline_chronology_unavailable" in _types(row)


def test_prior_baseline_leaves_out_sessions_that_never_did_the_thing() -> None:
    zeros = [_session(index, extra={Metric.COMPACTION_COUNT: 0.0}) for index in range(10)]
    current = _session(10, extra={Metric.COMPACTION_COUNT: 3.0})

    baseline = prior_baseline(current, [*zeros, current], Metric.COMPACTION_COUNT)

    assert baseline is not None
    assert baseline.members == ()


def test_prior_baseline_prices_cost_against_verified_sessions_only() -> None:
    priced = [_session(i, extra={Metric.COST_USD: 1.0}) for i in range(4)]
    placeholder = [
        _session(4 + i, extra={Metric.COST_USD: 1.0}, pricing_quality=PricingQuality.UNAVAILABLE)
        for i in range(4)
    ]
    current = _session(9, extra={Metric.COST_USD: 9.0})

    baseline = prior_baseline(current, [*priced, *placeholder, current], Metric.COST_USD)

    assert baseline is not None
    assert baseline.members == tuple(_ref(i) for i in range(4))


# ---- the statistics -------------------------------------------------------------------


def _baseline(values: list[float]) -> Baseline:
    return Baseline(
        cohort=CohortRef(
            rung=CohortRung.USER_WIDE,
            label=COHORT_LABELS[CohortRung.USER_WIDE],
            dimensions=(),
            pool="observed_history",
            sample_count=len(values),
        ),
        members=tuple(_ref(i) for i in range(len(values))),
        values=tuple(values),
    )


def test_assess_metric_refuses_an_empty_baseline() -> None:
    with pytest.raises(ValueError, match="no value"):
        assess_metric(Metric.TOTAL_TOKENS, 1.0, _baseline([]))


def test_assess_metric_single_value_never_raises() -> None:
    assessment = assess_metric(Metric.TOTAL_TOKENS, 1_000_000.0, _baseline([1_000.0]))

    assert assessment.raised is False
    assert assessment.robust_method == "zero_dispersion"
    assert assessment.robust_z is None


def test_assess_metric_falls_back_to_the_interquartile_range() -> None:
    values = [1_000.0] * 15 + [900.0, 925.0, 950.0, 975.0, 1_025.0]

    assessment = assess_metric(Metric.TOTAL_TOKENS, 10_000.0, _baseline(values))

    assert assessment.robust_method in {"iqr", "zero_dispersion"}
    assert assessment.mad == pytest.approx(0.0)
    assert assessment.raised is True


def test_assess_metric_practical_floor_blocks_a_tiny_increase() -> None:
    values = [1_000.0 + i % 3 for i in range(20)]

    assert assess_metric(Metric.TOTAL_TOKENS, 1_050.0, _baseline(values)).raised is False


def test_assess_metric_zero_centred_cohort_never_raises() -> None:
    assessment = assess_metric(Metric.CHILD_COUNT, 9.0, _baseline([0.0] * 20))

    assert assessment.median == pytest.approx(0.0)
    assert assessment.practical_ratio is None
    assert assessment.raised is False


def test_assess_metric_at_the_seven_and_eight_sample_floor() -> None:
    seven = assess_metric(Metric.TOTAL_TOKENS, 10_000.0, _baseline([1_000.0 + i for i in range(7)]))
    eight = assess_metric(Metric.TOTAL_TOKENS, 10_000.0, _baseline([1_000.0 + i for i in range(8)]))

    assert seven.raised is False
    assert eight.raised is True


# ---- CR-742: the finding ladder and the noise class ----------------------------------


def test_finding_ladder_and_noise() -> None:
    usual = [_session(index, total=1_000.0 + index) for index in range(10)]
    spike = _session(10, total=50_000.0)
    churn = _session(11, total=60_000.0, extra={Metric.COMPACTION_COUNT: 4.0})
    broken = _session(12, issues=(StructuralIssue.TIMESTAMP_REGRESSION,))
    noise = _session(13, total=500_000.0, steps=1.0, tools=0.0)
    review = _session(14, total=7_000.0, model="codex-auto-review", runtime="codex")
    short = _session(15, first_input_chars=1, steps=6.0)
    after = _session(16, total=1_200.0)
    population = [*usual, spike, churn, broken, noise, review, short, after]

    analysis = analyze_sessions(population, requests=(), now=_NOW)

    severities = {f.severity for row in analysis.sessions for f in row.findings}
    assert severities == set(Severity)
    for row in analysis.sessions:
        for finding in row.findings:
            assert finding.severity in set(Severity)
    assert _row(analysis, 11).findings[0].severity is Severity.INVESTIGATE
    assert "total_tokens_anomaly" in _types(_row(analysis, 11))
    confirmed = next(
        f for f in _row(analysis, 12).findings if f.finding_type == "timestamp_regression"
    )
    assert confirmed.severity is Severity.CONFIRMED_ISSUE

    noise_row = _row(analysis, 13)
    assert noise_row.session_class is SessionClass.NOISE
    assert noise_row.noise_reasons == (NoiseReason.NO_OPERATOR_WORK,)
    assert noise_row.findings == ()
    assert noise_row.anomalies == ()
    review_row = _row(analysis, 14)
    assert review_row.session_class is SessionClass.NOISE
    assert NoiseReason.AUTOMATED_REVIEW in review_row.noise_reasons

    baseline = prior_baseline(
        after,
        population,
        Metric.TOTAL_TOKENS,
        excluded=frozenset({noise.session_ref, review.session_ref}),
    )
    assert baseline is not None
    assert noise.session_ref not in baseline.members
    assert review.session_ref not in baseline.members
    assert short.session_ref in baseline.members

    assert analysis.usage.sessions == len(population)
    assert analysis.usage.noise_sessions == 2
    assert analysis.usage.total_tokens == int(
        sum(row.metrics[Metric.TOTAL_TOKENS] for row in population)
    )

    short_row = _row(analysis, 15)
    assert short_row.session_class is SessionClass.LISTED
    assert short_row.noise_reasons == ()
    minimal = next(f for f in short_row.findings if f.finding_type == "minimal_input")
    assert minimal.severity is Severity.NOTICE


@pytest.mark.parametrize(
    ("steps", "tools", "noise"),
    [
        (0.0, 0.0, True),
        (1.0, 0.0, True),
        (1.0, 3.0, False),
        (2.0, 0.0, False),
        (140.0, 0.0, False),
        (1.0, None, False),
    ],
)
def test_no_operator_work_needs_one_step_and_an_observed_zero(
    steps: float, tools: float | None, noise: bool
) -> None:
    row = analyze_sessions([_session(1, steps=steps, tools=tools)], requests=(), now=_NOW).sessions[
        0
    ]

    assert (row.session_class is SessionClass.NOISE) is noise


def test_a_short_input_with_work_is_never_noise_on_length_alone() -> None:
    for chars in (1, 5):
        row = analyze_sessions(
            [_session(1, first_input_chars=chars, steps=3.0)], requests=(), now=_NOW
        ).sessions[0]
        assert row.session_class is SessionClass.LISTED


def test_severity_counts_carry_every_severity() -> None:
    analysis = analyze_sessions([], requests=(), now=_NOW)

    assert analysis.severity_counts == dict.fromkeys(Severity, 0)
    assert analysis.sessions == ()
    assert analysis.usage.sessions == 0
    assert analysis.usage.cost_usd is None


def test_repeated_patterns_group_findings_above_notice() -> None:
    analysis = analyze_sessions(
        [
            _session(1, issues=(StructuralIssue.IMPOSSIBLE_TOKEN_TOTALS,)),
            _session(2, issues=(StructuralIssue.IMPOSSIBLE_TOKEN_TOTALS,)),
        ],
        requests=(),
        now=_NOW,
    )

    (pattern,) = [
        p for p in analysis.repeated_patterns if p.finding_type == "impossible_token_totals"
    ]
    assert pattern.severity is Severity.CONFIRMED_ISSUE
    assert pattern.sessions == (_ref(1), _ref(2))


def test_every_anomaly_metric_has_a_plain_subject() -> None:
    assert all("_" not in noun for noun in METRIC_NOUNS.values())
    assert set(METRIC_NOUNS) == set(Metric)


# ---- rankings and cost --------------------------------------------------------------------


def test_unpriced_codex_sessions_carry_no_dollar_and_rank_only_by_tokens() -> None:
    claude = [
        _session(
            i,
            issues=(StructuralIssue.TIMESTAMP_REGRESSION,),
            cost_usd=Decimal(i + 1),
        )
        for i in range(3)
    ]
    codex = _session(
        3,
        runtime="codex",
        model="gpt-5-codex",
        issues=(StructuralIssue.TIMESTAMP_REGRESSION,),
        cost_usd=Decimal(99),
        pricing_quality=PricingQuality.UNAVAILABLE,
        total=9_000_000.0,
    )

    analysis = analyze_sessions([*claude, codex], requests=(), now=_NOW)

    assert analysis.session_cost_ranking == (_ref(2), _ref(1), _ref(0))
    assert codex.session_ref not in analysis.session_cost_ranking
    assert analysis.session_token_ranking == (codex.session_ref,)
    assert _row(analysis, 3).cost_usd is None
    assert analysis.usage.cost_usd == Decimal(6)
    assert analysis.usage.unpriced_sessions == 1


def test_titles_ride_on_their_sessions() -> None:
    title = SessionTitle(
        session_ref=_ref(1), title="Split the loader", source=TitleSource.MODEL_WRITTEN
    )

    analysis = analyze_sessions([_session(1)], requests=(), now=_NOW, titles={_ref(1): title})

    assert analysis.sessions[0].title == title


def test_progress_reports_the_analyse_stage() -> None:
    seen: list[tuple[str, int, int]] = []

    analyze_sessions(
        [_session(i) for i in range(3)], requests=(), now=_NOW, progress=lambda *a: seen.append(a)
    )

    assert seen[0] == ("sessions", 0, 3)
    assert seen[-1] == ("sessions", 3, 3)
    assert {stage for stage, _done, _total in seen} == {"sessions"}


# ---- reading the index ----------------------------------------------------------------


def _request(ref: str, ordinal: int, *, ts: datetime, tokens: int = 100) -> ObservedRequest:
    return ObservedRequest(
        vendor_session_ref=ref,
        runtime="claude",
        request_ref=f"req-{ordinal:032x}",
        ordinal=ordinal,
        ts=ts,
        model="claude-opus-4-7",
        input_tokens=tokens,
        output_tokens=10,
        cache_read_tokens=1_000 * ordinal,
        cache_write_tokens=0,
        total_tokens=tokens + 10 + 1_000 * ordinal,
        cost_usd=Decimal("0.01"),
        price_source=PriceSourceKind.LIST_RECONSTRUCTED,
        rate_table_version="v",
    )


def _observed(ref: str, requests: list[ObservedRequest], **fields: Any) -> ObservedSession:
    values: dict[str, Any] = {
        "vendor_session_ref": ref,
        "runtime": "claude",
        "project_id": "scope",
        "cwd_digest": f"cwd-{'a' * 32}",
        "model": "claude-opus-4-7",
        "started_at": _T0,
        "ended_at": _T0 + timedelta(hours=2),
        "duration_ms": 7_200_000,
        "turn_count": len(requests),
        "input_tokens": sum(r.input_tokens for r in requests),
        "output_tokens": sum(r.output_tokens for r in requests),
        "cache_read_tokens": sum(r.cache_read_tokens for r in requests),
        "cache_write_tokens": 0,
        "total_tokens": sum(r.total_tokens for r in requests),
        "cost_usd": Decimal("0.02"),
        "price_source": PriceSourceKind.LIST_RECONSTRUCTED,
        "rate_table_version": "v",
    }
    values.update(fields)
    return ObservedSession.model_validate(values)


def test_history_sessions_reads_metrics_and_issues_from_index_rows() -> None:
    ref = _ref(1)
    requests = [_request(ref, 1, ts=_T0), _request(ref, 2, ts=_T0 + timedelta(minutes=1))]
    durations = [
        ObservedDuration(
            vendor_session_ref=ref,
            runtime="claude",
            kind=DurationKind.TOOL_CALL,
            label="Read",
            count=4,
            total_ms=40,
            max_ms=20,
        ),
        ObservedDuration(
            vendor_session_ref=ref,
            runtime="claude",
            kind=DurationKind.SUBAGENT,
            label="Explore",
            count=2,
            total_ms=40,
            max_ms=30,
        ),
    ]
    regressed = _ref(2)
    backwards = [
        _request(regressed, 1, ts=_T0),
        _request(regressed, 2, ts=_T0 - timedelta(minutes=1)),
    ]
    indexed = IndexedHistory(
        sessions=(
            _observed(ref, requests),
            _observed(regressed, backwards, input_tokens=1, total_tokens=3_021),
        ),
        requests=(*requests, *backwards),
        durations=tuple(durations),
    )

    first, second = history_sessions(
        indexed, details={}, memory_files={}, project_labels={f"cwd-{'a' * 32}": "SYN"}
    )

    assert first.project_label == "SYN"
    assert first.metrics[Metric.TOOL_CALLS] == pytest.approx(4)
    assert first.metrics[Metric.CHILD_COUNT] == pytest.approx(2)
    assert first.orchestrated is True
    assert first.metrics[Metric.PEAK_CONTEXT_TOKENS] == pytest.approx(2_100)
    assert first.metrics[Metric.FINAL_CONTEXT_TOKENS] == pytest.approx(2_100)
    assert first.metrics[Metric.GROSS_INPUT_TOKENS] == pytest.approx(3_200)
    assert Metric.COMPACTION_COUNT not in first.metrics
    assert first.issues == ()
    assert first.pricing_quality is PricingQuality.VERIFIED
    assert first.model_family == "claude-opus"
    assert set(second.issues) == {
        StructuralIssue.IMPOSSIBLE_TOKEN_TOTALS,
        StructuralIssue.TIMESTAMP_REGRESSION,
    }


def test_history_sessions_keep_each_lane_in_its_own_order() -> None:
    ref = _ref(6)
    lane = "agent-" + "c" * 32
    main = [_request(ref, 1, ts=_T0), _request(ref, 2, ts=_T0 + timedelta(minutes=10))]
    sub = [
        _request(ref, ordinal, ts=_T0 + timedelta(minutes=minute), tokens=50_000).model_copy(
            update={"lane": lane, "request_ref": f"req-{ordinal + 8:032x}"}
        )
        for ordinal, minute in ((1, 5), (2, 6))
    ]
    requests = [*main, *sub]
    indexed = IndexedHistory(
        sessions=(_observed(ref, requests),), requests=tuple(requests), durations=()
    )

    (session,) = history_sessions(indexed, details={}, memory_files={}, project_labels={})

    assert session.issues == ()
    assert session.metrics[Metric.PEAK_CONTEXT_TOKENS] == pytest.approx(2_100)
    assert session.metrics[Metric.FINAL_CONTEXT_TOKENS] == pytest.approx(2_100)
    assert session.metrics[Metric.MODEL_STEPS] == pytest.approx(4)


def test_history_sessions_rebuild_memory_only_where_its_project_was_read() -> None:
    own, foreign = f"cwd-{'a' * 32}", f"cwd-{'b' * 32}"
    rows = (_observed(_ref(7), [], cwd_digest=own), _observed(_ref(8), [], cwd_digest=foreign))
    details = {
        row.vendor_session_ref: SessionDetail(
            vendor_session_ref=row.vendor_session_ref,
            runtime="claude",
            cwd_digest=row.cwd_digest,
            steps=(),
            tool_spans={},
            blocks=(),
            compactions=(),
            runtime_version=None,
        )
        for row in rows
    }

    mine, theirs = history_sessions(
        IndexedHistory(sessions=rows, requests=(), durations=()),
        details=details,
        memory_files={own: (("AGENTS.md", 900),)},
        project_labels={},
    )

    assert mine.context is not None and theirs.context is not None
    assert "preamble_memory_files_unknown" not in mine.context.limitations
    assert "preamble_memory_files_unknown" in theirs.context.limitations
    memory = [
        block.tokens
        for generation in mine.context.generations
        for block in generation.blocks
        if block.origin is InstructionOrigin.MEMORY_FILE
    ]
    assert memory == [900]
    assert not [
        block
        for generation in theirs.context.generations
        for block in generation.blocks
        if block.origin is InstructionOrigin.MEMORY_FILE
    ]


def test_history_sessions_leaves_codex_tool_counts_unobserved() -> None:
    ref = _ref(3)
    row = _observed(
        ref,
        [],
        runtime="codex",
        model="gpt-5-codex",
        cwd_digest=None,
        cost_usd=None,
        price_source=PriceSourceKind.UNPRICED,
        rate_table_version=None,
        turn_count=1,
        input_tokens=5,
        output_tokens=5,
        total_tokens=10,
    )

    (session,) = history_sessions(
        IndexedHistory(sessions=(row,), requests=(), durations=()),
        details={},
        memory_files={},
        project_labels={},
    )

    assert Metric.TOOL_CALLS not in session.metrics
    assert session.project_key == "unknown"
    assert session.pricing_quality is PricingQuality.UNAVAILABLE
    row_analysis = analyze_sessions([session], requests=(), now=_NOW).sessions[0]
    assert row_analysis.session_class is SessionClass.LISTED


def test_load_indexed_history_reads_every_observed_table(tmp_path: Path) -> None:
    store = SqliteMetricsStore(tmp_path / "m.db")
    store.init_schema()
    ref = _ref(1)
    requests = [_request(ref, 1, ts=_T0)]
    store.upsert("telemetry_observed_sessions", _observed(ref, requests))
    store.upsert("telemetry_observed_requests", requests[0])
    store.commit()

    indexed = load_indexed_history(store)

    assert [row.vendor_session_ref for row in indexed.sessions] == [ref]
    assert len(indexed.requests) == 1
    assert indexed.durations == ()
    empty = SqliteMetricsStore(tmp_path / "empty.db")
    empty.init_schema()
    assert load_indexed_history(empty).sessions == ()


# ---- storing --------------------------------------------------------------------------


def test_write_analysis_files_the_document_by_day(tmp_path: Path) -> None:
    analysis = analyze_sessions([_session(1)], requests=(), now=_NOW)

    path = write_analysis(tmp_path / "reflect", analysis, on=_NOW.date())

    assert path.name == f"{ANALYSIS_PREFIX}2026-11-01.json"
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_write_analysis_refuses_an_opaque_blob(tmp_path: Path) -> None:
    analysis = analyze_sessions([_session(1, project_label="Ab3d" * 60)], requests=(), now=_NOW)

    with pytest.raises(OpaqueBlobError):
        write_analysis(tmp_path, analysis, on=_NOW.date())
    assert list(tmp_path.iterdir()) == []


def test_history_session_refuses_an_unknown_field() -> None:
    with pytest.raises(ValueError, match="extra"):
        _session(1, prompt="text")


def test_min_baseline_is_eight() -> None:
    assert MIN_BASELINE == 8
