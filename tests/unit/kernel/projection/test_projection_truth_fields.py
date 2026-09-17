"""Every displayed field validates as a ``TruthField`` and every projection has a strict header.

A value validates only together with its state, truth kind, precision, measurement quality
and freshness enums, its producer and revision, and its provenance; a missing value says
why and carries none. The header refuses unknown fields, a projection kind no projection
carries, and a completeness claim its connection cannot back.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import StrictInt, StrictStr, ValidationError

from eawf.kernel.projection.read_models import READ_MODEL_BY_KIND, ReadModelKind
from eawf.kernel.projection.truth import (
    Completeness,
    ConnectionState,
    Freshness,
    Precision,
    ProjectionHeader,
    TruthField,
    TruthKind,
    TruthState,
)
from eawf.kernel.state.enums import MeasurementQuality

AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
MISSING_STATES = [state for state in TruthState if state is not TruthState.KNOWN]
FIELD_REQUIRED = (
    "value",
    "state",
    "truth_kind",
    "producer",
    "producer_revision",
    "precision",
    "measurement_quality",
    "freshness",
    "provenance_refs",
)
HEADER_FIELDS = (
    "schema_version",
    "projection_kind",
    "scope_id",
    "projection_revision",
    "source_cursor",
    "generated_at",
    "observed_at",
    "connection_state",
    "completeness",
    "freshness",
    "producer_refs",
    "policy_revision",
)


def _error_fields(excinfo: pytest.ExceptionInfo[ValidationError]) -> set[object]:
    """Return the top-level field each error in ``excinfo`` names."""
    return {error["loc"][0] for error in excinfo.value.errors() if error["loc"]}


def _known(**overrides: Any) -> dict[str, Any]:
    """Return the fields of a valid known count, with ``overrides`` applied."""
    fields: dict[str, Any] = {
        "value": 3,
        "state": "known",
        "truth_kind": "derived",
        "producer": "daemon.projection",
        "producer_revision": 41,
        "occurred_at": AT,
        "received_at": AT + timedelta(seconds=1),
        "precision": "exact",
        "measurement_quality": "exact",
        "freshness": "live",
        "provenance_refs": ["EVT-0001"],
    }
    return fields | overrides


def _missing(state: TruthState, **overrides: Any) -> dict[str, Any]:
    """Return the fields of a valid missing value in ``state``, with ``overrides`` applied."""
    return (
        _known(
            value=None,
            state=state,
            precision="unavailable",
            measurement_quality="unavailable",
            provenance_refs=[],
            missing_reason="no outcome recorded",
        )
        | overrides
    )


def _header(**overrides: Any) -> dict[str, Any]:
    """Return the fields of a valid live header, with ``overrides`` applied."""
    fields: dict[str, Any] = {
        "schema_version": "1.0",
        "projection_kind": "scope_home_view",
        "scope_id": "EAWF",
        "projection_revision": 41208,
        "source_cursor": "c-000041208",
        "generated_at": AT,
        "observed_at": AT - timedelta(seconds=2),
        "connection_state": "live",
        "completeness": "complete",
        "freshness": "live",
        "producer_refs": ["daemon.projection"],
        "policy_revision": 47,
    }
    return fields | overrides


def test_truth_field_enums_are_the_declared_closed_sets() -> None:
    assert [s.value for s in TruthState] == [
        "known",
        "unknown",
        "unavailable",
        "denied",
        "purged",
        "invalidated",
    ]
    assert [k.value for k in TruthKind] == [
        "stored",
        "observed",
        "derived",
        "estimated",
        "imported",
    ]
    assert [p.value for p in Precision] == ["exact", "bounded", "approximate", "unavailable"]
    assert [f.value for f in Freshness] == [
        "live",
        "aging",
        "stale",
        "offline_snapshot",
        "replaying",
        "gap",
    ]
    assert [c.value for c in Completeness] == ["complete", "partial", "unverified"]
    assert len(ConnectionState) == 8
    assert ConnectionState.SNAPSHOT_REQUIRED != ConnectionState.SNAPSHOT_LOADING


def test_truth_field_known_value_validates() -> None:
    field = TruthField[StrictInt].model_validate(_known())
    assert field.value == 3
    assert field.state is TruthState.KNOWN
    assert field.truth_kind is TruthKind.DERIVED
    assert field.precision is Precision.EXACT
    assert field.measurement_quality is MeasurementQuality.EXACT
    assert field.freshness is Freshness.LIVE
    assert field.provenance_refs == ("EVT-0001",)
    assert field.missing_reason is None


@pytest.mark.parametrize("truth_kind", list(TruthKind))
def test_truth_field_each_truth_kind_validates(truth_kind: TruthKind) -> None:
    field = TruthField[StrictInt].model_validate(_known(truth_kind=truth_kind, precision="bounded"))
    assert field.truth_kind is truth_kind


@pytest.mark.parametrize("precision", [p for p in Precision if p is not Precision.UNAVAILABLE])
def test_truth_field_each_available_precision_validates(precision: Precision) -> None:
    assert TruthField[StrictInt].model_validate(_known(precision=precision)).precision is precision


@pytest.mark.parametrize(
    "quality", [q for q in MeasurementQuality if q is not MeasurementQuality.UNAVAILABLE]
)
def test_truth_field_each_available_quality_validates(quality: MeasurementQuality) -> None:
    field = TruthField[StrictInt].model_validate(_known(measurement_quality=quality))
    assert field.measurement_quality is quality


@pytest.mark.parametrize("freshness", list(Freshness))
def test_truth_field_each_freshness_validates(freshness: Freshness) -> None:
    assert TruthField[StrictInt].model_validate(_known(freshness=freshness)).freshness is freshness


@pytest.mark.parametrize("state", MISSING_STATES)
def test_truth_field_each_missing_state_validates_without_value(state: TruthState) -> None:
    field = TruthField[StrictInt].model_validate(_missing(state))
    assert field.value is None
    assert field.state is state
    assert field.precision is Precision.UNAVAILABLE
    assert field.measurement_quality is MeasurementQuality.UNAVAILABLE
    assert field.missing_reason == "no outcome recorded"


@pytest.mark.parametrize("state", MISSING_STATES)
def test_truth_field_missing_state_with_value_rejected(state: TruthState) -> None:
    with pytest.raises(ValidationError, match="only a known field carries a value"):
        TruthField[StrictInt].model_validate(_missing(state, value=0))


@pytest.mark.parametrize("state", MISSING_STATES)
def test_truth_field_missing_state_without_reason_rejected(state: TruthState) -> None:
    with pytest.raises(ValidationError, match="a missing value names its reason"):
        TruthField[StrictInt].model_validate(_missing(state, missing_reason=None))


@pytest.mark.parametrize(
    "overrides",
    [{"precision": "exact"}, {"measurement_quality": "estimated"}],
    ids=["precision", "quality"],
)
def test_truth_field_missing_value_claiming_precision_rejected(overrides: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="a missing value has no precision or quality"):
        TruthField[StrictInt].model_validate(_missing(TruthState.UNKNOWN, **overrides))


def test_truth_field_known_state_without_value_is_the_declared_no_value() -> None:
    field = TruthField[StrictStr].model_validate(
        _known(value=None, precision="unavailable", measurement_quality="unavailable")
    )
    assert field.value is None
    assert field.state is TruthState.KNOWN


def test_truth_field_known_state_with_reason_rejected() -> None:
    with pytest.raises(ValidationError, match="a known field has no missing_reason"):
        TruthField[StrictInt].model_validate(_known(missing_reason="stale"))


@pytest.mark.parametrize(
    "overrides",
    [{"precision": "unavailable"}, {"measurement_quality": "unavailable"}],
    ids=["precision", "quality"],
)
def test_truth_field_value_without_precision_rejected(overrides: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="a value states precision and quality"):
        TruthField[StrictInt].model_validate(_known(**overrides))


def test_truth_field_known_without_provenance_rejected() -> None:
    with pytest.raises(ValidationError, match="a known field names its provenance"):
        TruthField[StrictInt].model_validate(_known(provenance_refs=[]))


def test_truth_field_exact_estimate_rejected() -> None:
    with pytest.raises(ValidationError, match="an estimate is not exact"):
        TruthField[StrictInt].model_validate(_known(truth_kind="estimated"))


def test_truth_field_received_before_occurred_rejected() -> None:
    with pytest.raises(ValidationError, match="received_at precedes occurred_at"):
        TruthField[StrictInt].model_validate(_known(received_at=AT - timedelta(seconds=1)))


def test_truth_field_equal_timestamps_and_absent_timestamps_validate() -> None:
    assert TruthField[StrictInt].model_validate(_known(received_at=AT)).received_at == AT
    field = TruthField[StrictInt].model_validate(_known(occurred_at=None, received_at=None))
    assert field.occurred_at is None
    assert field.received_at is None


def test_truth_field_offset_timestamp_normalised_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    field = TruthField[StrictInt].model_validate(
        _known(occurred_at=AT.astimezone(plus_two), received_at=AT.astimezone(plus_two))
    )
    assert field.occurred_at == AT
    assert field.occurred_at is not None
    assert field.occurred_at.utcoffset() == timedelta(0)


def test_truth_field_several_contradictions_reported_together() -> None:
    with pytest.raises(ValidationError) as excinfo:
        TruthField[StrictInt].model_validate(_known(provenance_refs=[], truth_kind="estimated"))
    message = str(excinfo.value)
    assert "a known field names its provenance" in message
    assert "an estimate is not exact" in message


@pytest.mark.parametrize("name", FIELD_REQUIRED)
def test_truth_field_missing_required_field_rejected(name: str) -> None:
    fields = _known()
    del fields[name]
    with pytest.raises(ValidationError, match="Field required") as excinfo:
        TruthField[StrictInt].model_validate(fields)
    assert _error_fields(excinfo) == {name}


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("state", "fresh"),
        ("truth_kind", "guessed"),
        ("precision", "rough"),
        ("measurement_quality", "approximate"),
        ("freshness", "recent"),
        ("producer", "   "),
        ("producer_revision", 0),
        ("producer_revision", "41"),
        ("provenance_refs", [""]),
        ("occurred_at", AT.replace(tzinfo=None)),
    ],
)
def test_truth_field_invalid_value_rejected(name: str, value: object) -> None:
    with pytest.raises(ValidationError) as excinfo:
        TruthField[StrictInt].model_validate(_known(**{name: value}))
    assert _error_fields(excinfo) == {name}


def test_truth_field_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TruthField[StrictInt].model_validate(_known(quality="exact"))


def test_truth_field_value_of_wrong_type_rejected() -> None:
    with pytest.raises(ValidationError, match="valid integer") as excinfo:
        TruthField[StrictInt].model_validate(_known(value="3"))
    assert _error_fields(excinfo) == {"value"}


def test_truth_field_frozen_after_validation() -> None:
    field = TruthField[StrictInt].model_validate(_known())
    with pytest.raises(ValidationError, match="frozen"):
        field.value = 4


def test_truth_field_json_round_trip_keeps_every_fact() -> None:
    field = TruthField[StrictStr].model_validate(_known(value="BAT-0001", truth_kind="stored"))
    assert TruthField[StrictStr].model_validate_json(field.model_dump_json()) == field


def test_projection_header_valid_live_header_validates() -> None:
    header = ProjectionHeader.model_validate(_header())
    assert header.projection_kind is ReadModelKind.SCOPE_HOME_VIEW
    assert header.connection_state is ConnectionState.LIVE
    assert header.completeness is Completeness.COMPLETE
    assert header.producer_refs == ("daemon.projection",)


@pytest.mark.parametrize("state", list(ConnectionState))
def test_projection_header_each_connection_state_validates_partial(state: ConnectionState) -> None:
    header = ProjectionHeader.model_validate(
        _header(connection_state=state, completeness="partial")
    )
    assert header.connection_state is state


@pytest.mark.parametrize("state", [s for s in ConnectionState if s is not ConnectionState.LIVE])
def test_projection_header_complete_claim_off_live_rejected(state: ConnectionState) -> None:
    with pytest.raises(ValidationError, match="cannot claim completeness"):
        ProjectionHeader.model_validate(_header(connection_state=state))


def test_projection_header_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProjectionHeader.model_validate(_header(row_offset=3))


@pytest.mark.parametrize("name", HEADER_FIELDS)
def test_projection_header_missing_field_rejected(name: str) -> None:
    fields = _header()
    del fields[name]
    with pytest.raises(ValidationError, match="Field required") as excinfo:
        ProjectionHeader.model_validate(fields)
    assert _error_fields(excinfo) == {name}


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("schema_version", "2.0"),
        ("schema_version", 1),
        ("schema_version", True),
        ("projection_kind", "raw_state"),
        ("scope_id", ""),
        ("projection_revision", 0),
        ("source_cursor", ""),
        ("connection_state", "snapshot"),
        ("completeness", "all"),
        ("freshness", "fresh"),
        ("producer_refs", []),
        ("policy_revision", -1),
    ],
)
def test_projection_header_invalid_value_rejected(name: str, value: object) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ProjectionHeader.model_validate(_header(**{name: value}))
    assert _error_fields(excinfo) == {name}


def test_projection_header_process_frame_kind_rejected() -> None:
    assert not READ_MODEL_BY_KIND[ReadModelKind.PROCESS_FRAME].projection_backed
    with pytest.raises(ValidationError, match="no projection carries process_frame"):
        ProjectionHeader.model_validate(_header(projection_kind="process_frame"))


@pytest.mark.parametrize(
    "kind", [k for k in ReadModelKind if READ_MODEL_BY_KIND[k].projection_backed]
)
def test_projection_header_each_projection_backed_kind_validates(kind: ReadModelKind) -> None:
    assert ProjectionHeader.model_validate(_header(projection_kind=kind)).projection_kind is kind


def test_projection_header_observed_after_generated_rejected() -> None:
    with pytest.raises(ValidationError, match="observed_at is after generated_at"):
        ProjectionHeader.model_validate(_header(observed_at=AT + timedelta(microseconds=1)))


def test_projection_header_observed_at_generated_validates() -> None:
    assert ProjectionHeader.model_validate(_header(observed_at=AT)).observed_at == AT


def test_projection_header_frozen_after_validation() -> None:
    header = ProjectionHeader.model_validate(_header())
    with pytest.raises(ValidationError, match="frozen"):
        header.policy_revision = 48
