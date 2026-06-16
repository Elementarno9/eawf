"""Tests: the W45 LIVE per-chunk output producer (agent.output.chunk).

The terminal ``agent.output`` event fans a completed spawn's whole answer at
the end; this wave adds a streaming, PERSISTED counterpart so the TUI Watch
tail fills live AND the per-chunk output survives the TUI closing. These
assertions cover:

- the typed :class:`~eawf.kernel.store.kinds.events.AgentOutputChunkPayload`
  round-trips through :data:`~eawf.kernel.store.kinds.events.C09EventPayloadUnion`,
  and an unknown discriminator over the chunk shape is rejected;
- :func:`~eawf.runtime.daemon.dispatch_runner.emit_agent_output_chunk` appends
  one ``agent.output.chunk`` envelope keyed on ``scope_id == wave_id``, publishes
  it to the bus, and is a no-op on empty output / a stateless context;
- ordered ``seq`` is reconstructible from the persisted rows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.store.kinds.event import validate_event_payload
from eawf.kernel.store.kinds.events import (
    AgentOutputChunkPayload,
    C09EventPayloadUnion,
)
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.dispatch_runner import (
    AGENT_OUTPUT_CHUNK_EVENT_TYPE,
    emit_agent_output_chunk,
)
from eawf.runtime.daemon.methods import MethodContext

pytestmark = pytest.mark.integration

_WAVE_ID = "P30-I20-W45"


def _ctx(tmp_path: Path | None, *, bus: EventBus | None = None) -> MethodContext:
    event_path = None
    if tmp_path is not None:
        store = tmp_path / "store"
        store.mkdir(parents=True, exist_ok=True)
        event_path = store / "event.jsonl"
    return MethodContext(
        started_at="2026-06-16T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.6.0",
        bus=bus,
        event_path=event_path,
        state_path=None,
    )


def _chunk_rows(sub: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    while sub.queue:
        env = sub.queue.popleft()
        if env.payload.get("event_type") == AGENT_OUTPUT_CHUNK_EVENT_TYPE:
            rows.append(env.payload)
    return rows


# ---- payload round-trips through the union ----------------------------------


def test_agent_output_chunk_payload_round_trips_through_union() -> None:
    """The chunk payload parses + serialises through the C09 discriminated union."""
    payload = AgentOutputChunkPayload(
        timestamp="2026-06-16T00:00:00Z",
        wave_id=_WAVE_ID,
        session_id="sess-1",
        seq=3,
        lines="first\nsecond",
    )
    dumped = payload.model_dump(mode="json")
    assert dumped["event_type"] == "agent.output.chunk"
    # validate_event_payload routes the dotted tag to the C09 union arm.
    back = validate_event_payload(dumped)
    assert isinstance(back, AgentOutputChunkPayload)
    assert back.wave_id == _WAVE_ID
    assert back.seq == 3
    assert back.lines == "first\nsecond"


def test_agent_output_chunk_in_union_membership() -> None:
    """AgentOutputChunkPayload is a member of the C09 union (typed-row family)."""
    from typing import get_args

    members = get_args(get_args(C09EventPayloadUnion)[0])
    assert AgentOutputChunkPayload in members


def test_agent_output_chunk_negative_seq_rejected() -> None:
    """A negative seq fails fast (seq is a monotonic 0-based index)."""
    with pytest.raises(ValidationError):
        AgentOutputChunkPayload(
            timestamp="2026-06-16T00:00:00Z",
            wave_id=_WAVE_ID,
            session_id=None,
            seq=-1,
            lines="x",
        )


def test_agent_output_chunk_unknown_discriminator_rejected() -> None:
    """A body worn under the chunk tag but the wrong shape fails fast.

    A payload that claims ``event_type='agent.output.chunk'`` but carries an
    unknown extra field is rejected by the strict (``extra='forbid'``) arm rather
    than silently accepted -- the discriminator-emit invariant.
    """
    bad = {
        "event_type": AGENT_OUTPUT_CHUNK_EVENT_TYPE,
        "timestamp": "2026-06-16T00:00:00Z",
        "wave_id": _WAVE_ID,
        "session_id": None,
        "seq": 0,
        "lines": "x",
        "unexpected_field": "boom",
    }
    with pytest.raises(ValidationError):
        validate_event_payload(bad)


# ---- emit_agent_output_chunk publishes + persists ---------------------------


def test_emit_agent_output_chunk_persists_and_publishes(tmp_path: Path) -> None:
    """The emit helper appends one chunk envelope keyed on the wave + publishes it."""
    bus = EventBus()
    sub = bus.register(connection_id="watch-tail")
    ctx = _ctx(tmp_path, bus=bus)

    envelope_id = emit_agent_output_chunk(
        ctx, wave_id=_WAVE_ID, session_id="sess-1", seq=0, text="hello world\nsecond line"
    )
    assert envelope_id is not None
    rows = _chunk_rows(sub)
    assert len(rows) == 1
    assert rows[0]["wave_id"] == _WAVE_ID
    assert rows[0]["seq"] == 0
    assert rows[0]["lines"] == "hello world\nsecond line"
    # The envelope is keyed on the wave's scope_id so the Watch filter routes it.
    log = (tmp_path / "store" / "event.jsonl").read_text(encoding="utf-8")
    assert AGENT_OUTPUT_CHUNK_EVENT_TYPE in log
    assert f'"scope_id":"{_WAVE_ID}"' in log.replace(" ", "")


def test_emit_agent_output_chunk_seq_is_ordered(tmp_path: Path) -> None:
    """Successive emits carry the caller's monotonic seq, in order."""
    bus = EventBus()
    sub = bus.register(connection_id="watch-tail")
    ctx = _ctx(tmp_path, bus=bus)

    emit_agent_output_chunk(ctx, wave_id=_WAVE_ID, session_id="s", seq=0, text="a")
    emit_agent_output_chunk(ctx, wave_id=_WAVE_ID, session_id="s", seq=1, text="b")
    emit_agent_output_chunk(ctx, wave_id=_WAVE_ID, session_id="s", seq=2, text="c")

    rows = _chunk_rows(sub)
    assert [row["seq"] for row in rows] == [0, 1, 2]
    assert [row["lines"] for row in rows] == ["a", "b", "c"]


def test_emit_agent_output_chunk_empty_is_noop(tmp_path: Path) -> None:
    """A chunk with no capturable text emits no event (no empty row)."""
    bus = EventBus()
    sub = bus.register(connection_id="watch-tail")
    ctx = _ctx(tmp_path, bus=bus)

    assert (
        emit_agent_output_chunk(ctx, wave_id=_WAVE_ID, session_id="s", seq=0, text="   \n\n")
        is None
    )
    assert not sub.queue


def test_emit_agent_output_chunk_stateless_context_is_noop() -> None:
    """A context with no event store fans nothing (no crash)."""
    ctx = _ctx(None, bus=EventBus())
    assert (
        emit_agent_output_chunk(ctx, wave_id=_WAVE_ID, session_id="s", seq=0, text="some output")
        is None
    )
