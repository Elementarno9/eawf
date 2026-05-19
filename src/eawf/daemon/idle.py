"""Idle-timeout watchdog for the daemon.

Per C02 §5.5 the daemon self-shuts-down after a configurable idle
window when (a) no subscribers are attached, (b) no mutations are in
flight, and (c) no non-subscribe RPC has been dispatched for
``idle_timeout_seconds``. The watchdog runs as an asyncio task and
checks every 30 s (a tighter cadence wastes wakeups; a looser cadence
delays shutdown).

The default idle timeout of 300 s is aligned with the Anthropic
prompt-cache TTL (C02 §4 D11) — a CLI that consults the daemon every
five minutes keeps it warm, while truly idle sessions release the
runtime resources.

The watchdog does NOT block on the activity gate — it polls. Per-call
activity refresh is owned by the dispatcher
(:meth:`MethodContext.touch_activity`); the watchdog only reads.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


#: How often the watchdog re-evaluates the idle gate (seconds).
WATCHDOG_TICK_SECONDS: float = 30.0


class IdleTimeoutWatchdog:
    """Asyncio watchdog that signals shutdown after a configurable idle window.

    Attributes:
        idle_timeout_seconds: Idle window in seconds. A
            ``time.monotonic()`` delta greater than this — combined
            with the no-subscriber + no-in-flight gates — triggers
            shutdown.
        last_activity: Callable returning ``time.monotonic()`` of the
            most recent non-subscribe RPC dispatch. Accepts a callable
            so the watchdog stays decoupled from ``MethodContext``.
        has_subscribers: Callable returning ``True`` when at least one
            event subscriber is attached. Subscribers count as live
            activity even when no RPCs are flowing.
        in_flight: Callable returning the current count of mutations
            being applied. Non-zero means do not shutdown.
        tick_seconds: Override for :data:`WATCHDOG_TICK_SECONDS`.
            Tests use a much smaller value so suites complete quickly.
    """

    def __init__(
        self,
        *,
        idle_timeout_seconds: float,
        last_activity: Callable[[], float],
        has_subscribers: Callable[[], bool],
        in_flight: Callable[[], int],
        tick_seconds: float = WATCHDOG_TICK_SECONDS,
    ) -> None:
        if idle_timeout_seconds <= 0:
            raise ValueError(f"idle_timeout_seconds must be positive: {idle_timeout_seconds!r}")
        if tick_seconds <= 0:
            raise ValueError(f"tick_seconds must be positive: {tick_seconds!r}")
        self.idle_timeout_seconds = float(idle_timeout_seconds)
        self.last_activity = last_activity
        self.has_subscribers = has_subscribers
        self.in_flight = in_flight
        self.tick_seconds = float(tick_seconds)

    async def run(self, shutdown_event: asyncio.Event) -> None:
        """Poll the idle gate until *shutdown_event* fires or the gate trips.

        Args:
            shutdown_event: Event the watchdog sets when the daemon
                has been idle for ``idle_timeout_seconds`` with no
                subscribers and no in-flight mutations. The caller
                (``_run_server``) waits on the same event and tears
                down the listener when it fires.

        The loop exits cleanly when *shutdown_event* is already set
        before the next tick — typically because ``daemon.shutdown``
        was issued or the OS signalled a graceful stop.
        """
        logger.info(f"run start idle_timeout={self.idle_timeout_seconds} tick={self.tick_seconds}")
        while not shutdown_event.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(shutdown_event.wait(), timeout=self.tick_seconds)
            if shutdown_event.is_set():
                return
            idle_for = time.monotonic() - self.last_activity()
            if idle_for <= self.idle_timeout_seconds:
                continue
            if self.has_subscribers():
                continue
            if self.in_flight() > 0:
                continue
            logger.info(f"run idle-timeout-trip idle_for={idle_for:.1f}")
            shutdown_event.set()
            return


__all__ = [
    "WATCHDOG_TICK_SECONDS",
    "IdleTimeoutWatchdog",
]
