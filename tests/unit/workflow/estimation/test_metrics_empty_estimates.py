"""Empty-data contract for the estimate-vs-actual variance metrics.

Wave claim no longer seeds a derived estimate row from the effort bucket,
so ``state.estimates`` is legitimately ``None`` (key absent) or ``{}``
(key present, no rows) on a healthy repo. Both variance roll-ups must
return their declared empty-data shape in that case rather than raising
or fabricating a ``0%`` reading:

- :func:`compute_estimate_actual_variance` -> zero counts and
  ``variance_pct is None`` (the "no data" branch the ship-gate and the
  VarianceTile render).
- :func:`compute_eu_variance` -> an all-zero :class:`EuVarianceMetric`.

Boundary coverage runs empty -> single -> two samples; the error paths
cover a CLOSED wave with an actual but no estimate (must not contribute)
and a malformed estimate row (must not validate).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    ActualStatus,
    Confidence,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    ActualSummary,
    EstimateSummary,
    State,
    Wave,
)
from eawf.workflow.estimation.metrics import (
    EstimateActualVarianceMetric,
    EuVarianceMetric,
    compute_estimate_actual_variance,
    compute_eu_variance,
)

_T0 = datetime(2026, 5, 1, tzinfo=UTC)


def _empty_state() -> State:
    """Return a minimal valid State with no waves, estimates, or actuals."""
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _T0.isoformat(),
            "project": {
                "code": "QR",
                "slug": "qr",
                "title": "QR",
                "domains": ["x"],
                "default_branch": "main",
                "status": "active",
                "repo_urn": "urn:eawf:v1:repo:QR",
            },
            "current": {"project_code": "QR"},
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


def _closed_wave(*, wave_id: str) -> Wave:
    return Wave(
        id=wave_id,
        iter_id="-".join(wave_id.split("-")[:2]),
        title=f"wave {wave_id}",
        status=WaveStatus.CLOSED,
        deps=[],
        blocks=[],
        file_scopes=[],
        success_criteria=[],
        effort_bucket=None,
        opened_at=_T0,
        closed_at=_T0 + timedelta(minutes=30),
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
        reference_class="operator",
        confidence=Confidence.MEDIUM,
        current_store_record_id=f"REC-{wave_id}",
        updated_at=_T0,
    )


def _actual(*, wave_id: str, elapsed_eu: float) -> ActualSummary:
    return ActualSummary(
        id=f"ACT-{wave_id}",
        scope_id=wave_id,
        status=ActualStatus.DONE,
        elapsed_eu=elapsed_eu,
        current_store_record_id=f"REC-{wave_id}",
        updated_at=_T0,
    )


# ---- compute_estimate_actual_variance ---------------------------------------


@pytest.mark.parametrize("estimates", [None, {}])
def test_compute_estimate_actual_variance_empty_map_returns_no_data(
    estimates: dict[str, EstimateSummary] | None,
) -> None:
    """Boundary: a ``None`` or empty estimate map yields the no-data shape."""
    state = _empty_state()
    state.estimates = estimates

    result = compute_estimate_actual_variance(state)

    assert result == EstimateActualVarianceMetric(
        sample_count=0,
        planned_eu=0.0,
        actual_eu=0.0,
        variance_pct=None,
    )


def test_compute_estimate_actual_variance_closed_wave_without_estimate_is_no_data() -> None:
    """Error path: a CLOSED wave with an actual but no estimate contributes nothing.

    This is the post-seeding steady state -- actuals are recorded at close,
    estimates only when an operator authors one -- so the metric must report
    "no data" rather than dividing by a zero planned-EU denominator.
    """
    state = _empty_state()
    wave = _closed_wave(wave_id="P01-I01-W01")
    state.waves[wave.id] = wave
    state.actuals = {wave.id: _actual(wave_id=wave.id, elapsed_eu=2.0)}
    state.estimates = {}

    result = compute_estimate_actual_variance(state)

    assert result.sample_count == 0
    assert result.planned_eu == pytest.approx(0.0)
    assert result.variance_pct is None


def test_compute_estimate_actual_variance_single_estimated_wave_reports_pct() -> None:
    """Boundary: exactly one estimated + closed wave yields a real percentage."""
    state = _empty_state()
    wave = _closed_wave(wave_id="P01-I01-W01")
    state.waves[wave.id] = wave
    state.estimates = {wave.id: _estimate(wave_id=wave.id, expected_eu=2.0, pessimistic_eu=4.0)}
    state.actuals = {wave.id: _actual(wave_id=wave.id, elapsed_eu=3.0)}

    result = compute_estimate_actual_variance(state)

    assert result.sample_count == 1
    assert result.planned_eu == pytest.approx(2.0)
    assert result.actual_eu == pytest.approx(3.0)
    assert result.variance_pct == pytest.approx(50.0)


# ---- compute_eu_variance ----------------------------------------------------


@pytest.mark.parametrize("estimates", [None, {}])
def test_compute_eu_variance_empty_map_returns_zero_sample(
    estimates: dict[str, EstimateSummary] | None,
) -> None:
    """Boundary: a ``None`` or empty estimate map yields the zero-sample shape."""
    state = _empty_state()
    state.estimates = estimates

    result = compute_eu_variance(state)

    assert result == EuVarianceMetric(
        sample_count=0,
        mean_delta_eu=0.0,
        stdev_delta_eu=0.0,
        inside_pessimistic_share=0.0,
    )


def test_compute_eu_variance_closed_wave_without_estimate_is_zero_sample() -> None:
    """Error path: an actual without a matching estimate never contributes."""
    state = _empty_state()
    wave = _closed_wave(wave_id="P01-I01-W01")
    state.waves[wave.id] = wave
    state.actuals = {wave.id: _actual(wave_id=wave.id, elapsed_eu=1.0)}
    state.estimates = {}

    result = compute_eu_variance(state)

    assert result.sample_count == 0
    assert result.mean_delta_eu == pytest.approx(0.0)
    assert result.inside_pessimistic_share == pytest.approx(0.0)


def test_compute_eu_variance_single_estimated_wave_counts_one_sample() -> None:
    """Boundary: one estimated + closed wave inside the pessimistic band."""
    state = _empty_state()
    wave = _closed_wave(wave_id="P01-I01-W01")
    state.waves[wave.id] = wave
    state.estimates = {wave.id: _estimate(wave_id=wave.id, expected_eu=1.0, pessimistic_eu=2.0)}
    state.actuals = {wave.id: _actual(wave_id=wave.id, elapsed_eu=1.5)}

    result = compute_eu_variance(state)

    assert result.sample_count == 1
    assert result.mean_delta_eu == pytest.approx(0.5)
    assert result.inside_pessimistic_share == pytest.approx(1.0)


# ---- the map itself ---------------------------------------------------------


def test_estimates_map_rejects_row_missing_required_field() -> None:
    """Error path: a partial estimate row fails strict validation, not the metric."""
    state = _empty_state()
    with pytest.raises(ValidationError):
        State.model_validate(
            {
                **state.model_dump(mode="json"),
                "estimates": {"P01-I01-W01": {"id": "EST-P01-I01-W01"}},
            }
        )
