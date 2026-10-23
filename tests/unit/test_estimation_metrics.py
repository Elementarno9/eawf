"""Unit tests for the pure metrics computations under ``eawf.workflow.estimation.metrics``.

Covers the v0.4 :class:`~eawf.kernel.state.models.ActualSummary` fields
the computations read: their defaults, overrides and refusals.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eawf.kernel.state.enums import (
    ActualStatus,
)
from eawf.kernel.state.models import ActualSummary

_T0 = datetime(2026, 5, 1, tzinfo=UTC)


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


# ---- compute_wave_elapsed ---------------------------------------------------


# ---- ActualSummary v0.4 fields --------------------------------


def test_meas_041_actual_summary_defaults_unknown_tokens_and_cost() -> None:
    """ActualSummary defaults both priced fields to ``None``, never to a zero.

    Rows that omit the fields, and rows that recorded a zero, both still
    validate: the first reads unknown, the second a measured zero.
    """
    actual = _actual(wave_id="P01-I01-W01", elapsed_eu=1.5)
    assert actual.actual_tokens is None
    assert actual.actual_cost_usd is None
    zeroed = ActualSummary.model_validate({**actual.model_dump(), "actual_tokens": 0})
    assert zeroed.actual_tokens == 0


def test_meas_041_every_priced_field_is_nullable_at_the_type_level() -> None:
    """The type admits ``None`` on every priced field of an actual."""
    for name in ("actual_tokens", "actual_cost_usd"):
        field = ActualSummary.model_fields[name]
        assert field.default is None
        assert not field.is_required()


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
