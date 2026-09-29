"""The one builder every register count is stated through.

The Activity, Attention and budget readings each built their counts with
a private copy of the same truth-field constructor; they now share
:func:`count_field` and :func:`revision_of`. This pins the three shapes a
count takes -- exact, estimated naming why, unknown naming why -- and the
cursor-plus-one revision every derived field of a view is stated at.
"""

from __future__ import annotations

import pytest

from eawf.kernel.projection.compute import PROJECTION_PRODUCER
from eawf.kernel.projection.registers import count_field
from eawf.kernel.projection.truth import Precision, TruthKind, TruthState
from eawf.kernel.state.enums import MeasurementQuality


def test_an_exact_count_is_known_and_derived() -> None:
    field = count_field(value=3, revision=7, refs=("activity",), reason=None)

    assert (field.value, field.state, field.truth_kind) == (
        "3",
        TruthState.KNOWN,
        TruthKind.DERIVED,
    )
    assert field.precision is Precision.EXACT
    assert field.measurement_quality is MeasurementQuality.EXACT
    assert field.producer == PROJECTION_PRODUCER
    assert field.producer_revision == 7
    assert field.provenance_refs == ("activity",)


def test_a_zero_count_is_a_count_not_an_unknown() -> None:
    field = count_field(value=0, revision=1, refs=("attention",), reason=None)

    assert field.value == "0"
    assert field.state is TruthState.KNOWN


def test_an_estimate_is_known_but_bounded() -> None:
    field = count_field(value=2, revision=1, refs=("activity",), reason="stale", estimate=True)

    assert field.truth_kind is TruthKind.ESTIMATED
    assert field.precision is Precision.BOUNDED
    assert field.measurement_quality is MeasurementQuality.ESTIMATED


def test_an_absent_count_is_unknown_naming_why() -> None:
    field = count_field(value=None, revision=1, refs=("scope",), reason="no producer")

    assert field.value is None
    assert field.state is TruthState.UNKNOWN
    assert field.precision is Precision.UNAVAILABLE
    assert field.missing_reason == "no producer"


def test_an_unknown_count_without_a_reason_is_refused() -> None:
    with pytest.raises(ValueError, match="missing_reason"):
        count_field(value=None, revision=1, refs=("scope",), reason=None)


def test_a_revision_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="producer_revision"):
        count_field(value=1, revision=0, refs=("scope",), reason=None)
