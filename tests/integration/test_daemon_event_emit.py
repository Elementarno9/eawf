"""Integration: dispatch runner emits C09 events via the daemon writer.

Exercises :mod:`eawf.daemon.dispatch_runner` end-to-end against a real
``event.jsonl`` on a tmp filesystem. The assertions prove three things
the C09 §5.11 success criteria require:

1. A simulated V5 fallback emits a ``runtime_switched`` event and the
   post-dispatch step emits a ``dispatch_cost`` event, both readable from
   ``event.jsonl``.
2. Events route through the **daemon canonical writer**
   (:func:`eawf.store.append.append_envelope` + the subscription bus),
   never a direct ``event.jsonl`` open or ``atomic_write_json`` — verified
   by reading the rows the writer produced and asserting the bus saw the
   same envelopes + ``ctx.last_event_id`` advanced.
3. Every emitted envelope passes discriminated-union validation
   (:data:`eawf.store.kinds.events.C09EventPayloadUnion`) — and a payload
   whose body contradicts its ``event_type`` tag fails fast at validation.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from eawf.daemon.bus import EventBus
from eawf.daemon.dispatch_runner import (
    DispatchTokens,
    emit_dispatch_cost,
    run_dispatch,
)
from eawf.daemon.methods import MethodContext
from eawf.state.enums import StoreKind
from eawf.store.envelope import Envelope
from eawf.store.kinds.events import (
    C09EventPayloadUnion,
    DispatchCostPayload,
    RuntimeSwitchedPayload,
)

_UNION_ADAPTER: TypeAdapter[object] = TypeAdapter(C09EventPayloadUnion)


def _ctx(tmp_path: Path) -> tuple[MethodContext, Path, EventBus]:
    """Build a daemon context wired to a tmp ``event.jsonl`` + a live bus.

    Args:
        tmp_path: Pytest tmp directory.

    Returns:
        Tuple of ``(ctx, event_path, bus)``.
    """
    event_path = tmp_path / "store" / "event.jsonl"
    bus = EventBus()
    ctx = MethodContext(
        started_at="2026-05-22T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.3.0",
        bus=bus,
        event_path=event_path,
    )
    return ctx, event_path, bus


def _read_envelopes(event_path: Path) -> list[Envelope]:
    """Load every envelope row from *event_path* in append order."""
    rows: list[Envelope] = []
    with event_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(Envelope.model_validate_json(line))
    return rows


def _tokens() -> DispatchTokens:
    return DispatchTokens(
        input_tokens=1200,
        output_tokens=340,
        cache_creation_input_tokens=8000,
        cache_read_input_tokens=64000,
    )


@pytest.mark.integration
def test_run_dispatch_v5_fallback_emits_runtime_switched_and_cost(tmp_path: Path) -> None:
    """A simulated V5 fallback persists ``runtime_switched`` then ``dispatch_cost``."""
    ctx, event_path, _bus = _ctx(tmp_path)

    result = run_dispatch(
        ctx,
        wave_id="W10",
        primary_runtime="codex",
        fallback_runtime="claude",
        model="claude-opus-4-7",
        pricing_version="2026.05.17",
        primary_error="RUNTIME_RATE_LIMIT",
        tokens=_tokens(),
        cost_usd=Decimal("0.123456"),
    )

    assert result.switched is True
    assert result.runtime == "claude"

    envelopes = _read_envelopes(event_path)
    assert [e.payload["event_type"] for e in envelopes] == [
        "runtime_switched",
        "dispatch_cost",
    ]

    switched, cost = envelopes
    assert switched.kind == StoreKind.EVENT
    assert switched.payload["wave_id"] == "W10"
    assert switched.payload["runtime_from"] == "codex"
    assert switched.payload["runtime_to"] == "claude"
    assert switched.payload["cause"] == "RUNTIME_RATE_LIMIT"
    assert cost.payload["runtime"] == "claude"
    assert cost.payload["cost_usd"] == "0.123456"
    # The switch's to-attempt is the attempt the cost is billed against.
    assert switched.payload["attempt_id_to"] == cost.payload["attempt_id"]


@pytest.mark.integration
def test_run_dispatch_no_error_emits_only_dispatch_cost(tmp_path: Path) -> None:
    """No primary error → no switch event, only the post-dispatch cost row."""
    ctx, event_path, _bus = _ctx(tmp_path)

    result = run_dispatch(
        ctx,
        wave_id="W10",
        primary_runtime="claude",
        fallback_runtime="codex",
        model="claude-opus-4-7",
        pricing_version="2026.05.17",
        primary_error=None,
        tokens=_tokens(),
        cost_usd=Decimal("0.05"),
    )

    assert result.switched is False
    assert result.runtime == "claude"
    envelopes = _read_envelopes(event_path)
    assert [e.payload["event_type"] for e in envelopes] == ["dispatch_cost"]
    assert envelopes[0].payload["attempt_id"] == result.attempt_id


@pytest.mark.integration
def test_emitted_events_route_through_canonical_writer_and_bus(tmp_path: Path) -> None:
    """The bus sees the same envelopes the canonical writer appended (no direct write)."""
    ctx, event_path, bus = _ctx(tmp_path)
    sub = bus.register(connection_id="c-1", scope_filter=None, kind_filter=None)

    run_dispatch(
        ctx,
        wave_id="W10",
        primary_runtime="codex",
        fallback_runtime="claude",
        model="claude-opus-4-7",
        pricing_version="2026.05.17",
        primary_error="RUNTIME_SERVER_ERROR",
        tokens=_tokens(),
        cost_usd=Decimal("0.2"),
    )

    on_disk = _read_envelopes(event_path)
    published = list(sub.queue)
    # Canonical-writer proof: the rows persisted to event.jsonl and the
    # rows the bus published are the same envelopes (same ids, same order).
    assert [e.id for e in published] == [e.id for e in on_disk]
    assert ctx.last_event_id == on_disk[-1].id


@pytest.mark.integration
def test_emitted_payloads_pass_discriminated_union_validation(tmp_path: Path) -> None:
    """Every persisted payload validates through the C09 discriminated union."""
    ctx, event_path, _bus = _ctx(tmp_path)

    run_dispatch(
        ctx,
        wave_id="W10",
        primary_runtime="codex",
        fallback_runtime="claude",
        model="claude-opus-4-7",
        pricing_version="2026.05.17",
        primary_error="RUNTIME_TIMEOUT",
        tokens=_tokens(),
        cost_usd=Decimal("0.3"),
    )

    by_tag: dict[str, type] = {
        "runtime_switched": RuntimeSwitchedPayload,
        "dispatch_cost": DispatchCostPayload,
    }
    for env in _read_envelopes(event_path):
        validated = _UNION_ADAPTER.validate_python(env.payload)
        assert type(validated) is by_tag[env.payload["event_type"]]


@pytest.mark.integration
def test_discriminated_union_rejects_tag_body_mismatch() -> None:
    """A body that contradicts its ``event_type`` tag fails fast at append."""
    # A dispatch_cost-shaped body wearing the runtime_switched tag must be
    # rejected by the discriminator dispatch before it could ever persist.
    mismatched = {
        "event_type": "runtime_switched",
        "timestamp": "2026-05-22T00:00:00+00:00",
        "wave_id": "W10",
        "attempt_id": "a-1",
        "runtime": "claude",
        "model": "claude-opus-4-7",
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cost_usd": "0.1",
        "pricing_version": "2026.05.17",
    }
    with pytest.raises(ValidationError):
        _UNION_ADAPTER.validate_python(mismatched)


@pytest.mark.integration
def test_emit_dispatch_cost_requires_event_path(tmp_path: Path) -> None:
    """The runner refuses to emit when the canonical event path is unset."""
    bus = EventBus()
    ctx = MethodContext(
        started_at="2026-05-22T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.3.0",
        bus=bus,
        event_path=None,
    )
    with pytest.raises(RuntimeError, match="event_path not configured"):
        emit_dispatch_cost(
            ctx,
            wave_id="W10",
            attempt_id="a-1",
            runtime="claude",
            model="claude-opus-4-7",
            tokens=_tokens(),
            cost_usd=Decimal("0.1"),
            pricing_version="2026.05.17",
        )


@pytest.mark.integration
def test_emitted_dispatch_cost_round_trips_to_typed_payload(tmp_path: Path) -> None:
    """The persisted dispatch_cost row reloads into its typed payload losslessly."""
    ctx, event_path, _bus = _ctx(tmp_path)

    emit_dispatch_cost(
        ctx,
        wave_id=None,
        attempt_id=None,
        runtime="opencode",
        model="claude-opus-4-7",
        tokens=_tokens(),
        cost_usd=Decimal("0.5"),
        pricing_version="2026.05.17",
    )

    env = _read_envelopes(event_path)[0]
    payload = DispatchCostPayload.model_validate(env.payload)
    assert payload.wave_id is None
    assert payload.attempt_id is None
    assert payload.runtime == "opencode"
    assert payload.cost_usd == Decimal("0.5")
    assert json.loads(env.model_dump_json())["payload"]["event_type"] == "dispatch_cost"
