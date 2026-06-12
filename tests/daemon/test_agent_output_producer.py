"""Tests: the FA4 live-output producer (P30-I17-W08).

The agent-watch tail CONSUMER existed but had no producer: ``live_output_buffer``
was undefined and ``append_output`` had zero production callers, so the tail read
"waiting for output" forever. These assertions confirm the W08 producer in the
dispatch runner:

- C1: :func:`capture_output_lines` splits a spawn's captured text into the
  bounded TAIL of non-empty lines (the ring cap keeps the freshest output).
- C2: :func:`emit_agent_output` publishes one ``agent.output`` event carrying
  the wave id + the bounded line tail through the canonical event writer + the
  bus, and is a no-op on empty output / a stateless context.
- C3: :func:`run_dispatch` fans the captured ``output_text`` through the producer
  so the dispatch path drives the tail end to end.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.dispatch_runner import (
    AGENT_OUTPUT_EVENT_TYPE,
    AGENT_OUTPUT_LINE_CAP,
    DispatchTokens,
    capture_output_lines,
    emit_agent_output,
    run_dispatch,
)
from eawf.runtime.daemon.methods import MethodContext

pytestmark = pytest.mark.integration

_WAVE_ID = "P30-I17-W08"


def _ctx(tmp_path: Path | None, *, bus: EventBus | None = None) -> MethodContext:
    event_path = None
    if tmp_path is not None:
        store = tmp_path / "store"
        store.mkdir(parents=True, exist_ok=True)
        event_path = store / "event.jsonl"
    return MethodContext(
        started_at="2026-06-11T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.6.0",
        bus=bus,
        event_path=event_path,
        state_path=None,
    )


def _drain_output(sub: Any) -> list[tuple[str, str]]:
    """Drain the subscriber queue into ``(wave_id, line)`` rows from agent.output."""
    rows: list[tuple[str, str]] = []
    while sub.queue:
        env = sub.queue.popleft()
        payload = env.payload
        if payload.get("event_type") != AGENT_OUTPUT_EVENT_TYPE:
            continue
        extras = payload["extras"]
        for line in str(extras["lines"]).split("\n"):
            rows.append((str(extras["wave_id"]), line))
    return rows


# ---- C1: capture_output_lines splits + bounds the line tail ------------------


def test_capture_output_lines_splits_and_drops_blanks() -> None:
    """C1: non-empty lines are kept in order; blank lines are dropped."""
    text = "first line\n\n  \nsecond line\nthird line\n"
    assert capture_output_lines(text) == ["first line", "second line", "third line"]


def test_capture_output_lines_keeps_freshest_tail_under_cap() -> None:
    """C1: when over the cap only the LAST cap lines (the freshest tail) survive."""
    text = "\n".join(f"line {n}" for n in range(AGENT_OUTPUT_LINE_CAP + 50))
    kept = capture_output_lines(text)
    assert len(kept) == AGENT_OUTPUT_LINE_CAP
    # The freshest tail is kept (the last line is present, the first scrolled off).
    assert kept[-1] == f"line {AGENT_OUTPUT_LINE_CAP + 49}"
    assert kept[0] == f"line {50}"


def test_capture_output_lines_empty_is_empty() -> None:
    """C1: an all-blank / empty text yields no lines."""
    assert capture_output_lines("") == []
    assert capture_output_lines("   \n\n  ") == []


# ---- C2: emit_agent_output publishes the agent.output event ------------------


def test_emit_agent_output_publishes_bounded_line_tail(tmp_path: Path) -> None:
    """C2: emit_agent_output publishes one agent.output event with the line tail."""
    bus = EventBus()
    sub = bus.register(connection_id="watch-tail")
    ctx = _ctx(tmp_path, bus=bus)

    envelope_id = emit_agent_output(ctx, wave_id=_WAVE_ID, text="hello world\nsecond line")
    assert envelope_id is not None
    rows = _drain_output(sub)
    assert rows == [(_WAVE_ID, "hello world"), (_WAVE_ID, "second line")]
    # The event landed in the on-disk event log too (the canonical writer path).
    log = (tmp_path / "store" / "event.jsonl").read_text(encoding="utf-8")
    assert AGENT_OUTPUT_EVENT_TYPE in log


def test_emit_agent_output_empty_output_is_noop(tmp_path: Path) -> None:
    """C2: a spawn that produced no capturable output emits no event."""
    bus = EventBus()
    sub = bus.register(connection_id="watch-tail")
    ctx = _ctx(tmp_path, bus=bus)

    assert emit_agent_output(ctx, wave_id=_WAVE_ID, text="   \n\n") is None
    assert not sub.queue


def test_emit_agent_output_stateless_context_is_noop() -> None:
    """C2: a context with no event store fans nothing (no crash)."""
    ctx = _ctx(None, bus=EventBus())
    assert emit_agent_output(ctx, wave_id=_WAVE_ID, text="some output") is None


# ---- C3: run_dispatch fans the captured output through the producer ----------


def test_run_dispatch_fans_output_text_when_supplied(tmp_path: Path) -> None:
    """C3: run_dispatch fans output_text through the producer end to end.

    A run driven with ``output_text`` emits an ``agent.output`` event carrying
    the captured line tail alongside the dispatch_cost event, so the dispatch
    path drives the live tail.
    """
    bus = EventBus()
    sub = bus.register(connection_id="watch-tail")
    ctx = _ctx(tmp_path, bus=bus)

    run_dispatch(
        ctx,
        wave_id=_WAVE_ID,
        primary_runtime="claude",
        fallback_runtime="claude",
        model="claude-opus-4-8",
        pricing_version="2026-01-01",
        primary_error=None,
        tokens=DispatchTokens(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        cost_usd=Decimal("0.01"),
        output_text="agent says: working on it\nagent says: done",
    )
    rows = _drain_output(sub)
    assert rows == [
        (_WAVE_ID, "agent says: working on it"),
        (_WAVE_ID, "agent says: done"),
    ]


def test_run_dispatch_without_output_text_fans_nothing(tmp_path: Path) -> None:
    """C3: run_dispatch with no output_text emits no agent.output event (no idle fan)."""
    bus = EventBus()
    sub = bus.register(connection_id="watch-tail")
    ctx = _ctx(tmp_path, bus=bus)

    run_dispatch(
        ctx,
        wave_id=_WAVE_ID,
        primary_runtime="claude",
        fallback_runtime="claude",
        model="claude-opus-4-8",
        pricing_version="2026-01-01",
        primary_error=None,
        tokens=DispatchTokens(
            input_tokens=1,
            output_tokens=1,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        cost_usd=Decimal("0.01"),
    )
    assert _drain_output(sub) == []
