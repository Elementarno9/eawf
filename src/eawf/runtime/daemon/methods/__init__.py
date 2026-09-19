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
import importlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Final

if TYPE_CHECKING:
    from pathlib import Path

    from eawf.runtime.daemon.epoch2_root import Epoch2RootContext

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


#: Stable refusal code for a daemon state write over a ``state.json`` that
#: is older than the one this daemon process last wrote at the same path.
STATE_REGRESSED: Final[str] = "state_regressed"


class StateRegressedError(DaemonValidationError):
    """Raised when a daemon state write would build on a restored older file.

    Something outside the daemon -- a ``git checkout``, a pre-commit stash
    restore -- put back a ``state.json`` whose ``updated_at`` predates the
    one this process last wrote at the path. Writing over that copy would
    silently drop every daemon write the copy predates, so the write is
    refused before anything (state, WAL record, event row) is persisted.

    The record of what this process wrote lives only in process memory, so
    a daemon restart is how an operator accepts a deliberate restore.

    Attributes:
        code: The stable refusal code carried in every message.
    """

    code: ClassVar[str] = STATE_REGRESSED


@dataclass
class MutationInFlight:
    """One in-flight mutation's telemetry row.

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
            ``state.mutate`` algorithm. ``None`` when the daemon
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
    #: Per-mutation in-flight telemetry: mutation_id ->
    #: :class:`MutationInFlight`. Populated at the in_flight increment,
    #: cleared at decrement; ``daemon.status`` projects it and the
    #: mutation watchdog sweeps it for over-ceiling holds.
    in_flight_details: dict[str, MutationInFlight] = field(default_factory=dict)
    #: The LockHandle of the mutation currently holding the state lock,
    #: or ``None``. The watchdog belt-and-braces heartbeat ticks it.
    active_lock_handle: Any = field(default=None)
    #: Native epoch-2 root contexts, keyed by root id. Kept apart from the
    #: epoch-1 anchors above, so attaching a native root never changes how
    #: an epoch-1 request resolves its state file, event log or WAL.
    native_roots: dict[str, Epoch2RootContext] = field(default_factory=dict)
    #: The ``updated_at`` of the ``state.json`` this process last wrote,
    #: keyed by resolved state path. Every daemon writer stamps the current
    #: time, so a file on disk carrying an older stamp than this entry was
    #: put there by something other than the daemon.
    state_written_at: dict[Path, datetime] = field(default_factory=dict)

    def native_root_context(self, tree_root: Path) -> Epoch2RootContext:
        """Return the native context of the epoch-2 tree at ``tree_root``.

        Args:
            tree_root: The tree's root directory, as the native fence
                resolved it for the request.

        Returns:
            The tree's one context, whose WAL namespace is a subdirectory
            of :attr:`wal_dir`.

        Raises:
            NativeAuthorityRequiredError: The tree resolves to epoch 1.
            MigrationDualAuthorityError: The tree's select is not whole.
            RuntimeError: :attr:`wal_dir` is unset.
        """
        # Imported here: every daemon method module imports this package,
        # and only a native request needs the epoch-2 migration stack.
        from eawf.runtime.daemon.epoch2_root import attach_root_context

        return attach_root_context(
            self.native_roots, tree_root=tree_root, daemon_wal_dir=self.wal_dir
        )

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

    def refuse_regressed_state(self, state_path: Path, *, updated_at: datetime) -> None:
        """Refuse a write over a state file older than this process's last write.

        Call this under the state lock, after the read and before anything
        is written, so a refusal leaves the state bytes, the WAL and the
        event log exactly as they were.

        An equal stamp passes: a CLI fallback write that kept the daemon's
        ``updated_at`` lost nothing, and a later one moved the file forward.

        Args:
            state_path: The ``state.json`` about to be written.
            updated_at: The ``updated_at`` of the file as it was just read.

        Raises:
            StateRegressedError: The on-disk file is older than the one this
                process last wrote at *state_path*.
        """
        written_at = self.state_written_at.get(state_path.resolve())
        if written_at is None or updated_at >= written_at:
            return
        logger.warning(
            f"refuse_regressed_state state_path={str(state_path)!r} "
            f"on_disk={updated_at.isoformat()} last_written={written_at.isoformat()}"
        )
        raise StateRegressedError(
            f"validation_failed: {STATE_REGRESSED}: state.json carries updated_at "
            f"{updated_at.isoformat()}, older than the {written_at.isoformat()} this "
            "daemon last wrote there; something outside the daemon (a git checkout, a "
            "stash restore) put an older copy back, and writing over it would drop "
            "every daemon write that copy predates. Put the newer state.json back, or "
            "run `eawf daemon restart` to accept a deliberate restore"
        )

    def note_state_written(self, state_path: Path, *, updated_at: datetime) -> None:
        """Record the ``updated_at`` this process just wrote to *state_path*.

        Call this immediately after the atomic state write, under the same
        lock hold, so the next read of that path is compared against what
        this process actually left on disk.

        The newest write replaces the entry outright rather than being
        maxed into it: a wall clock stepping backwards would otherwise make
        the file this process just wrote look regressed and refuse every
        subsequent write until the clock caught up.

        Args:
            state_path: The ``state.json`` that was written.
            updated_at: The ``updated_at`` stamped into the written payload.
        """
        self.state_written_at[state_path.resolve()] = updated_at


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


#: Every method module whose import registers handlers.
#:
#: Handlers register by import side effect, so importing every module up
#: front made daemon startup pay for the whole method surface before it
#: could answer a ping. Almost all of that cost is Pydantic building
#: validators for models a liveness probe never touches. ``server`` imports
#: only the liveness and subscribe modules, because it reaches for names in
#: them; every other verb registers when this function runs.
#:
#: Only top-level entry modules are listed. Several of them import helper
#: modules that register verbs of their own, which is why the companion test
#: asserts that every registered name in the package resolves rather than
#: that every file appears here -- a filename list would both miss those
#: helpers and break whenever one is split out.
#:
#: The list is explicit rather than discovered by scanning the package, so
#: adding an entry module is a visible edit and a stray file cannot quietly
#: become part of the surface.
_METHOD_MODULES: Final[tuple[str, ...]] = (
    "agent",
    "daemon",
    "candidate",
    "close",
    "close_hosted",
    "close_rereceipt",
    "config",
    "conformance",
    "delivery",
    "delivery_acceptance",
    "doctor",
    "domain",
    "domain_envelope",
    "event",
    "evidence",
    "fleet",
    "integration",
    "jury",
    "migration",
    "needs_user",
    "planning",
    "projection",
    "registry",
    "registry_workspace",
    "release",
    "release_candidate",
    "release_disposition",
    "release_receipts",
    "research",
    "run",
    "run_budget",
    "semantic",
    "spec",
    "spec_convert",
    "spec_repoint",
    "state",
    "state_subscribe",
    "wal_admin",
    "workspace_lease",
)

_lazy_modules_loaded = False


def ensure_all_methods_registered() -> None:
    """Import every deferred method module, at most once per process.

    Idempotent: the flag is set only after every module has imported, so a
    failed import is retried on the next call rather than leaving a
    half-registered surface behind a flag that says otherwise.
    """
    global _lazy_modules_loaded
    if _lazy_modules_loaded:
        return
    for suffix in _METHOD_MODULES:
        importlib.import_module(f"{__name__}.{suffix}")
    _lazy_modules_loaded = True
    logger.debug(f"ensure_all_methods_registered modules={len(_METHOD_MODULES)}")


async def dispatch(name: str, ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a JSON-RPC method call.

    A registry miss is not yet an unknown method: most handlers register on
    first need, so the deferred modules load and the lookup is retried once
    before the call is refused.

    Args:
        name: The method name from the request envelope.
        ctx: Server context shared across handlers.
        params: Decoded ``params`` object; ``{}`` when absent.

    Returns:
        The handler's result dict.

    Raises:
        MethodNotFoundError: When *name* is in no module's registry.
    """
    handler = _REGISTRY.get(name)
    if handler is None:
        ensure_all_methods_registered()
        try:
            handler = _REGISTRY[name]
        except KeyError as exc:
            raise MethodNotFoundError(name) from exc
    return await handler(ctx, params)


def registered_methods() -> tuple[str, ...]:
    """Return the registered method names in registration order.

    Loads the deferred modules first, so the answer is the whole surface
    rather than whichever part of it has been called so far.

    Returns:
        Tuple of method names.
    """
    ensure_all_methods_registered()
    return tuple(_REGISTRY)


def reset_registry() -> None:
    """Clear the registry. Test-only helper — do not call from production code."""
    global _lazy_modules_loaded
    _REGISTRY.clear()
    _lazy_modules_loaded = False
