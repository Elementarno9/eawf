"""Tests for :class:`eawf.daemon.idle.IdleTimeoutWatchdog`.

The watchdog runs as an asyncio task; the tests drive it with a tiny
``tick_seconds`` so the suite completes in well under a second.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from eawf.daemon.idle import IdleTimeoutWatchdog
from eawf.daemon.methods import MethodContext


def _build_ctx() -> MethodContext:
    return MethodContext(
        started_at="2026-05-19T00:00:00+00:00",
        pid=12345,
        protocol_version="1",
        version="0.2.0",
    )


def test_watchdog_fires_after_idle_window() -> None:
    """No activity + no subscribers + zero in-flight → shutdown set."""

    async def runner() -> None:
        ctx = _build_ctx()
        # Push last_activity comfortably into the past so the very
        # first tick trips the gate.
        ctx.last_activity = time.monotonic() - 1.0
        event = asyncio.Event()
        watchdog = IdleTimeoutWatchdog(
            idle_timeout_seconds=0.05,
            last_activity=lambda: ctx.last_activity,
            has_subscribers=lambda: False,
            in_flight=lambda: 0,
            tick_seconds=0.02,
        )
        task = asyncio.create_task(watchdog.run(event))
        await asyncio.wait_for(event.wait(), timeout=1.0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert event.is_set()

    asyncio.run(runner())


def test_watchdog_holds_when_subscribers_attached() -> None:
    """Live subscribers → watchdog never trips even past the idle window."""

    async def runner() -> None:
        ctx = _build_ctx()
        ctx.last_activity = time.monotonic() - 1.0
        event = asyncio.Event()
        watchdog = IdleTimeoutWatchdog(
            idle_timeout_seconds=0.02,
            last_activity=lambda: ctx.last_activity,
            has_subscribers=lambda: True,
            in_flight=lambda: 0,
            tick_seconds=0.01,
        )
        task = asyncio.create_task(watchdog.run(event))
        # Give the watchdog enough ticks to fire if the gate were
        # wrong; the event must stay clear.
        await asyncio.sleep(0.1)
        assert not event.is_set()
        event.set()  # explicit shutdown to unblock the loop
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(runner())


def test_watchdog_holds_when_in_flight_nonzero() -> None:
    """In-flight mutations block shutdown even with no subscribers."""

    async def runner() -> None:
        ctx = _build_ctx()
        ctx.last_activity = time.monotonic() - 1.0
        event = asyncio.Event()
        watchdog = IdleTimeoutWatchdog(
            idle_timeout_seconds=0.02,
            last_activity=lambda: ctx.last_activity,
            has_subscribers=lambda: False,
            in_flight=lambda: 1,
            tick_seconds=0.01,
        )
        task = asyncio.create_task(watchdog.run(event))
        await asyncio.sleep(0.1)
        assert not event.is_set()
        event.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(runner())


def test_touch_activity_resets_timer() -> None:
    """Repeated ``touch_activity`` keeps the watchdog from firing."""

    async def runner() -> None:
        ctx = _build_ctx()
        ctx.last_activity = time.monotonic() - 1.0
        event = asyncio.Event()
        watchdog = IdleTimeoutWatchdog(
            idle_timeout_seconds=0.05,
            last_activity=lambda: ctx.last_activity,
            has_subscribers=lambda: False,
            in_flight=lambda: 0,
            tick_seconds=0.01,
        )
        task = asyncio.create_task(watchdog.run(event))
        # Touch activity every few ms; the watchdog must keep
        # rescheduling itself.
        keep_alive_until = time.monotonic() + 0.15
        while time.monotonic() < keep_alive_until:
            ctx.touch_activity()
            await asyncio.sleep(0.01)
            if event.is_set():
                pytest.fail("watchdog fired despite continuous touch_activity")
        assert not event.is_set()
        event.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(runner())


def test_watchdog_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="idle_timeout_seconds"):
        IdleTimeoutWatchdog(
            idle_timeout_seconds=0.0,
            last_activity=lambda: 0.0,
            has_subscribers=lambda: False,
            in_flight=lambda: 0,
        )


def test_watchdog_rejects_non_positive_tick() -> None:
    with pytest.raises(ValueError, match="tick_seconds"):
        IdleTimeoutWatchdog(
            idle_timeout_seconds=1.0,
            last_activity=lambda: 0.0,
            has_subscribers=lambda: False,
            in_flight=lambda: 0,
            tick_seconds=0.0,
        )


def test_method_context_touch_activity_advances_monotonic() -> None:
    """``touch_activity`` writes a fresh ``time.monotonic()`` value."""
    ctx = _build_ctx()
    before = ctx.last_activity
    time.sleep(0.005)
    ctx.touch_activity()
    after = ctx.last_activity
    assert after > before
