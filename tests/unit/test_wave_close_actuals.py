"""Auto-recorded actuals on wave close + default estimate on claim (P27-I02-W25).

These tests close the estimation learning loop: claiming a wave seeds a
default :class:`~eawf.kernel.state.models.EstimateSummary` from its effort bucket,
and closing a wave records an :class:`~eawf.kernel.state.models.ActualSummary` from
the open->close wall-clock span. Together they give
:func:`~eawf.workflow.estimation.metrics.compute_estimate_actual_variance` a sample to
report instead of "no data".

The close path is exercised by the live orchestrator (``eawf wave
close``/``land``), so the crash-safety contract — a close on a wave with
missing timestamps derives nothing rather than raising — is asserted
explicitly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eawf.kernel.state.enums import (
    ActualStatus,
    Confidence,
    EffortBucket,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import CurrentPointers, Project, State
from eawf.workflow.estimation.buckets import (
    actual_summary_from_timestamps,
    default_estimate_summary,
)
from eawf.workflow.estimation.metrics import compute_estimate_actual_variance
from eawf.workflow.lifecycle.transitions import (
    claim_wave,
    close_wave,
    open_iter,
    open_phase,
    plan_wave,
)


def _empty_state() -> State:
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": datetime.now(UTC).isoformat(),
            "project": Project(
                code="QR",
                slug="qr",
                title="QR",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _seed_wave(
    state: State,
    *,
    wave_id: str = "P01-I01-W01",
    effort_bucket: EffortBucket | None = EffortBucket.M,
) -> None:
    """Plan a phase -> iter -> single wave with *effort_bucket* set."""
    open_phase(state, phase_id="P01", title="x")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="y")
    plan_wave(
        state,
        wave_id=wave_id,
        iter_id="P01-I01",
        title="w",
        file_scopes=["src/"],
        effort_bucket=effort_bucket,
    )


# ---- default_estimate_summary (pure helper) ---------------------------------


def test_default_estimate_summary_from_bucket_fields() -> None:
    state = _empty_state()
    _seed_wave(state, effort_bucket=EffortBucket.M)
    wave = state.waves["P01-I01-W01"]
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)

    est = default_estimate_summary(wave, now=now)

    assert est is not None
    assert est.scope_id == "P01-I01-W01"
    assert est.id == "EST-P01-I01-W01"
    # M bucket centroid is 1.0 EU; pessimistic applies the 3.6 ratio.
    assert est.expected_eu == pytest.approx(1.0)
    assert est.pessimistic_eu == pytest.approx(3.6)
    assert est.expected_minutes == pytest.approx(30.0)
    assert est.confidence == Confidence.LOW
    assert est.reference_class == "bucket:M"
    assert est.updated_at == now


def test_default_estimate_summary_no_bucket_returns_none() -> None:
    state = _empty_state()
    _seed_wave(state, effort_bucket=None)
    wave = state.waves["P01-I01-W01"]

    assert default_estimate_summary(wave, now=datetime.now(UTC)) is None


# ---- actual_summary_from_timestamps (pure helper) ---------------------------


def test_actual_summary_from_timestamps_positive_span() -> None:
    state = _empty_state()
    _seed_wave(state)
    wave = state.waves["P01-I01-W01"]
    wave.opened_at = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    wave.closed_at = wave.opened_at + timedelta(minutes=90)
    now = wave.closed_at

    act = actual_summary_from_timestamps(wave, now=now)

    assert act is not None
    assert act.scope_id == "P01-I01-W01"
    assert act.id == "ACT-P01-I01-W01"
    assert act.status == ActualStatus.DONE
    # 90 minutes / 30 minutes-per-EU = 3.0 EU.
    assert act.elapsed_eu == pytest.approx(3.0)
    assert act.updated_at == now


def test_actual_summary_from_timestamps_missing_opened_returns_none() -> None:
    state = _empty_state()
    _seed_wave(state)
    wave = state.waves["P01-I01-W01"]
    wave.opened_at = None
    wave.closed_at = datetime.now(UTC)

    assert actual_summary_from_timestamps(wave, now=datetime.now(UTC)) is None


def test_actual_summary_from_timestamps_non_positive_span_returns_none() -> None:
    state = _empty_state()
    _seed_wave(state)
    wave = state.waves["P01-I01-W01"]
    wave.opened_at = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    # closed before opened -> non-positive span -> no actual.
    wave.closed_at = wave.opened_at - timedelta(minutes=5)

    assert actual_summary_from_timestamps(wave, now=datetime.now(UTC)) is None


# ---- claim_wave seeds a default estimate ------------------------------------


def test_claim_wave_seeds_default_estimate_from_bucket() -> None:
    state = _empty_state()
    _seed_wave(state, effort_bucket=EffortBucket.L)

    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")

    assert state.estimates is not None
    est = state.estimates["P01-I01-W01"]
    # L bucket centroid is 2.0 EU.
    assert est.expected_eu == pytest.approx(2.0)
    assert est.scope_id == "P01-I01-W01"


def test_claim_wave_no_bucket_writes_no_estimate() -> None:
    state = _empty_state()
    _seed_wave(state, effort_bucket=None)

    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")

    # No bucket -> no estimate seeded; the dict stays None/empty.
    assert not (state.estimates or {})


# ---- close_wave records an actual -------------------------------------------


def test_close_wave_records_actual_from_timestamps() -> None:
    state = _empty_state()
    _seed_wave(state)
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    # Pin opened_at into the past so the open->close span is positive.
    state.waves["P01-I01-W01"].opened_at = datetime.now(UTC) - timedelta(minutes=60)

    close_wave(state, wave_id="P01-I01-W01", outcome="ok")

    assert state.actuals is not None
    act = state.actuals["P01-I01-W01"]
    assert act.status == ActualStatus.DONE
    assert act.elapsed_eu > 0.0
    assert act.scope_id == "P01-I01-W01"


def test_close_wave_missing_opened_at_does_not_crash() -> None:
    state = _empty_state()
    _seed_wave(state)
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    # Simulate a wave with no opened_at — the close must not crash and must
    # derive no actual (skips gracefully).
    state.waves["P01-I01-W01"].opened_at = None

    wave = close_wave(state, wave_id="P01-I01-W01", outcome="ok")

    assert wave.status == WaveStatus.CLOSED
    assert not (state.actuals or {})


def test_close_wave_idempotent_double_close_is_safe() -> None:
    state = _empty_state()
    _seed_wave(state)
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    state.waves["P01-I01-W01"].opened_at = datetime.now(UTC) - timedelta(minutes=30)
    close_wave(state, wave_id="P01-I01-W01", outcome="ok")

    # A second close on an already-CLOSED wave is rejected by the status
    # guard rather than corrupting the recorded actual.
    with pytest.raises(Exception, match="not claimed"):
        close_wave(state, wave_id="P01-I01-W01", outcome="ok")
    assert state.actuals is not None
    assert "P01-I01-W01" in state.actuals


# ---- end-to-end: variance metric returns a non-empty sample -----------------


def test_metrics_variance_non_empty_after_close() -> None:
    state = _empty_state()
    _seed_wave(state, effort_bucket=EffortBucket.M)
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    state.waves["P01-I01-W01"].opened_at = datetime.now(UTC) - timedelta(minutes=45)
    close_wave(state, wave_id="P01-I01-W01", outcome="ok")

    metric = compute_estimate_actual_variance(state)

    assert metric.sample_count == 1
    assert metric.planned_eu == pytest.approx(1.0)
    assert metric.actual_eu > 0.0
    assert metric.variance_pct is not None
