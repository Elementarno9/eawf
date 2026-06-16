"""Tests for the C09 typed event-payload discriminated union (P27-I01-W09).

Pins the C09-owned ``EventPayload`` sub-classes per the C09 spec §5.11
and the §5.8 correlation-ID chain:

* each sub-class validates via its ``event_type`` discriminator,
* a wrong-shape body fails fast with ``ValidationError``,
* the trace-ID chain lives on the shared union base, and
* the C09-local union dispatches the right sub-class per ``event_type``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from eawf.kernel.store.kinds.events import (
    AgentOutputChunkPayload,
    C09EventPayloadUnion,
    CacheMislayerAlarmPayload,
    DispatchCostPayload,
    RuntimeSwitchedPayload,
    SessionContinuedPayload,
    SessionFailoverPayload,
    TracedEventPayload,
)

_TS = datetime(2026, 5, 22, tzinfo=UTC)

_UNION_ADAPTER: TypeAdapter[
    RuntimeSwitchedPayload
    | SessionContinuedPayload
    | SessionFailoverPayload
    | DispatchCostPayload
    | CacheMislayerAlarmPayload
    | AgentOutputChunkPayload
] = TypeAdapter(C09EventPayloadUnion)


# ---------------------------------------------------------------------------
# Valid-payload builders (one per sub-class)
# ---------------------------------------------------------------------------


def _runtime_switched(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "event_type": "runtime_switched",
        "timestamp": _TS,
        "wave_id": "W09",
        "attempt_id_from": "att-1",
        "attempt_id_to": "att-2",
        "runtime_from": "claude",
        "runtime_to": "codex",
        "cause": "RUNTIME_SERVER_ERROR",
        "error_detail": "<scrubbed>",
        "idempotency_key": "idem-1",
    }
    data.update(overrides)
    return data


def _session_continued(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "event_type": "session_continued",
        "timestamp": _TS,
        "wave_id": "W09",
        "attempt_id": "att-1",
        "runtime": "claude",
        "session_handle": "sess-abc",
        "session_log_path": "logs/sess-abc.jsonl",
        "prior_turn_count": 7,
    }
    data.update(overrides)
    return data


def _session_failover(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "event_type": "session_failover",
        "timestamp": _TS,
        "wave_id": "W09",
        "attempt_id_continue": "att-1",
        "attempt_id_fresh": "att-2",
        "runtime": "opencode",
        "reason": "session_expired",
        "prior_session_handle": "sess-old",
    }
    data.update(overrides)
    return data


def _dispatch_cost(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "event_type": "dispatch_cost",
        "timestamp": _TS,
        "wave_id": "W09",
        "attempt_id": "att-1",
        "runtime": "claude",
        "model": "claude-opus-4-7",
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_input_tokens": 2000,
        "cache_read_input_tokens": 8000,
        "cost_usd": Decimal("0.1234"),
        "pricing_version": "2026.05.17",
    }
    data.update(overrides)
    return data


def _cache_mislayer(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "event_type": "cache_mislayer_alarm",
        "timestamp": _TS,
        "runtime": "claude",
        "scope_id": "P27-I01-W09",
        "window_seconds": 300,
        "cache_creation_floor_tokens": 2000,
        "ratio_threshold": 10.0,
        "observed_ratio_a": 12.5,
        "observed_ratio_b": 11.0,
        "observed_cc_a": 4000,
        "observed_cc_b": 3500,
    }
    data.update(overrides)
    return data


def _agent_output_chunk(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "event_type": "agent.output.chunk",
        "timestamp": _TS,
        "wave_id": "W09",
        "session_id": "sess-abc",
        "seq": 0,
        "lines": "first line\nsecond line",
    }
    data.update(overrides)
    return data


_BUILDERS = {
    "runtime_switched": (_runtime_switched, RuntimeSwitchedPayload),
    "session_continued": (_session_continued, SessionContinuedPayload),
    "session_failover": (_session_failover, SessionFailoverPayload),
    "dispatch_cost": (_dispatch_cost, DispatchCostPayload),
    "cache_mislayer_alarm": (_cache_mislayer, CacheMislayerAlarmPayload),
    "agent.output.chunk": (_agent_output_chunk, AgentOutputChunkPayload),
}


# ---------------------------------------------------------------------------
# Direct sub-class validation (happy path)
# ---------------------------------------------------------------------------


def test_runtime_switched_validates() -> None:
    payload = RuntimeSwitchedPayload.model_validate(_runtime_switched())
    assert payload.event_type == "runtime_switched"
    assert payload.runtime_from == "claude"
    assert payload.runtime_to == "codex"


def test_session_continued_validates() -> None:
    payload = SessionContinuedPayload.model_validate(_session_continued())
    assert payload.event_type == "session_continued"
    assert payload.prior_turn_count == 7


def test_session_failover_validates() -> None:
    payload = SessionFailoverPayload.model_validate(_session_failover())
    assert payload.event_type == "session_failover"
    assert payload.reason == "session_expired"


def test_dispatch_cost_validates() -> None:
    payload = DispatchCostPayload.model_validate(_dispatch_cost())
    assert payload.event_type == "dispatch_cost"
    assert payload.cost_usd == Decimal("0.1234")


def test_dispatch_cost_allows_none_wave_and_attempt_for_interactive() -> None:
    payload = DispatchCostPayload.model_validate(_dispatch_cost(wave_id=None, attempt_id=None))
    assert payload.wave_id is None
    assert payload.attempt_id is None


def test_cache_mislayer_validates() -> None:
    payload = CacheMislayerAlarmPayload.model_validate(_cache_mislayer())
    assert payload.event_type == "cache_mislayer_alarm"
    assert payload.ratio_threshold == 10.0


def test_cache_mislayer_allows_none_scope_for_interactive() -> None:
    payload = CacheMislayerAlarmPayload.model_validate(_cache_mislayer(scope_id=None))
    assert payload.scope_id is None


# ---------------------------------------------------------------------------
# extra="forbid" + wrong-shape rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(_BUILDERS))
def test_subclass_rejects_extra_field(kind: str) -> None:
    builder, model = _BUILDERS[kind]
    with pytest.raises(ValidationError):
        model.model_validate(builder(rogue_field="surprise"))


def test_runtime_switched_rejects_unknown_runtime() -> None:
    with pytest.raises(ValidationError):
        RuntimeSwitchedPayload.model_validate(_runtime_switched(runtime_to="gemini"))


def test_session_failover_rejects_unknown_reason() -> None:
    with pytest.raises(ValidationError):
        SessionFailoverPayload.model_validate(_session_failover(reason="exploded"))


def test_dispatch_cost_rejects_non_numeric_tokens() -> None:
    with pytest.raises(ValidationError):
        DispatchCostPayload.model_validate(_dispatch_cost(input_tokens="lots"))


def test_runtime_switched_rejects_missing_required_field() -> None:
    body = _runtime_switched()
    del body["attempt_id_to"]
    with pytest.raises(ValidationError):
        RuntimeSwitchedPayload.model_validate(body)


# ---------------------------------------------------------------------------
# Trace-ID chain on the shared union base
# ---------------------------------------------------------------------------


def test_trace_ids_present_on_union_base() -> None:
    fields = TracedEventPayload.model_fields
    assert {"trace_request_id", "trace_wave_id", "trace_attempt_id"} <= set(fields)


@pytest.mark.parametrize("kind", list(_BUILDERS))
def test_trace_ids_default_none_on_each_subclass(kind: str) -> None:
    builder, model = _BUILDERS[kind]
    payload = model.model_validate(builder())
    assert payload.trace_request_id is None
    assert payload.trace_wave_id is None
    assert payload.trace_attempt_id is None


def test_trace_ids_round_trip_when_set() -> None:
    payload = RuntimeSwitchedPayload.model_validate(
        _runtime_switched(
            trace_request_id="req-uuid",
            trace_wave_id="W09",
            trace_attempt_id="att-uuid",
        )
    )
    assert payload.trace_request_id == "req-uuid"
    assert payload.trace_wave_id == "W09"
    assert payload.trace_attempt_id == "att-uuid"


def test_every_subclass_derives_from_traced_base() -> None:
    for _builder, model in _BUILDERS.values():
        assert issubclass(model, TracedEventPayload)


# ---------------------------------------------------------------------------
# C09-local union discriminator dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(_BUILDERS))
def test_union_dispatches_correct_subclass(kind: str) -> None:
    builder, model = _BUILDERS[kind]
    parsed = _UNION_ADAPTER.validate_python(builder())
    assert isinstance(parsed, model)
    assert parsed.event_type == kind


def test_union_rejects_unknown_event_type() -> None:
    with pytest.raises(ValidationError):
        _UNION_ADAPTER.validate_python(_runtime_switched(event_type="not_a_kind"))


def test_union_rejects_mismatched_body_for_tag() -> None:
    """A dispatch_cost tag with a runtime_switched body must fail fast."""
    mismatched = _runtime_switched(event_type="dispatch_cost")
    with pytest.raises(ValidationError):
        _UNION_ADAPTER.validate_python(mismatched)


def test_union_round_trips_through_json() -> None:
    for builder, model in _BUILDERS.values():
        instance = model.model_validate(builder())
        json_str = instance.model_dump_json()
        reloaded = _UNION_ADAPTER.validate_json(json_str)
        assert reloaded == instance


def test_union_member_is_strict_base_model() -> None:
    """Every union member is a closed (extra=forbid) Pydantic model."""
    for _builder, model in _BUILDERS.values():
        assert issubclass(model, BaseModel)
        assert model.model_config.get("extra") == "forbid"


# ---------------------------------------------------------------------------
# validate_event_payload — typed-aware EVENT payload dispatch (P27-I02-W06)
# ---------------------------------------------------------------------------


def test_c09_event_type_tags_match_union_members() -> None:
    from eawf.kernel.store.kinds.events import C09_EVENT_TYPE_TAGS

    assert frozenset(_BUILDERS) == C09_EVENT_TYPE_TAGS


@pytest.mark.parametrize("kind", list(_BUILDERS))
def test_validate_event_payload_routes_c09_to_union(kind: str) -> None:
    from eawf.kernel.store.kinds.event import validate_event_payload

    builder, model = _BUILDERS[kind]
    parsed = validate_event_payload(builder())
    assert isinstance(parsed, model)


def test_validate_event_payload_routes_flat_to_event_payload() -> None:
    from eawf.kernel.store.kinds.event import EventPayload, validate_event_payload

    flat = {
        "timestamp": _TS,
        "event_type": "state_mutated",
        "actor": "cli",
        "command": "eawf wave claim",
        "args_hash": "abc",
        "status": "ok",
        "message": "claimed",
    }
    parsed = validate_event_payload(flat)
    assert isinstance(parsed, EventPayload)
    assert parsed.event_type == "state_mutated"


def test_validate_event_payload_rejects_garbage_c09_body() -> None:
    from eawf.kernel.store.kinds.event import validate_event_payload

    with pytest.raises(ValidationError):
        validate_event_payload({"event_type": "dispatch_cost", "rogue": True})


def test_validate_event_payload_rejects_unknown_event_type() -> None:
    from eawf.kernel.store.kinds.event import validate_event_payload

    with pytest.raises(ValidationError):
        validate_event_payload({"event_type": "totally_unknown"})
