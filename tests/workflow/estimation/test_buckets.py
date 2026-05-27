"""Bucket-derived estimate selection tests for P28-I02-W14."""

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
from eawf.kernel.state.models import ActualSummary, CurrentPointers, Project, State, Wave
from eawf.workflow.estimation.buckets import BUCKET_EU, calibrate_buckets, default_estimate_summary

_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)


def _empty_state() -> State:
    """Return a minimal valid state for calibration tests."""
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _T0.isoformat(),
            "project": Project(
                code="QR",
                slug="qr",
                title="QR",
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


def _wave(
    *,
    wave_id: str,
    effort_bucket: EffortBucket = EffortBucket.M,
    status: WaveStatus = WaveStatus.PENDING,
) -> Wave:
    """Return a wave with the fields bucket estimation reads."""
    iter_id = "-".join(wave_id.split("-")[:2])
    return Wave(
        id=wave_id,
        iter_id=iter_id,
        title=f"wave {wave_id}",
        status=status,
        deps=[],
        blocks=[],
        file_scopes=[],
        success_criteria=[],
        effort_bucket=effort_bucket,
        opened_at=_T0,
        closed_at=_T0 if status == WaveStatus.CLOSED else None,
    )


def _actual(*, wave_id: str, elapsed_eu: float, updated_at: datetime = _T0) -> ActualSummary:
    """Return a completed actual row for *wave_id*."""
    return ActualSummary(
        id=f"ACT-{wave_id}",
        scope_id=wave_id,
        status=ActualStatus.DONE,
        elapsed_eu=elapsed_eu,
        current_store_record_id=f"REC-{wave_id}",
        updated_at=updated_at,
    )


def _state_with_samples(
    samples: tuple[float, ...],
    *,
    bucket: EffortBucket = EffortBucket.M,
) -> State:
    """Return state with CLOSED waves whose actuals match *samples*."""
    state = _empty_state()
    actuals: dict[str, ActualSummary] = {}
    for index, elapsed in enumerate(samples, start=1):
        wave_id = f"P01-I01-W{index:02d}"
        state.waves[wave_id] = _wave(
            wave_id=wave_id,
            effort_bucket=bucket,
            status=WaveStatus.CLOSED,
        )
        actuals[wave_id] = _actual(
            wave_id=wave_id,
            elapsed_eu=elapsed,
            updated_at=_T0 - timedelta(minutes=index),
        )
    state.actuals = actuals
    return state


def test_calibrate_buckets_populates_fitted_pessimistic_eu() -> None:
    """Fitted rows carry the p90 pessimistic EU alongside the mean."""
    state = _state_with_samples((1.0, 2.0, 3.0, 4.0, 5.0))

    report = calibrate_buckets(state, now=_T0)
    row = next(row for row in report.buckets if row.bucket == EffortBucket.M)

    assert row.fitted_eu == pytest.approx(3.0)
    assert row.fitted_pessimistic_eu == pytest.approx(5.0)


def test_default_estimate_summary_prefers_config_override() -> None:
    """Explicit config override wins over fitted samples."""
    state = _state_with_samples((1.0, 1.1, 1.2, 1.3, 1.4))
    wave = _wave(wave_id="P01-I01-W99", effort_bucket=EffortBucket.M)
    config = {
        "estimation": {
            "buckets": {
                "overrides": {
                    "M": {
                        "expected_eu": 2.75,
                        "pessimistic_eu": 7.5,
                    }
                }
            }
        }
    }

    estimate = default_estimate_summary(wave, now=_T0, state=state, config=config)

    assert estimate is not None
    assert estimate.expected_eu == pytest.approx(2.75)
    assert estimate.pessimistic_eu == pytest.approx(7.5)
    assert estimate.confidence == Confidence.HIGH
    assert "bucket-config" in estimate.current_store_record_id


def test_default_estimate_summary_uses_fitted_eu_at_n_min() -> None:
    """Five in-window samples trigger fitted expected EU with MEDIUM confidence."""
    state = _state_with_samples((0.8, 1.0, 1.2, 1.4, 1.6))
    wave = _wave(wave_id="P01-I01-W99", effort_bucket=EffortBucket.M)

    estimate = default_estimate_summary(wave, now=_T0, state=state)

    assert estimate is not None
    assert estimate.expected_eu == pytest.approx(1.2)
    assert estimate.pessimistic_eu == pytest.approx(1.6)
    assert estimate.confidence == Confidence.MEDIUM
    assert "bucket-fitted" in estimate.current_store_record_id


def test_default_estimate_summary_high_confidence_at_30_samples() -> None:
    """Thirty samples promote fitted estimates to HIGH confidence."""
    state = _state_with_samples(tuple(1.5 for _ in range(30)))
    wave = _wave(wave_id="P01-I01-W99", effort_bucket=EffortBucket.M)

    estimate = default_estimate_summary(wave, now=_T0, state=state)

    assert estimate is not None
    assert estimate.expected_eu == pytest.approx(1.5)
    assert estimate.confidence == Confidence.HIGH


def test_default_estimate_summary_falls_back_below_n_min() -> None:
    """Fewer than five samples keep the static bucket fallback."""
    state = _state_with_samples((1.5, 1.5, 1.5, 1.5))
    wave = _wave(wave_id="P01-I01-W99", effort_bucket=EffortBucket.M)

    estimate = default_estimate_summary(wave, now=_T0, state=state)

    assert estimate is not None
    assert estimate.expected_eu == pytest.approx(BUCKET_EU[EffortBucket.M])
    assert estimate.confidence == Confidence.LOW
    assert "bucket-fitted" not in estimate.current_store_record_id
