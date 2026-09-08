"""Unit tests for deriving telemetry session rows from session-close events.

Pins the two halves of the session producer:

- **Derivation** — every ``session_closed`` event envelope folds into exactly
  one typed :class:`~eawf.observability.telemetry.models.TelemetrySession`, with
  the duration taken from the open/close timestamps the event carries.
- **Version pin** — a pre-bump payload (the flat, untyped session close that
  predates the typed payload) raises rather than projecting nothing, at both
  the read boundary (the aggregator) and the write boundary
  (``validate_event_payload``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventPayload, validate_event_payload
from eawf.kernel.store.kinds.events.session_closed import (
    SESSION_PAYLOAD_SCHEMA_VERSION,
    SessionClosedPayload,
)
from eawf.observability.telemetry.aggregator import (
    SessionPayloadSchemaError,
    session_from_envelope,
)
from eawf.observability.telemetry.models import TelemetrySession

_OPENED = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
_CLOSED = datetime(2026, 5, 22, 12, 30, tzinfo=UTC)
_HANDLE = "urn:eawf:v1:session-log:claude:0f0f0f0f"


def _payload(**overrides: Any) -> dict[str, Any]:
    """Return a valid ``session_closed`` payload mapping with *overrides* applied."""
    data: dict[str, Any] = SessionClosedPayload(
        timestamp=_CLOSED,
        opened_at=_OPENED,
        session_id="sess-1",
        runtime="claude",
        session_log_handle=_HANDLE,
        wave_id="W31",
        attempt_id="att-1",
        model="claude-opus-4",
        end_marker="clean_stop",
        input_tokens=120,
        output_tokens=45,
        cache_creation_input_tokens=10,
        cache_read_input_tokens=90,
    ).model_dump(mode="json")
    data.update(overrides)
    return data


def _envelope(
    payload: dict[str, Any],
    *,
    env_id: str = "EV-1",
    kind: StoreKind = StoreKind.EVENT,
) -> Envelope:
    """Wrap *payload* in a store envelope of *kind* (event kind by default)."""
    return Envelope(
        id=env_id,
        kind=kind,
        scope_id="W31",
        created_at=_CLOSED,
        updated_at=None,
        summary=f"session_closed {env_id}",
        payload=payload,
    )


def _flat_pre_bump_payload() -> dict[str, Any]:
    """Return the pre-bump session close: a flat event with facts in ``extras``."""
    return EventPayload(
        timestamp=_CLOSED,
        event_type="session_closed",
        actor="daemon",
        command="dispatch_runner.close_session",
        args_hash="",
        status="ok",
        message="session closed",
        extras={"session_id": "sess-1", "runtime": "claude"},
    ).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def test_session_close_event_projects_typed_row() -> None:
    """Each session-close event derives exactly one typed session row."""
    envelopes = [
        _envelope(_payload(session_id=f"sess-{idx}"), env_id=f"EV-{idx}") for idx in range(3)
    ]

    rows = [session_from_envelope(envelope) for envelope in envelopes]

    assert len(rows) == len(envelopes)
    assert all(isinstance(row, TelemetrySession) for row in rows)
    assert [row.session_id for row in rows if row is not None] == [
        "sess-0",
        "sess-1",
        "sess-2",
    ]


def test_session_from_envelope_maps_every_payload_fact() -> None:
    """The derived row carries the payload's ids, tallies, and marker."""
    row = session_from_envelope(_envelope(_payload()))

    assert row is not None
    assert row.session_id == "sess-1"
    assert row.project_id == ""
    assert row.runtime == "claude"
    assert row.wave_id == "W31"
    assert row.attempt_id == "att-1"
    assert row.session_log_path == _HANDLE
    assert row.started_at == _OPENED
    assert row.ended_at == _CLOSED
    assert row.duration_ms == 30 * 60 * 1000
    assert row.model_primary == "claude-opus-4"
    assert row.total_input_tokens == 120
    assert row.total_output_tokens == 45
    assert row.total_cache_write == 10
    assert row.total_cache_read == 90
    assert row.end_marker == "clean_stop"


def test_session_from_envelope_duration_is_open_to_close_span() -> None:
    """The row duration equals the close-minus-open timestamp difference."""
    closed = _OPENED + timedelta(milliseconds=1)

    row = session_from_envelope(_envelope(_payload(timestamp=closed.isoformat())))

    assert row is not None
    assert row.duration_ms == 1


def test_session_from_envelope_zero_duration_for_instant_session() -> None:
    """A session that opens and closes at the same instant projects duration zero."""
    row = session_from_envelope(_envelope(_payload(timestamp=_OPENED.isoformat())))

    assert row is not None
    assert row.duration_ms == 0


def test_session_from_envelope_allows_interactive_session_without_wave() -> None:
    """An interactive session with no wave / attempt still projects a row."""
    row = session_from_envelope(_envelope(_payload(wave_id=None, attempt_id=None, model=None)))

    assert row is not None
    assert row.wave_id is None
    assert row.attempt_id is None
    assert row.model_primary is None


def test_session_from_envelope_skips_other_event_type() -> None:
    """An event this derivation does not own yields no row."""
    other = _payload()
    other["event_type"] = "dispatch_cost"

    assert session_from_envelope(_envelope(other)) is None


def test_session_from_envelope_skips_non_event_store_kind() -> None:
    """A non-event store kind yields no row even with a session-close payload."""
    assert session_from_envelope(_envelope(_payload(), kind=StoreKind.AUDIT)) is None


# ---------------------------------------------------------------------------
# Version pin (read boundary)
# ---------------------------------------------------------------------------


def test_pre_bump_payload_is_rejected() -> None:
    """A pre-bump session close raises instead of projecting nothing."""
    envelope = _envelope(_flat_pre_bump_payload())

    with pytest.raises(SessionPayloadSchemaError) as excinfo:
        session_from_envelope(envelope)

    assert SESSION_PAYLOAD_SCHEMA_VERSION in str(excinfo.value)
    assert "EV-1" in str(excinfo.value)


def test_session_from_envelope_rejects_older_declared_version() -> None:
    """An explicitly older payload version is rejected, not silently skipped."""
    with pytest.raises(SessionPayloadSchemaError):
        session_from_envelope(_envelope(_payload(payload_schema_version="1.0")))


def test_session_from_envelope_rejects_missing_session_id() -> None:
    """A current-version payload missing a required field fails validation."""
    body = _payload()
    del body["session_id"]

    with pytest.raises(ValidationError):
        session_from_envelope(_envelope(body))


def test_session_from_envelope_rejects_empty_session_id() -> None:
    """An empty session id is rejected at the boundary, not stored as a blank key."""
    with pytest.raises(ValidationError):
        session_from_envelope(_envelope(_payload(session_id="")))


def test_session_from_envelope_rejects_unknown_runtime() -> None:
    """A runtime outside the closed triple fails validation."""
    with pytest.raises(ValidationError):
        session_from_envelope(_envelope(_payload(runtime="gemini")))


def test_session_from_envelope_rejects_non_numeric_tokens() -> None:
    """A non-numeric token tally fails validation rather than coercing to zero."""
    with pytest.raises(ValidationError):
        session_from_envelope(_envelope(_payload(input_tokens="lots")))


# ---------------------------------------------------------------------------
# Payload model contract
# ---------------------------------------------------------------------------


def test_session_closed_payload_rejects_close_before_open() -> None:
    """A close that precedes its own open is rejected rather than negative-duration."""
    with pytest.raises(ValidationError, match="timestamp cannot precede opened_at"):
        SessionClosedPayload.model_validate(_payload(timestamp="2026-05-22T11:00:00+00:00"))


def test_session_closed_payload_rejects_negative_tokens() -> None:
    """A negative token tally is out of range."""
    with pytest.raises(ValidationError):
        SessionClosedPayload.model_validate(_payload(output_tokens=-1))


def test_session_closed_payload_rejects_extra_field() -> None:
    """The payload is closed: an unknown key fails fast."""
    with pytest.raises(ValidationError):
        SessionClosedPayload.model_validate(_payload(rogue_field="surprise"))


def test_session_closed_payload_defaults_to_pinned_version() -> None:
    """A payload built in-process carries the pinned schema version."""
    body = _payload()

    assert body["payload_schema_version"] == SESSION_PAYLOAD_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Version pin (write boundary)
# ---------------------------------------------------------------------------


def test_validate_event_payload_routes_session_closed_to_typed_arm() -> None:
    """The canonical event validator dispatches the tag to the typed payload."""
    parsed = validate_event_payload(_payload())

    assert isinstance(parsed, SessionClosedPayload)


def test_validate_event_payload_rejects_flat_session_closed() -> None:
    """A pre-bump flat session close can no longer be appended to the event store."""
    with pytest.raises(ValidationError):
        validate_event_payload(_flat_pre_bump_payload())
