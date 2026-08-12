"""Unit tests for the pure metrics computations under ``eawf.workflow.estimation.metrics``.

Covers the four wave-level metrics — EU variance, audit pass rate, wave
elapsed minutes, and the planned-vs-reactive split — at the function
level. Per AGENTS test discipline: each public ``compute_*`` helper has
both boundary-case (empty / single / off-by-one) and error-path coverage,
and float aggregates are compared via :func:`pytest.approx`.

The integration smoke for the CLI command is in
``tests/integration/test_cli_metrics.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eawf.kernel.state.enums import (
    ActualStatus,
    AuditKind,
    AuditStatus,
    AuditVerdict,
    Confidence,
    EffortBucket,
    IterStatus,
    IterTrigger,
    WaveStatus,
)
from eawf.kernel.state.models import (
    ActualSummary,
    Audit,
    EstimateSummary,
    Iter,
    State,
    Wave,
)
from eawf.workflow.estimation.metrics import (
    METRICS_SCHEMA_VERSION,
    AuditPassRateMetric,
    EuVarianceMetric,
    MetricsSummary,
    PlannedVsReactiveMetric,
    RealisticWallClockMetric,
    WaveElapsedMetric,
    compute_audit_pass_rate,
    compute_eu_variance,
    compute_metrics,
    compute_planned_vs_reactive,
    compute_realistic_wall_clock,
    compute_wave_elapsed,
)

_T0 = datetime(2026, 5, 1, tzinfo=UTC)


def _empty_state() -> State:
    """Return a minimal but valid State with no waves/audits/estimates."""
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "QR",
            "slug": "quant",
            "title": "Quant",
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {
            "project_code": "QR",
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    return State.model_validate(payload)


def _wave(
    *,
    wave_id: str,
    status: WaveStatus = WaveStatus.CLOSED,
    opened_at: datetime = _T0,
    closed_at: datetime | None = None,
    effort_bucket: EffortBucket | None = None,
    deps: list[str] | None = None,
) -> Wave:
    """Return a ``Wave`` with the minimum fields the metrics rely on."""
    if closed_at is None and status == WaveStatus.CLOSED:
        closed_at = opened_at + timedelta(minutes=30)
    iter_id = "-".join(wave_id.split("-")[:2])
    return Wave(
        id=wave_id,
        iter_id=iter_id,
        title=f"wave {wave_id}",
        status=status,
        deps=deps or [],
        blocks=[],
        file_scopes=[],
        success_criteria=[],
        effort_bucket=effort_bucket,
        opened_at=opened_at,
        closed_at=closed_at,
    )


def _iter(*, iter_id: str, trigger: IterTrigger) -> Iter:
    """Return an ``Iter`` carrying *trigger* with the minimum required fields."""
    phase_id = iter_id.split("-")[0]
    return Iter(
        id=iter_id,
        phase_id=phase_id,
        title=f"iter {iter_id}",
        status=IterStatus.CLOSED,
        trigger=trigger,
        opened_at=_T0,
    )


def _estimate(*, wave_id: str, expected_eu: float, pessimistic_eu: float) -> EstimateSummary:
    return EstimateSummary(
        id=f"EST-{wave_id}",
        scope_id=wave_id,
        expected_eu=expected_eu,
        pessimistic_eu=pessimistic_eu,
        expected_minutes=expected_eu * 30.0,
        pessimistic_minutes=pessimistic_eu * 30.0,
        display=f"{expected_eu} EU",
        reference_class="core_swe",
        confidence=Confidence.MEDIUM,
        current_store_record_id=f"REC-{wave_id}",
        updated_at=_T0,
    )


def _actual(
    *, wave_id: str, elapsed_eu: float, calibration_excluded: bool = False
) -> ActualSummary:
    return ActualSummary(
        id=f"ACT-{wave_id}",
        scope_id=wave_id,
        status=ActualStatus.DONE,
        elapsed_eu=elapsed_eu,
        current_store_record_id=f"REC-{wave_id}",
        updated_at=_T0,
        calibration_excluded=calibration_excluded,
    )


def _audit(*, audit_id: str, scope_id: str, verdict: AuditVerdict | None) -> Audit:
    return Audit(
        id=audit_id,
        scope_id=scope_id,
        kind=AuditKind.EVALUATION,
        status=AuditStatus.COMPLETE,
        created_at=_T0,
        verdict=verdict,
    )


# ---- compute_eu_variance ----------------------------------------------------


def test_compute_eu_variance_empty_state_returns_zero_sample() -> None:
    """Boundary: a state with no waves yields a zero-sample metric."""
    result = compute_eu_variance(_empty_state())
    assert result == EuVarianceMetric(
        sample_count=0,
        mean_delta_eu=0.0,
        stdev_delta_eu=0.0,
        inside_pessimistic_share=0.0,
    )


def test_compute_eu_variance_single_sample_inside_pessimistic() -> None:
    """One CLOSED wave with elapsed inside the pessimistic band counts as 100% inside."""
    state = _empty_state()
    wave = _wave(wave_id="P01-I01-W01")
    state.waves[wave.id] = wave
    state.estimates = {wave.id: _estimate(wave_id=wave.id, expected_eu=1.0, pessimistic_eu=2.0)}
    state.actuals = {wave.id: _actual(wave_id=wave.id, elapsed_eu=1.5)}

    result = compute_eu_variance(state)
    assert result.sample_count == 1
    assert result.mean_delta_eu == pytest.approx(0.5)
    # Single sample: population stdev is 0.0 (no spread to report).
    assert result.stdev_delta_eu == pytest.approx(0.0)
    assert result.inside_pessimistic_share == pytest.approx(1.0)


def test_compute_eu_variance_excludes_non_closed_waves() -> None:
    """Boundary: only CLOSED waves contribute; in-progress samples are dropped."""
    state = _empty_state()
    open_wave = _wave(wave_id="P01-I01-W01", status=WaveStatus.IN_PROGRESS, closed_at=None)
    closed_wave = _wave(wave_id="P01-I01-W02")
    state.waves[open_wave.id] = open_wave
    state.waves[closed_wave.id] = closed_wave
    state.estimates = {
        open_wave.id: _estimate(wave_id=open_wave.id, expected_eu=1.0, pessimistic_eu=2.0),
        closed_wave.id: _estimate(wave_id=closed_wave.id, expected_eu=1.0, pessimistic_eu=2.0),
    }
    state.actuals = {
        open_wave.id: _actual(wave_id=open_wave.id, elapsed_eu=10.0),
        closed_wave.id: _actual(wave_id=closed_wave.id, elapsed_eu=1.5),
    }

    result = compute_eu_variance(state)
    assert result.sample_count == 1
    assert result.mean_delta_eu == pytest.approx(0.5)


def test_compute_eu_variance_drops_a_calibration_excluded_actual() -> None:
    """An excluded actual cannot move the precision headline.

    Both waves are CLOSED and carry an estimate and an actual, so only the
    exclusion flag separates them. The excluded row sits far outside the
    pessimistic band; if the filter is removed both the mean delta and the
    inside-pessimistic share change.
    """
    state = _empty_state()
    clean = _wave(wave_id="P01-I01-W01")
    excluded = _wave(wave_id="P01-I01-W02")
    state.waves[clean.id] = clean
    state.waves[excluded.id] = excluded
    state.estimates = {
        clean.id: _estimate(wave_id=clean.id, expected_eu=1.0, pessimistic_eu=2.0),
        excluded.id: _estimate(wave_id=excluded.id, expected_eu=1.0, pessimistic_eu=2.0),
    }
    state.actuals = {
        clean.id: _actual(wave_id=clean.id, elapsed_eu=1.5),
        excluded.id: _actual(wave_id=excluded.id, elapsed_eu=50.0, calibration_excluded=True),
    }

    result = compute_eu_variance(state)
    assert result.sample_count == 1
    assert result.mean_delta_eu == pytest.approx(0.5)
    assert result.inside_pessimistic_share == pytest.approx(1.0)


def test_compute_eu_variance_excludes_missing_estimate_or_actual() -> None:
    """A CLOSED wave with no estimate or no actual is not counted."""
    state = _empty_state()
    only_est = _wave(wave_id="P01-I01-W01")
    only_act = _wave(wave_id="P01-I01-W02")
    state.waves[only_est.id] = only_est
    state.waves[only_act.id] = only_act
    state.estimates = {
        only_est.id: _estimate(wave_id=only_est.id, expected_eu=1.0, pessimistic_eu=2.0)
    }
    state.actuals = {only_act.id: _actual(wave_id=only_act.id, elapsed_eu=1.5)}

    result = compute_eu_variance(state)
    assert result.sample_count == 0


def test_compute_eu_variance_outside_pessimistic_share_drops() -> None:
    """One sample over pessimistic, one under: inside-pess share = 0.5."""
    state = _empty_state()
    wa = _wave(wave_id="P01-I01-W01")
    wb = _wave(wave_id="P01-I01-W02")
    state.waves[wa.id] = wa
    state.waves[wb.id] = wb
    state.estimates = {
        wa.id: _estimate(wave_id=wa.id, expected_eu=1.0, pessimistic_eu=2.0),
        wb.id: _estimate(wave_id=wb.id, expected_eu=1.0, pessimistic_eu=2.0),
    }
    state.actuals = {
        wa.id: _actual(wave_id=wa.id, elapsed_eu=1.5),  # inside
        wb.id: _actual(wave_id=wb.id, elapsed_eu=3.0),  # outside
    }
    result = compute_eu_variance(state)
    assert result.sample_count == 2
    assert result.inside_pessimistic_share == pytest.approx(0.5)
    # Mean delta: ((1.5 - 1.0) + (3.0 - 1.0)) / 2 = 1.25
    assert result.mean_delta_eu == pytest.approx(1.25)
    # Population stdev around 1.25 with values [0.5, 2.0] = sqrt(((0.75)^2 + (0.75)^2)/2) = 0.75.
    assert result.stdev_delta_eu == pytest.approx(0.75)


# ---- compute_realistic_wall_clock ------------------------------------------


def test_compute_realistic_wall_clock_empty_waves_zeroes() -> None:
    """Boundary: empty DAG returns a zero rollup with the configured units."""
    result = compute_realistic_wall_clock([], max_parallel_waves=2, eu_minutes=60.0)
    assert result == RealisticWallClockMetric(
        work_sum_eu=0.0,
        critical_path_eu=0.0,
        queue_wall_clock_eu=0.0,
        inside_pessimistic_share=None,
        pessimism_multiplier=1.0,
        realistic_wall_clock_eu=0.0,
        realistic_wall_clock_hours=0.0,
        max_parallel_waves=2,
        eu_minutes=60.0,
    )


def test_compute_realistic_wall_clock_uses_critical_path_queue_and_pessimism() -> None:
    """DAG schedule respects deps, finite queue, and inside-pess calibration."""
    waves = [
        _wave(wave_id="P01-I01-W01", effort_bucket=EffortBucket.M),
        _wave(wave_id="P01-I01-W02", effort_bucket=EffortBucket.M),
        _wave(wave_id="P01-I01-W03", effort_bucket=EffortBucket.M),
        _wave(wave_id="P01-I01-W04", effort_bucket=EffortBucket.M),
        _wave(wave_id="P01-I01-W05", effort_bucket=EffortBucket.M),
    ]

    result = compute_realistic_wall_clock(
        waves,
        max_parallel_waves=2,
        inside_pessimistic_share=0.5,
        eu_minutes=60.0,
    )

    assert result.work_sum_eu == pytest.approx(5.0)
    assert result.critical_path_eu == pytest.approx(1.0)
    assert result.queue_wall_clock_eu == pytest.approx(3.0)
    assert result.pessimism_multiplier == pytest.approx(1.5)
    assert result.realistic_wall_clock_eu == pytest.approx(4.5)
    assert result.realistic_wall_clock_hours == pytest.approx(4.5)


def test_compute_realistic_wall_clock_dependency_chain_sets_lower_bound() -> None:
    """Critical path dominates even when parallel worker count is high."""
    waves = [
        _wave(wave_id="P01-I01-W01", effort_bucket=EffortBucket.M),
        _wave(
            wave_id="P01-I01-W02",
            effort_bucket=EffortBucket.M,
            deps=["P01-I01-W01"],
        ),
        _wave(
            wave_id="P01-I01-W03",
            effort_bucket=EffortBucket.M,
            deps=["P01-I01-W02"],
        ),
        _wave(wave_id="P01-I01-W04", effort_bucket=EffortBucket.M),
    ]

    result = compute_realistic_wall_clock(waves, max_parallel_waves=4)

    assert result.critical_path_eu == pytest.approx(3.0)
    assert result.queue_wall_clock_eu == pytest.approx(3.0)
    assert result.realistic_wall_clock_eu == pytest.approx(3.0)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_parallel_waves": 0}, "max_parallel_waves"),
        ({"eu_minutes": 0.0}, "eu_minutes"),
        ({"inside_pessimistic_share": 1.5}, "inside_pessimistic_share"),
    ],
)
def test_compute_realistic_wall_clock_rejects_invalid_inputs(
    kwargs: dict[str, object],
    message: str,
) -> None:
    """Error paths: invalid queue, conversion, and calibration inputs fail."""
    with pytest.raises(ValueError, match=message):
        compute_realistic_wall_clock([], **kwargs)


# ---- compute_audit_pass_rate ------------------------------------------------


def test_compute_audit_pass_rate_empty_state_zero_share() -> None:
    """Boundary: no audits → zero decided, zero share."""
    result = compute_audit_pass_rate(_empty_state())
    assert result == AuditPassRateMetric(
        decided_count=0,
        pass_count=0,
        minor_count=0,
        major_count=0,
        pass_share=0.0,
    )


def test_compute_audit_pass_rate_excludes_pending_verdicts() -> None:
    """Audits with no verdict yet are excluded from the denominator."""
    state = _empty_state()
    state.audits = {
        "A01": _audit(audit_id="A01", scope_id="P01-I01-W01", verdict=AuditVerdict.PASS),
        "A02": _audit(audit_id="A02", scope_id="P01-I01-W02", verdict=None),
    }
    result = compute_audit_pass_rate(state)
    assert result.decided_count == 1
    assert result.pass_count == 1
    assert result.pass_share == pytest.approx(1.0)


def test_compute_audit_pass_rate_mixed_verdicts() -> None:
    """Mix of pass / minor / major reflects in the per-verdict counters and share."""
    state = _empty_state()
    state.audits = {
        "A01": _audit(audit_id="A01", scope_id="P01-I01-W01", verdict=AuditVerdict.PASS),
        "A02": _audit(audit_id="A02", scope_id="P01-I01-W02", verdict=AuditVerdict.PASS),
        "A03": _audit(audit_id="A03", scope_id="P01-I01-W03", verdict=AuditVerdict.MINOR),
        "A04": _audit(audit_id="A04", scope_id="P01-I01-W04", verdict=AuditVerdict.MAJOR),
    }
    result = compute_audit_pass_rate(state)
    assert result.decided_count == 4
    assert result.pass_count == 2
    assert result.minor_count == 1
    assert result.major_count == 1
    assert result.pass_share == pytest.approx(0.5)


# ---- compute_wave_elapsed ---------------------------------------------------


def test_compute_wave_elapsed_empty_state_zero_aggregates() -> None:
    """Boundary: empty state yields zero counts and zero aggregates."""
    result = compute_wave_elapsed(_empty_state())
    assert result == WaveElapsedMetric(
        sample_count=0,
        mean_minutes=0.0,
        median_minutes=0.0,
        max_minutes=0.0,
    )


def test_compute_wave_elapsed_single_wave() -> None:
    """Single 30-minute wave: mean = median = max = 30."""
    state = _empty_state()
    wave = _wave(wave_id="P01-I01-W01")
    state.waves[wave.id] = wave
    result = compute_wave_elapsed(state)
    assert result.sample_count == 1
    assert result.mean_minutes == pytest.approx(30.0)
    assert result.median_minutes == pytest.approx(30.0)
    assert result.max_minutes == pytest.approx(30.0)


def test_compute_wave_elapsed_skips_open_waves() -> None:
    """Boundary: a wave with no closed_at is excluded."""
    state = _empty_state()
    open_wave = _wave(wave_id="P01-I01-W01", status=WaveStatus.PENDING, closed_at=None)
    closed_wave = _wave(wave_id="P01-I01-W02")
    state.waves[open_wave.id] = open_wave
    state.waves[closed_wave.id] = closed_wave
    result = compute_wave_elapsed(state)
    assert result.sample_count == 1


def test_compute_wave_elapsed_skips_clock_skew() -> None:
    """Error-path: a wave with closed_at earlier than opened_at is skipped."""
    state = _empty_state()
    skewed = _wave(
        wave_id="P01-I01-W01",
        opened_at=_T0,
        closed_at=_T0 - timedelta(minutes=5),
    )
    state.waves[skewed.id] = skewed
    result = compute_wave_elapsed(state)
    assert result.sample_count == 0


def test_compute_wave_elapsed_median_with_three_samples() -> None:
    """Median of {10, 30, 90} minutes is 30; max is 90; mean is ~43.33."""
    state = _empty_state()
    for idx, mins in enumerate((10, 30, 90), start=1):
        wave_id = f"P01-I01-W0{idx}"
        state.waves[wave_id] = _wave(
            wave_id=wave_id,
            opened_at=_T0,
            closed_at=_T0 + timedelta(minutes=mins),
        )
    result = compute_wave_elapsed(state)
    assert result.sample_count == 3
    assert result.median_minutes == pytest.approx(30.0)
    assert result.max_minutes == pytest.approx(90.0)
    assert result.mean_minutes == pytest.approx((10 + 30 + 90) / 3)


# ---- compute_planned_vs_reactive --------------------------------------------


def test_compute_planned_vs_reactive_empty_zero_split() -> None:
    """Boundary: no waves → 0 / 0 with share 0.0."""
    result = compute_planned_vs_reactive(_empty_state())
    assert result == PlannedVsReactiveMetric(
        planned_count=0,
        reactive_count=0,
        reactive_share=0.0,
    )


def test_compute_planned_vs_reactive_i01_only_is_all_planned() -> None:
    """All I01 waves: 100% planned, share=0%."""
    state = _empty_state()
    for idx in (1, 2, 3):
        wave_id = f"P01-I01-W0{idx}"
        state.waves[wave_id] = _wave(wave_id=wave_id)
    result = compute_planned_vs_reactive(state)
    assert result.planned_count == 3
    assert result.reactive_count == 0
    assert result.reactive_share == pytest.approx(0.0)


def test_compute_planned_vs_reactive_i02_counts_as_reactive() -> None:
    """Waves under I02+ count as reactive."""
    state = _empty_state()
    state.waves["P01-I01-W01"] = _wave(wave_id="P01-I01-W01")
    state.waves["P01-I02-W01"] = _wave(wave_id="P01-I02-W01")
    state.waves["P01-I02-W02"] = _wave(wave_id="P01-I02-W02")
    result = compute_planned_vs_reactive(state)
    assert result.planned_count == 1
    assert result.reactive_count == 2
    assert result.reactive_share == pytest.approx(2 / 3)


def test_compute_planned_vs_reactive_i10_counts_as_reactive() -> None:
    """Two-digit iter suffix is still classified as reactive (fallback path)."""
    state = _empty_state()
    state.waves["P01-I01-W01"] = _wave(wave_id="P01-I01-W01")
    state.waves["P01-I10-W01"] = _wave(wave_id="P01-I10-W01")
    result = compute_planned_vs_reactive(state)
    assert result.planned_count == 1
    assert result.reactive_count == 1
    assert result.reactive_share == pytest.approx(0.5)


# ---- compute_planned_vs_reactive: Iter.trigger denominator -------------------


def test_compute_planned_vs_reactive_trigger_none_excluded_from_denominator() -> None:
    """A ``none``-trigger iter drops its waves out of the denominator entirely."""
    state = _empty_state()
    # Two waves under a none-trigger iter + one proactive wave: only the
    # proactive wave is in the denominator, so the reactive share is 0.
    state.iters["P01-I01"] = _iter(iter_id="P01-I01", trigger=IterTrigger.NONE)
    state.iters["P01-I02"] = _iter(iter_id="P01-I02", trigger=IterTrigger.PROACTIVE)
    state.waves["P01-I01-W01"] = _wave(wave_id="P01-I01-W01")
    state.waves["P01-I01-W02"] = _wave(wave_id="P01-I01-W02")
    state.waves["P01-I02-W01"] = _wave(wave_id="P01-I02-W01")
    result = compute_planned_vs_reactive(state)
    assert result.planned_count == 1
    assert result.reactive_count == 0
    assert result.reactive_share == pytest.approx(0.0)


def test_compute_planned_vs_reactive_proactive_i02_counts_as_planned() -> None:
    """An I02+ iter tagged ``proactive`` counts as planned, not reactive.

    This is the artifact the wave corrects: the old id-suffix heuristic
    would have binned this I02 wave as reactive, but a proactive scope
    expansion is planned work.
    """
    state = _empty_state()
    state.iters["P01-I01"] = _iter(iter_id="P01-I01", trigger=IterTrigger.PROACTIVE)
    state.iters["P01-I02"] = _iter(iter_id="P01-I02", trigger=IterTrigger.PROACTIVE)
    state.waves["P01-I01-W01"] = _wave(wave_id="P01-I01-W01")
    state.waves["P01-I02-W01"] = _wave(wave_id="P01-I02-W01")
    result = compute_planned_vs_reactive(state)
    assert result.planned_count == 2
    assert result.reactive_count == 0
    assert result.reactive_share == pytest.approx(0.0)


def test_compute_planned_vs_reactive_reactive_trigger_counts_as_reactive() -> None:
    """A ``reactive``-trigger iter feeds the reactive numerator."""
    state = _empty_state()
    state.iters["P01-I01"] = _iter(iter_id="P01-I01", trigger=IterTrigger.PROACTIVE)
    state.iters["P01-I02"] = _iter(iter_id="P01-I02", trigger=IterTrigger.REACTIVE)
    state.waves["P01-I01-W01"] = _wave(wave_id="P01-I01-W01")
    state.waves["P01-I02-W01"] = _wave(wave_id="P01-I02-W01")
    result = compute_planned_vs_reactive(state)
    assert result.planned_count == 1
    assert result.reactive_count == 1
    assert result.reactive_share == pytest.approx(0.5)


def test_compute_planned_vs_reactive_all_none_is_empty_denominator() -> None:
    """Boundary: every iter ``none`` -> 0 / 0 with share 0.0 (renders n/a)."""
    state = _empty_state()
    state.iters["P01-I01"] = _iter(iter_id="P01-I01", trigger=IterTrigger.NONE)
    state.iters["P01-I02"] = _iter(iter_id="P01-I02", trigger=IterTrigger.NONE)
    state.waves["P01-I01-W01"] = _wave(wave_id="P01-I01-W01")
    state.waves["P01-I02-W01"] = _wave(wave_id="P01-I02-W01")
    result = compute_planned_vs_reactive(state)
    assert result == PlannedVsReactiveMetric(
        planned_count=0,
        reactive_count=0,
        reactive_share=0.0,
    )


def test_compute_planned_vs_reactive_mixed_triggers_share() -> None:
    """Mixed reactive/proactive/none: none is excluded; share over the rest."""
    state = _empty_state()
    state.iters["P01-I01"] = _iter(iter_id="P01-I01", trigger=IterTrigger.PROACTIVE)
    state.iters["P01-I02"] = _iter(iter_id="P01-I02", trigger=IterTrigger.REACTIVE)
    state.iters["P01-I03"] = _iter(iter_id="P01-I03", trigger=IterTrigger.NONE)
    # proactive: 2 planned; reactive: 1; none: 1 excluded.
    state.waves["P01-I01-W01"] = _wave(wave_id="P01-I01-W01")
    state.waves["P01-I01-W02"] = _wave(wave_id="P01-I01-W02")
    state.waves["P01-I02-W01"] = _wave(wave_id="P01-I02-W01")
    state.waves["P01-I03-W01"] = _wave(wave_id="P01-I03-W01")
    result = compute_planned_vs_reactive(state)
    assert result.planned_count == 2
    assert result.reactive_count == 1
    # Denominator excludes the none-iter wave: 1 / (2 + 1).
    assert result.reactive_share == pytest.approx(1 / 3)


def test_compute_planned_vs_reactive_falls_back_to_id_when_iter_absent() -> None:
    """A wave whose iter row is absent falls back to the id-suffix heuristic."""
    state = _empty_state()
    # No iter rows at all: I01 -> planned, I02 -> reactive via the fallback.
    state.waves["P01-I01-W01"] = _wave(wave_id="P01-I01-W01")
    state.waves["P01-I02-W01"] = _wave(wave_id="P01-I02-W01")
    result = compute_planned_vs_reactive(state)
    assert result.planned_count == 1
    assert result.reactive_count == 1
    assert result.reactive_share == pytest.approx(0.5)


# ---- compute_metrics ---------------------------------------------------------


def test_compute_metrics_returns_typed_summary() -> None:
    """The top-level entry point returns a :class:`MetricsSummary` with schema_version=1."""
    state = _empty_state()
    result = compute_metrics(state)
    assert isinstance(result, MetricsSummary)
    assert result.schema_version == METRICS_SCHEMA_VERSION
    assert result.schema_version == 1


def test_compute_metrics_aggregates_all_four_metrics() -> None:
    """End-to-end: one CLOSED wave + one audit yields populated values across all four metrics."""
    state = _empty_state()
    wave = _wave(wave_id="P01-I01-W01")
    state.waves[wave.id] = wave
    state.estimates = {wave.id: _estimate(wave_id=wave.id, expected_eu=1.0, pessimistic_eu=2.0)}
    state.actuals = {wave.id: _actual(wave_id=wave.id, elapsed_eu=1.5)}
    state.audits = {
        "A01": _audit(audit_id="A01", scope_id="P01-I01-W01", verdict=AuditVerdict.PASS)
    }
    result = compute_metrics(state)
    assert result.eu_variance.sample_count == 1
    assert result.audit_pass_rate.decided_count == 1
    assert result.wave_elapsed.sample_count == 1
    assert result.planned_vs_reactive.planned_count == 1


def test_metrics_summary_json_round_trip_preserves_schema_version() -> None:
    """Round-trip through ``model_dump(mode='json')`` keeps schema_version=1."""
    state = _empty_state()
    summary = compute_metrics(state)
    payload = summary.model_dump(mode="json")
    assert payload["schema_version"] == 1
    revived = MetricsSummary.model_validate(payload)
    assert revived == summary


def test_metrics_summary_rejects_unknown_schema_version() -> None:
    """``Literal[1]`` enforces the wire contract at validate time."""
    state = _empty_state()
    summary = compute_metrics(state)
    payload = summary.model_dump(mode="json")
    payload["schema_version"] = 2
    with pytest.raises(ValueError, match="schema_version"):
        MetricsSummary.model_validate(payload)


def test_metrics_summary_rejects_extra_keys() -> None:
    """Pydantic strict mode (``extra='forbid'``) rejects unknown fields."""
    state = _empty_state()
    summary = compute_metrics(state)
    payload = summary.model_dump(mode="json")
    payload["unexpected_metric"] = {"foo": "bar"}
    with pytest.raises(ValueError, match="unexpected_metric"):
        MetricsSummary.model_validate(payload)


# ---- ActualSummary v0.4 fields --------------------------------


def test_actual_summary_defaults_zero_tokens_and_cost() -> None:
    """ActualSummary defaults ``actual_tokens=0`` and ``actual_cost_usd=0.0``.

    Existing on-disk rows (pre-P28-I02-W03) omit these fields; the
    defaults keep the model additive / replay-safe with no schema bump.
    """
    actual = _actual(wave_id="P01-I01-W01", elapsed_eu=1.5)
    assert actual.actual_tokens == 0
    assert actual.actual_cost_usd == 0.0


def test_actual_summary_accepts_token_and_cost_overrides() -> None:
    """The two new fields round-trip through validation when explicit."""
    actual = ActualSummary(
        id="ACT-P01-I01-W01",
        scope_id="P01-I01-W01",
        status=ActualStatus.DONE,
        elapsed_eu=1.5,
        actual_tokens=12345,
        actual_cost_usd=0.42,
        current_store_record_id="REC-P01-I01-W01",
        updated_at=_T0,
    )
    assert actual.actual_tokens == 12345
    assert actual.actual_cost_usd == pytest.approx(0.42)


def test_actual_summary_rejects_negative_tokens() -> None:
    """``actual_tokens`` carries ``Field(ge=0)`` — negative values fail."""
    with pytest.raises(ValueError, match="actual_tokens"):
        ActualSummary(
            id="ACT-W01",
            scope_id="P01-I01-W01",
            status=ActualStatus.DONE,
            elapsed_eu=0.0,
            actual_tokens=-1,
            current_store_record_id="REC-W01",
            updated_at=_T0,
        )


def test_actual_summary_rejects_negative_cost() -> None:
    """``actual_cost_usd`` carries ``Field(ge=0.0)`` — negative cost fails."""
    with pytest.raises(ValueError, match="actual_cost_usd"):
        ActualSummary(
            id="ACT-W01",
            scope_id="P01-I01-W01",
            status=ActualStatus.DONE,
            elapsed_eu=0.0,
            actual_cost_usd=-0.01,
            current_store_record_id="REC-W01",
            updated_at=_T0,
        )
