"""Canonical Event + EventPayload model tests (P25-W06 / C07b).

These tests pin the canonical shape that C02 streaming, C06 reactivity,
C09 telemetry projection, and C11 webhook ingress all consume. Any
future module that imports ``Event`` MUST go through
``eawf.kernel.store.kinds.event`` (or its re-export at ``eawf.kernel.store``) — never
define its own event envelope.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import get_args

import pytest
from pydantic import ValidationError

from eawf.kernel.store import Event as EventReExport
from eawf.kernel.store import EventKind as EventKindReExport
from eawf.kernel.store import EventPayload as EventPayloadReExport
from eawf.kernel.store.kinds.event import Event, EventKind, EventPayload


def _valid_payload(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "timestamp": datetime(2026, 5, 19, tzinfo=UTC),
        "event_type": "wave.claim",
        "actor": "cli",
        "command": "wave claim",
        "args_hash": "abc123",
        "status": "ok",
        "message": "claimed",
    }
    defaults.update(overrides)
    return defaults


def _valid_event(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "id": "e-2026-05-19-0001-wave_claimed",
        "scope_id": "P25-I01-W06",
        "occurred_at": datetime(2026, 5, 19, tzinfo=UTC),
        "payload": _valid_payload(event_kind="wave_claimed"),
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Single-source-of-truth import contract
# ---------------------------------------------------------------------------


def test_event_importable_from_canonical_module() -> None:
    """C07b §5.4 / Q14: Event lives at eawf.kernel.store.kinds.event."""
    assert Event is EventReExport
    assert EventPayload is EventPayloadReExport
    assert EventKind is EventKindReExport


def test_event_re_exported_at_package_root() -> None:
    """Callers should be able to write ``from eawf.kernel.store import Event``."""
    from eawf.kernel import store

    assert store.Event is Event
    assert store.EventPayload is EventPayload
    assert store.EventKind is EventKind


# ---------------------------------------------------------------------------
# Event outer model
# ---------------------------------------------------------------------------


def test_event_round_trip_json() -> None:
    event = Event(**_valid_event())  # type: ignore[arg-type]
    json_str = event.model_dump_json()
    reloaded = Event.model_validate_json(json_str)
    assert reloaded == event


def test_event_schema_version_defaults_to_1_0() -> None:
    event = Event(**_valid_event())  # type: ignore[arg-type]
    assert event.schema_version == "1.0"


def test_event_schema_version_rejects_non_1_0() -> None:
    data = _valid_event(schema_version="2.0")
    with pytest.raises(ValidationError):
        Event.model_validate(data)


def test_event_rejects_extra_field() -> None:
    data = _valid_event(rogue_field="surprise")
    with pytest.raises(ValidationError):
        Event.model_validate(data)


def test_event_empty_id_rejected() -> None:
    with pytest.raises(ValidationError):
        Event.model_validate(_valid_event(id=""))


def test_event_idempotency_key_optional_default_none() -> None:
    event = Event(**_valid_event())  # type: ignore[arg-type]
    assert event.idempotency_key is None


def test_event_idempotency_key_accepts_string() -> None:
    event = Event.model_validate(_valid_event(idempotency_key="idem-abc-1"))
    assert event.idempotency_key == "idem-abc-1"


def test_event_occurred_at_requires_timezone() -> None:
    data = _valid_event(occurred_at=datetime(2026, 5, 19))  # naive
    with pytest.raises(ValidationError, match="timezone"):
        Event.model_validate(data)


def test_event_payload_is_typed_event_payload() -> None:
    event = Event(**_valid_event())  # type: ignore[arg-type]
    assert isinstance(event.payload, EventPayload)


# ---------------------------------------------------------------------------
# EventPayload inner model
# ---------------------------------------------------------------------------


def test_event_payload_extra_field_rejected() -> None:
    data = _valid_payload(rogue="x")
    with pytest.raises(ValidationError):
        EventPayload.model_validate(data)


def test_event_payload_event_kind_optional_default_none() -> None:
    """v0.3-v0.5 migration window: rows without event_kind stay valid."""
    payload = EventPayload.model_validate(_valid_payload())
    assert payload.event_kind is None


def test_event_payload_event_kind_accepts_closed_literal_values() -> None:
    for kind in get_args(EventKind):
        payload = EventPayload.model_validate(_valid_payload(event_kind=kind))
        assert payload.event_kind == kind


def test_event_payload_event_kind_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        EventPayload.model_validate(_valid_payload(event_kind="not_a_real_kind"))


def test_event_payload_extras_default_empty_dict() -> None:
    payload = EventPayload.model_validate(_valid_payload())
    assert payload.extras == {}


def test_event_payload_extras_accepts_scalar_values() -> None:
    payload = EventPayload.model_validate(
        _valid_payload(extras={"k1": "v1", "k2": 42, "k3": 3.14, "k4": True})
    )
    assert payload.extras == {"k1": "v1", "k2": 42, "k3": 3.14, "k4": True}


def test_event_payload_extras_rejects_nested_dict() -> None:
    with pytest.raises(ValidationError):
        EventPayload.model_validate(_valid_payload(extras={"k": {"nested": "no"}}))


def test_event_payload_error_class_default_none() -> None:
    payload = EventPayload.model_validate(_valid_payload())
    assert payload.error_class is None


def test_event_payload_error_class_accepts_string() -> None:
    payload = EventPayload.model_validate(_valid_payload(error_class="LockConflict"))
    assert payload.error_class == "LockConflict"


# ---------------------------------------------------------------------------
# EventKind closed literal sanity
# ---------------------------------------------------------------------------


def test_event_kind_includes_core_state_lifecycle_kinds() -> None:
    """C07b §5.4: state lifecycle kinds MUST be in the closed enum."""
    members = set(get_args(EventKind))
    expected_core = {
        "state_mutated",
        "wave_claimed",
        "wave_closed",
        "phase_activated",
        "phase_closed",
        "iter_activated",
        "iter_closed",
    }
    assert expected_core.issubset(members)


def test_event_kind_includes_runtime_and_session_kinds() -> None:
    """V5 runtime + V8 session kinds MUST be in the closed enum."""
    members = set(get_args(EventKind))
    expected = {
        "runtime_switched",
        "runtime_paused",
        "runtime_auth_failed",
        "runtime_unavailable",
        "session_continued",
        "session_failover",
        "session_handle_pruned",
        "cache_mislayer_alarm",
        "dispatch_cost",
    }
    assert expected.issubset(members)


def test_event_kind_includes_subscription_and_observability_kinds() -> None:
    """C02 subscription + C09 telemetry kinds MUST be in the closed enum."""
    members = set(get_args(EventKind))
    expected = {
        "audit_emitted",
        "memory_appended",
        "spec_validated",
        "config_reloaded",
        "subscription_lag",
        "subscription_dropped",
    }
    assert expected.issubset(members)
