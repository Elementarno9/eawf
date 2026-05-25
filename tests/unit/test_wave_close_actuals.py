"""Default estimate on claim + no auto-actual on close (P27-I05-W28).

Claiming a wave seeds a default :class:`~eawf.kernel.state.models.EstimateSummary`
from its effort bucket (the estimate side of the learning loop, unchanged).

Closing a wave records NO actual: the open->close wall-clock span is not
agent effort (it counts overnight / cross-session / other-wave idle), so
the W28 EU-actual fix retired the auto-record. Real actuals come from
measured sources only (the manual ``eawf actual start/stop`` segments now,
per-wave token accounting in v0.4), so ``compute_estimate_actual_variance``
reads the honest empty state until a measured actual exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eawf.kernel.state.enums import (
    Confidence,
    EffortBucket,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import CurrentPointers, Project, State
from eawf.workflow.estimation.buckets import default_estimate_summary
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


# ---- close_wave records NO actual (W28: wall-clock auto-record retired) ------


def test_close_wave_records_no_actual() -> None:
    state = _empty_state()
    _seed_wave(state)
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    # A positive open->close span used to fabricate a wall-clock actual.
    state.waves["P01-I01-W01"].opened_at = datetime.now(UTC) - timedelta(minutes=60)

    wave = close_wave(state, wave_id="P01-I01-W01", outcome="ok")

    assert wave.status == WaveStatus.CLOSED
    # No actual is auto-recorded from the wall-clock span.
    assert not (state.actuals or {})


def test_close_wave_missing_opened_at_does_not_crash() -> None:
    state = _empty_state()
    _seed_wave(state)
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
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

    # A second close on an already-CLOSED wave is rejected by the status guard.
    with pytest.raises(Exception, match="not claimed"):
        close_wave(state, wave_id="P01-I01-W01", outcome="ok")


# ---- variance metric reads the honest empty state without a measured actual --


def test_metrics_variance_empty_without_measured_actual() -> None:
    state = _empty_state()
    _seed_wave(state, effort_bucket=EffortBucket.M)
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-1")
    state.waves["P01-I01-W01"].opened_at = datetime.now(UTC) - timedelta(minutes=45)
    close_wave(state, wave_id="P01-I01-W01", outcome="ok")

    metric = compute_estimate_actual_variance(state)

    # Close records no actual, so the variance metric has no sample to report.
    assert metric.sample_count == 0
    assert metric.variance_pct is None
