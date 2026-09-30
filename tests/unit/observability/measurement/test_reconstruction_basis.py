"""A measured share states its quality on the four-value ladder and its basis beside it.

``reconstructed`` is not a quality: a share computed from recorded
counters is ``derived`` and names how it was obtained in
``reconstruction_basis``. A share read whole from the record carries no
basis. Both directions are refused at validation, so no producer can
persist a fifth quality value or a reconstruction without its basis.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.epoch2.measurement import (
    CaptureSource,
    CounterName,
    CounterSnapshot,
    MeasuredRuntime,
    Observed,
    Unobserved,
    measure_run,
)

AT = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _counters(value: int) -> dict[CounterName, Observed]:
    return {name: Observed(value=Decimal(value)) for name in CounterName}


def _snapshot(value: int, *, source: CaptureSource, divisor: int, derived: bool) -> CounterSnapshot:
    return CounterSnapshot(
        captured_at=AT,
        source=source,
        harness="claude-code",
        model="claude-opus-5-5",
        measurement_version=1,
        concurrent_run_count=divisor,
        derived=derived,
        counters=_counters(value),
    )


def _share(*, source: CaptureSource, divisor: int, derived: bool = False) -> MeasuredRuntime:
    captured = measure_run(
        _snapshot(10, source=source, divisor=divisor, derived=derived),
        _snapshot(40, source=source, divisor=divisor, derived=derived),
        spans=Unobserved(reason="no spans"),
    )
    assert isinstance(captured, MeasuredRuntime)
    return captured


def _row(**overrides: Any) -> dict[str, Any]:
    row = _share(source=CaptureSource.TRANSCRIPT, divisor=1).model_dump(mode="json")
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("source", "divisor", "derived", "quality", "basis"),
    [
        (CaptureSource.TRANSCRIPT, 1, False, "measured", None),
        (CaptureSource.TRANSCRIPT, 3, False, "derived", "recorded_in_transcript"),
        (CaptureSource.SIDECAR, 2, False, "derived", "read_from_disk_now"),
        (CaptureSource.TRANSCRIPT, 1, True, "estimated", "recorded_in_transcript"),
    ],
)
def test_meas_079_every_producer_path_stamps_ladder_quality_and_basis(
    source: CaptureSource, divisor: int, derived: bool, quality: str, basis: str | None
) -> None:
    captured = _share(source=source, divisor=divisor, derived=derived)

    assert captured.measurement_quality == quality
    assert captured.reconstruction_basis == basis


@pytest.mark.parametrize("label", ["reconstructed", "exact", "inferred"])
def test_meas_060_a_label_outside_the_ladder_fails_strict_validation(label: str) -> None:
    with pytest.raises(ValidationError, match="measurement_quality"):
        MeasuredRuntime.model_validate(_row(measurement_quality=label))


def test_meas_079_a_reconstructed_share_without_a_basis_is_refused() -> None:
    row = _share(source=CaptureSource.TRANSCRIPT, divisor=2).model_dump(mode="json")
    row["reconstruction_basis"] = None

    with pytest.raises(ValidationError, match="reconstruction_basis"):
        MeasuredRuntime.model_validate(row)


def test_meas_079_a_recorded_share_with_a_basis_is_refused() -> None:
    with pytest.raises(ValidationError, match="reconstruction_basis"):
        MeasuredRuntime.model_validate(_row(reconstruction_basis="version_table"))


def test_meas_079_a_basis_outside_the_closed_set_is_refused() -> None:
    row = _share(source=CaptureSource.TRANSCRIPT, divisor=2).model_dump(mode="json")
    row["reconstruction_basis"] = "tokenizer_guess"

    with pytest.raises(ValidationError, match="reconstruction_basis"):
        MeasuredRuntime.model_validate(row)


def test_meas_060_a_quality_the_divisor_does_not_support_is_refused() -> None:
    """A sole, read baseline cannot claim to be derived, nor a shared one measured."""
    with pytest.raises(ValidationError, match="divisor 1"):
        MeasuredRuntime.model_validate(
            _row(measurement_quality="derived", reconstruction_basis="recorded_in_transcript")
        )
