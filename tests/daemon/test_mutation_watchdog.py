"""Per-mutation telemetry + the self-deadlock watchdog (P30-I23-W10).

A wedged close was invisible (``in_flight_mutations`` is a bare int) and
recovery was a manual pkill + rm ceremony (ZD-R6). Now every in-flight
mutation carries ``started_at`` + kind on the context, ``daemon.status``
projects the rows with a running duration, completion logs
``duration_ms``, and a watchdog loop alarms past the hold ceiling and
ABORTS the mutation task past the hard limit — the daemon keeps serving
without a restart.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from eawf import __version__
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.main import run_mutation_watchdog_loop
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.daemon import status as daemon_status

pytestmark = pytest.mark.unit


def _ctx() -> MethodContext:
    return MethodContext(
        started_at="2026-07-02T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        idempotency_cache={},
    )


def _run(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(body())


# ---- CR-01: telemetry rows + duration log -----------------------------------


def test_daemon_status_reports_in_flight_started_at_and_kind() -> None:
    """CR-01: daemon.status carries kind + started_at + duration per row."""

    async def body() -> None:
        ctx = _ctx()
        ctx.in_flight_mutations = 1
        ctx.mutation_started("m-1", "wave_close")
        result = await daemon_status(ctx, {})
        rows = result["in_flight"]
        assert len(rows) == 1
        assert rows[0]["mutation_id"] == "m-1"
        assert rows[0]["kind"] == "wave_close"
        assert rows[0]["started_at"]
        assert rows[0]["duration_s"] >= 0.0

    _run(body)


def test_mutation_finished_returns_duration_and_clears_row() -> None:
    """CR-01: the decrement clears the row and yields a duration_ms figure."""

    async def body() -> None:
        ctx = _ctx()
        ctx.mutation_started("m-1", "roadmap_revise")
        duration_ms = ctx.mutation_finished("m-1")
        assert duration_ms is not None and duration_ms >= 0.0
        assert ctx.in_flight_details == {}
        # A second clear is a no-op, never a crash.
        assert ctx.mutation_finished("m-1") is None

    _run(body)


# ---- CR-02: the watchdog aborts a wedged mutation ---------------------------


def test_watchdog_aborts_wedged_mutation_and_daemon_keeps_serving(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CR-02: past the hard limit the wedged task is cancelled, daemon lives.

    A mutation task that never completes is registered, the watchdog runs
    with sub-second ceilings, and the wedged task must be CANCELLED while
    the context keeps serving (a follow-up status call succeeds) — no
    restart, no manual ceremony.
    """

    async def body() -> None:
        ctx = _ctx()
        wedged_started = asyncio.Event()

        async def _wedged() -> None:
            ctx.mutation_started("m-wedged", "wave_close")
            wedged_started.set()
            await asyncio.sleep(3600)

        task = asyncio.create_task(_wedged())
        await wedged_started.wait()
        ctx.in_flight_details["m-wedged"].task = task

        stop = asyncio.Event()
        watchdog = asyncio.create_task(
            run_mutation_watchdog_loop(
                ctx,
                stop_event=stop,
                tick_seconds=0.05,
                alarm_seconds=0.0,
                hard_limit_seconds=0.1,
            )
        )
        await asyncio.sleep(0.4)
        stop.set()
        await watchdog

        assert task.cancelled() or task.done()
        assert "m-wedged" not in ctx.in_flight_details
        # The daemon keeps serving without a restart.
        result = await daemon_status(ctx, {})
        assert result["pid"] == ctx.pid

    with caplog.at_level(logging.ERROR, logger="eawf.runtime.daemon.main"):
        _run(body)
    assert any("mutation_watchdog abort" in r.getMessage() for r in caplog.records)


def test_watchdog_alarms_but_does_not_abort_under_hard_limit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Past the alarm ceiling but under the hard limit: WARNING only."""

    async def body() -> None:
        ctx = _ctx()
        ctx.mutation_started("m-slow", "wave_close")
        stop = asyncio.Event()
        watchdog = asyncio.create_task(
            run_mutation_watchdog_loop(
                ctx,
                stop_event=stop,
                tick_seconds=0.05,
                alarm_seconds=0.0,
                hard_limit_seconds=3600.0,
            )
        )
        await asyncio.sleep(0.2)
        stop.set()
        await watchdog
        # Alarmed, never aborted: the row survives.
        assert "m-slow" in ctx.in_flight_details

    with caplog.at_level(logging.WARNING, logger="eawf.runtime.daemon.main"):
        _run(body)
    assert any("mutation_watchdog alarm" in r.getMessage() for r in caplog.records)
    assert not any("mutation_watchdog abort" in r.getMessage() for r in caplog.records)


def test_watchdog_heartbeats_registered_lock_handle() -> None:
    """Belt-and-braces: the watchdog ticks the registered LockHandle."""

    class _Handle:
        def __init__(self) -> None:
            self.beats = 0

        def heartbeat(self) -> None:
            self.beats += 1

    async def body() -> None:
        ctx = _ctx()
        handle = _Handle()
        ctx.active_lock_handle = handle
        stop = asyncio.Event()
        watchdog = asyncio.create_task(
            run_mutation_watchdog_loop(ctx, stop_event=stop, tick_seconds=0.05)
        )
        await asyncio.sleep(0.2)
        stop.set()
        await watchdog
        assert handle.beats >= 1

    _run(body)
