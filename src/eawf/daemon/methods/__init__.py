"""JSON-RPC method registry for the daemon.

Methods are registered by name via :func:`register`; the listener calls
:func:`dispatch` to invoke them with already-decoded JSON params. Each
handler receives a :class:`MethodContext` (server state) plus the params
dict, and returns a JSON-serialisable result dict.

W01 wires only the ``daemon.*`` namespace. Subsequent waves attach
``state.*``, ``event.*``, ``agent.*``, etc. by importing this module.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class MethodNotFoundError(KeyError):
    """Raised by :func:`dispatch` when a method name is unknown.

    The listener translates this into JSON-RPC error code ``-32601``.
    """


@dataclass
class MethodContext:
    """State passed to every method handler.

    Attributes:
        started_at: ISO-8601 timestamp of daemon boot, used by ping/status.
        pid: PID of the running daemon, used by ping/status.
        protocol_version: Wire-protocol version string.
        version: Library / package version string.
        active_subscriptions: Number of live ``event.subscribe`` connections.
            W06 wires the real counter via :class:`eawf.daemon.bus.EventBus`.
        in_flight_mutations: Number of mutations currently being applied.
            W09 wires the real counter; W01 keeps the field for shape.
        last_event_id: Most recent ``event.jsonl`` envelope id.
            W09's mutator hook updates this after each publish; the bus
            itself stays oblivious to last-id bookkeeping.
        shutdown_event: asyncio event raised when ``daemon.shutdown`` is
            received; populated by the server before any method runs.
        bus: Subscription bus shared by every connection on this
            daemon process. Method handlers reach for it via
            ``ctx.bus.publish(envelope)``; W09 wires the real mutator
            path. ``None`` only on contexts built before W06 (legacy
            tests).
        event_path: Filesystem path to ``event.jsonl`` used by
            ``event.subscribe`` / ``event.list`` / ``event.show`` for
            catch-up + bounded reads. ``None`` when the daemon runs in
            a context without an on-disk store (unit tests only).
        state_path: Filesystem path to ``state.json`` used by the
            ``agent.*`` methods to inspect wave / session rows.
            ``None`` when the daemon runs without an on-disk state
            (unit tests; daemonless paths).
        last_activity: ``time.monotonic()`` value of the most recent
            non-subscribe RPC dispatch. Refreshed by the server in
            :func:`eawf.daemon.server._process_frame` and consumed by
            :class:`eawf.daemon.idle.IdleTimeoutWatchdog` to gate the
            idle-timeout shutdown.
    """

    started_at: str
    pid: int
    protocol_version: str
    version: str
    active_subscriptions: int = 0
    in_flight_mutations: int = 0
    last_event_id: str = ""
    shutdown_event: Any = field(default=None)
    bus: Any = field(default=None)
    event_path: Any = field(default=None)
    state_path: Any = field(default=None)
    last_activity: float = field(default_factory=time.monotonic)

    def touch_activity(self) -> None:
        """Refresh :attr:`last_activity` to the current monotonic time.

        Called by the server dispatcher before invoking every method
        EXCEPT ``event.subscribe`` (per C02 §5.5). Subscribers
        independently keep the daemon alive via the live-subscriber
        gate inside :class:`eawf.daemon.idle.IdleTimeoutWatchdog`.
        """
        self.last_activity = time.monotonic()


Handler = Callable[[MethodContext, dict[str, Any]], Awaitable[dict[str, Any]]]


_REGISTRY: dict[str, Handler] = {}


def register(name: str) -> Callable[[Handler], Handler]:
    """Decorator that registers *func* under JSON-RPC method *name*.

    Args:
        name: Dotted JSON-RPC method name (e.g. ``daemon.ping``).

    Returns:
        The decorator that registers and returns the handler unchanged.

    Raises:
        ValueError: When *name* is already registered. Re-registration
            usually signals a duplicate import path; fail fast.
    """

    def decorator(func: Handler) -> Handler:
        if name in _REGISTRY:
            raise ValueError(f"method already registered: {name!r}")
        _REGISTRY[name] = func
        logger.debug(f"register name={name}")
        return func

    return decorator


async def dispatch(name: str, ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a JSON-RPC method call.

    Args:
        name: The method name from the request envelope.
        ctx: Server context shared across handlers.
        params: Decoded ``params`` object; ``{}`` when absent.

    Returns:
        The handler's result dict.

    Raises:
        MethodNotFoundError: When *name* is not in the registry.
    """
    try:
        handler = _REGISTRY[name]
    except KeyError as exc:
        raise MethodNotFoundError(name) from exc
    return await handler(ctx, params)


def registered_methods() -> tuple[str, ...]:
    """Return the registered method names in registration order.

    Returns:
        Tuple of method names. Useful for ``daemon.status`` and tests.
    """
    return tuple(_REGISTRY)


def reset_registry() -> None:
    """Clear the registry. Test-only helper — do not call from production code."""
    _REGISTRY.clear()
