"""JSON-RPC method registry for the daemon.

Methods are registered by name via :func:`register`; the listener calls
:func:`dispatch` to invoke them with already-decoded JSON params. Each
handler receives a :class:`MethodContext` (server state) plus the params
dict, and returns a JSON-serialisable result dict.

W01 wires only the ``daemon.*`` namespace. Subsequent waves attach
``state.*``, ``event.*``, ``agent.*``, etc. by importing this module.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

logger = logging.getLogger(__name__)


#: JSON-RPC error code the server emits when a mutation is rejected by a
#: lifecycle guard or by post-mutation invariant validation. Single
#: source of truth shared by :class:`DaemonValidationError` raisers in
#: :mod:`eawf.runtime.daemon.methods.state` and the wire-mapping in
#: :func:`eawf.runtime.daemon.server._process_frame`; the CLI client maps it to
#: :class:`eawf.surfaces.cli.errors.ValidationError` (exit code 2).
VALIDATION_FAILED: Final[int] = -32002


class MethodNotFoundError(KeyError):
    """Raised by :func:`dispatch` when a method name is unknown.

    The listener translates this into JSON-RPC error code ``-32601``.
    """


class DaemonValidationError(ValueError):
    """Raised by a handler when a mutation fails validation.

    Distinguished from a bare :class:`ValueError` (which the server maps
    to ``-32602 invalid_params`` for malformed param shapes) so the
    server can emit :data:`VALIDATION_FAILED` (``-32002``) on the wire
    instead. The CLI client maps that code to
    :class:`eawf.surfaces.cli.errors.ValidationError`, matching the in-process
    fallback's exit code for the same rejection.

    Subclasses :class:`ValueError` so any callsite that already catches
    ``ValueError`` keeps working; the server's ordered ``except`` clauses
    catch this subclass first to pick the more specific wire code. The
    ``validation_failed: `` message prefix is preserved by every raiser.
    """


@dataclass
class MutationInFlight:
    """One in-flight mutation's telemetry row (P30-I23-W10).

    Attributes:
        kind: The mutation kind value (e.g. ``wave_close``).
        started_at_monotonic: ``time.monotonic()`` at registration; the
            watchdog measures hold duration against it.
        started_at: Wall-clock ISO stamp for ``daemon.status``.
        task: The asyncio task driving the mutation; the watchdog aborts
            it past the hard limit.
    """

    kind: str
    started_at_monotonic: float
    started_at: str
    task: Any = None


@dataclass
class MethodContext:
    """State passed to every method handler.

    Attributes:
        started_at: ISO-8601 timestamp of daemon boot, used by ping/status.
        pid: PID of the running daemon, used by ping/status.
        protocol_version: Wire-protocol version string.
        version: Library / package version string.
        active_subscriptions: Number of live ``event.subscribe`` connections.
            W06 wires the real counter via :class:`eawf.runtime.daemon.bus.EventBus`.
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
            ``agent.*`` + ``state.*`` methods to inspect wave / session
            rows + mutate state. ``None`` when the daemon runs without
            an on-disk state (unit tests; daemonless paths).
        wal_dir: Filesystem path to the daemon's outcome-WAL directory
            (typically ``<runtime_dir>/wal/``). Owned by
            :mod:`eawf.runtime.daemon.methods.state` for the
            ``state.mutate`` algorithm (W09). ``None`` when the daemon
            runs without an on-disk WAL (unit tests; daemonless paths).
        idempotency_cache: In-memory cache for ``state.mutate``
            idempotency replay (TTL 60 s). The cache is attached
            lazily by the mutator on first access; legacy contexts
            without the field get one added on demand. The durable
            replay guarantee lives in the WAL.
        last_activity: ``time.monotonic()`` value of the most recent
            non-subscribe RPC dispatch. Refreshed by the server in
            :func:`eawf.runtime.daemon.server._process_frame` and consumed by
            :class:`eawf.runtime.daemon.idle.IdleTimeoutWatchdog` to gate the
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
    wal_dir: Any = field(default=None)
    idempotency_cache: Any = field(default=None)
    last_activity: float = field(default_factory=time.monotonic)
    #: Per-mutation in-flight telemetry (P30-I23-W10): mutation_id ->
    #: :class:`MutationInFlight`. Populated at the in_flight increment,
    #: cleared at decrement; ``daemon.status`` projects it and the
    #: mutation watchdog sweeps it for over-ceiling holds.
    in_flight_details: dict[str, MutationInFlight] = field(default_factory=dict)
    #: The LockHandle of the mutation currently holding the state lock,
    #: or ``None``. The watchdog belt-and-braces heartbeat ticks it.
    active_lock_handle: Any = field(default=None)

    def mutation_started(self, mutation_id: str, kind: str) -> None:
        """Register an in-flight mutation for telemetry + the watchdog."""
        self.in_flight_details[mutation_id] = MutationInFlight(
            kind=kind,
            started_at_monotonic=time.monotonic(),
            started_at=datetime.now(UTC).isoformat(),
            task=asyncio.current_task(),
        )

    def mutation_finished(self, mutation_id: str) -> float | None:
        """Clear an in-flight mutation; return its duration in ms."""
        entry = self.in_flight_details.pop(mutation_id, None)
        if entry is None:
            return None
        return (time.monotonic() - entry.started_at_monotonic) * 1000.0

    def touch_activity(self) -> None:
        """Refresh :attr:`last_activity` to the current monotonic time.

        Called by the server dispatcher before invoking every method
        EXCEPT ``event.subscribe``. Subscribers independently keep the
        daemon alive via the live-subscriber gate inside
        :class:`eawf.runtime.daemon.idle.IdleTimeoutWatchdog`.
        """
        self.last_activity = time.monotonic()


def note_cross_root_serve(
    ctx: MethodContext,
    *,
    repo_root: str | None,
    command: str,
) -> None:
    """Log when a mutation targets a state root other than the boot root.

    Multi-root serve (supersedes the EP3 refusal from P30-I23-W11): a
    mutation carrying an explicit ``repo_root`` is honoured against THAT
    root — the mutator path resolves state / event / WAL routing from the
    request, the WAL record is stamped with the target ``state_path`` so
    crash replay routes per record, and the idempotency cache is
    namespaced per root. The EP3 incident (a daemon bound to a
    smoke-fixture state wrote the wrong repo's ``state.json``) was caused
    by the OMITTED-``repo_root`` boot-anchor fallback, not by explicit
    routing; the omitted shape still resolves to the bound root with the
    one-shot ``daemon_anchor_fallback`` warning.

    Cross-root serves are logged at INFO so the daemon log shows which
    repos the machine-global process wrote for.

    Args:
        ctx: Server context carrying the daemon-bound ``state_path``.
        repo_root: The caller's intended repo root, or ``None`` (the
            legacy omitted-param shape, which resolves to the bound root).
        command: Operator-facing command name for the log line.
    """
    if not repo_root or ctx.state_path is None:
        return
    from pathlib import Path

    intended = (Path(repo_root) / ".ea" / "state.json").resolve()
    bound = Path(ctx.state_path).resolve()
    if intended != bound:
        logger.info(f"cross_root_serve command={command!r} target={str(intended)!r}")


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
