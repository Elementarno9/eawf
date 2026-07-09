"""eawfd entry point.

Resolves :func:`eawf.runtime.daemon.runtime_dir.runtime_dir`, materialises the
PID file, builds the asyncio loop, and serves JSON-RPC until
``daemon.shutdown`` is received or the OS signals graceful stop.

POSIX listens on a Unix domain socket. Windows listens on the
per-user named pipe ``\\\\.\\pipe\\eawfd-<username>`` via
:class:`eawf.runtime.daemon.windows_pipe.WindowsPipeServer`, bridged into the
shared JSON-RPC dispatcher.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import logging.handlers
import os
import signal
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from eawf import __version__
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.resolve import resolve_with_reason
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.store.paths import store_path
from eawf.observability.logging.scrub import SensitiveScrubber
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.idle import IdleTimeoutWatchdog
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.recovery import replay_wal
from eawf.runtime.daemon.runtime_dir import (
    ensure_runtime_dir,
    harden_runtime_dir,
    log_path,
    pid_path,
    socket_path,
)
from eawf.runtime.daemon.server import process_frame_bytes, serve_unix
from eawf.runtime.daemon.session_ttl import DEFAULT_TTL_SECONDS, run_sweep_loop
from eawf.runtime.daemon.singleton import DaemonAlreadyRunningError, acquire_daemon_singleton
from eawf.runtime.daemon.stale_wave import (
    DEFAULT_ABSOLUTE_BACKSTOP_SECONDS,
)
from eawf.runtime.daemon.stale_wave import (
    run_sweep_loop as run_stale_wave_loop,
)
from eawf.runtime.daemon.wal import (
    resolve_wal_gc_interval_seconds,
    resolve_wal_retention_seconds,
    run_wal_gc_loop,
)
from eawf.runtime.session.store import reconcile_orphaned_sessions

logger = logging.getLogger(__name__)


#: Default idle window before the watchdog signals shutdown. Aligned
#: with the Anthropic prompt-cache TTL so a CLI that consults the
#: daemon every five minutes keeps it warm.
DEFAULT_IDLE_TIMEOUT_SECONDS: float = 300.0

#: Default per-file size cap for the rotating ``eawfd.log`` handler. At
#: 16 MiB a long-lived daemon rolls a handful of backups instead of
#: growing the live log unbounded across weeks of uptime.
DEFAULT_DAEMON_LOG_MAX_BYTES: int = 16 * 1024 * 1024

#: Default number of rolled ``eawfd.log.N`` backups the rotating handler
#: retains; older backups are unlinked so total log footprint stays
#: bounded at roughly ``(backup_count + 1) * max_bytes``.
DEFAULT_DAEMON_LOG_BACKUP_COUNT: int = 5

#: Service stderr sink name that shares the daemon runtime directory.
DAEMON_ERR_LOG_NAME: str = "eawfd.err"


def _resolve_idle_timeout() -> float:
    """Return the configured idle timeout in seconds.

    The env var ``EAWF_DAEMON_IDLE_TIMEOUT`` lets the operator override
    the default for testing + tuning; the canonical config surface
    lands in a later layered-config wave. A non-positive override falls
    back to the default and logs a warning.
    """
    raw = os.environ.get("EAWF_DAEMON_IDLE_TIMEOUT")
    if not raw:
        return DEFAULT_IDLE_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(f"_resolve_idle_timeout unparseable raw={raw!r}; using default")
        return DEFAULT_IDLE_TIMEOUT_SECONDS
    if value <= 0:
        logger.warning(f"_resolve_idle_timeout non-positive raw={raw!r}; using default")
        return DEFAULT_IDLE_TIMEOUT_SECONDS
    return value


def _resolve_session_ttl_seconds() -> int:
    """Return the configured session-handle TTL in seconds.

    The env var ``EAWF_DAEMON_SESSION_TTL`` lets the operator override
    the default for testing + tuning; the canonical layered-config
    surface (``config.daemon.session_handle_ttl_seconds``) lands when
    the daemon main reads merged config. A non-positive override falls
    back to the default and logs a warning.
    """
    raw = os.environ.get("EAWF_DAEMON_SESSION_TTL")
    if not raw:
        return DEFAULT_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"_resolve_session_ttl_seconds unparseable raw={raw!r}; using default")
        return DEFAULT_TTL_SECONDS
    if value <= 0:
        logger.warning(f"_resolve_session_ttl_seconds non-positive raw={raw!r}; using default")
        return DEFAULT_TTL_SECONDS
    return value


def _resolve_stale_wave_seconds() -> int:
    """Return the over-budget absolute-backstop window in seconds.

    The size-relative 0.8x / 1.0x bands are derived per wave from its
    pessimistic budget; this resolver only tunes the generous absolute
    backstop that catches an abandoned wave with no projectable budget.
    The env var ``EAWF_DAEMON_STALE_WAVE_SECONDS`` exists for tests and
    operator tuning while the layered-config daemon reader is still
    landing. Invalid values fall back to the default backstop window.
    """
    raw = os.environ.get("EAWF_DAEMON_STALE_WAVE_SECONDS")
    if not raw:
        return DEFAULT_ABSOLUTE_BACKSTOP_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"_resolve_stale_wave_seconds unparseable raw={raw!r}; using default")
        return DEFAULT_ABSOLUTE_BACKSTOP_SECONDS
    if value <= 0:
        logger.warning(f"_resolve_stale_wave_seconds non-positive raw={raw!r}; using default")
        return DEFAULT_ABSOLUTE_BACKSTOP_SECONDS
    return value


def _resolve_log_max_bytes() -> int:
    """Return the per-file size cap for the rotating ``eawfd.log`` handler.

    The env var ``EAWF_DAEMON_LOG_MAX_BYTES`` lets the operator (and the
    rotation test) override the default while the layered-config daemon
    reader is still landing. A non-positive or unparseable override falls
    back to :data:`DEFAULT_DAEMON_LOG_MAX_BYTES` and logs a warning -- a
    zero cap would disable rotation and let the live log grow unbounded.

    Returns:
        Positive per-file byte cap.
    """
    raw = os.environ.get("EAWF_DAEMON_LOG_MAX_BYTES")
    if not raw:
        return DEFAULT_DAEMON_LOG_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"_resolve_log_max_bytes unparseable raw={raw!r}; using default")
        return DEFAULT_DAEMON_LOG_MAX_BYTES
    if value <= 0:
        logger.warning(f"_resolve_log_max_bytes non-positive raw={raw!r}; using default")
        return DEFAULT_DAEMON_LOG_MAX_BYTES
    return value


def _resolve_log_backup_count() -> int:
    """Return the number of rolled ``eawfd.log.N`` backups to retain.

    The env var ``EAWF_DAEMON_LOG_BACKUP_COUNT`` overrides the default for
    tests and operator tuning. A non-positive or unparseable override falls
    back to :data:`DEFAULT_DAEMON_LOG_BACKUP_COUNT` and logs a warning -- a
    zero count would leave only the live log and discard rolled history.

    Returns:
        Positive backup-file count.
    """
    raw = os.environ.get("EAWF_DAEMON_LOG_BACKUP_COUNT")
    if not raw:
        return DEFAULT_DAEMON_LOG_BACKUP_COUNT
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"_resolve_log_backup_count unparseable raw={raw!r}; using default")
        return DEFAULT_DAEMON_LOG_BACKUP_COUNT
    if value <= 0:
        logger.warning(f"_resolve_log_backup_count non-positive raw={raw!r}; using default")
        return DEFAULT_DAEMON_LOG_BACKUP_COUNT
    return value


def _err_log_path(log_file: Path) -> Path:
    """Return the stderr log sibling for *log_file*."""
    return log_file.with_name(DAEMON_ERR_LOG_NAME)


def _iter_numbered_log_backups(base_path: Path) -> list[tuple[int, Path]]:
    """Return numeric ``<base>.N`` backup files sorted oldest-index first."""
    prefix = f"{base_path.name}."
    backups: list[tuple[int, Path]] = []
    for candidate in base_path.parent.glob(f"{base_path.name}.*"):
        suffix = candidate.name.removeprefix(prefix)
        if not suffix.isdecimal():
            continue
        backups.append((int(suffix), candidate))
    return sorted(backups, key=lambda item: item[0])


def _sweep_rotating_log_backups(base_path: Path, *, max_bytes: int, backup_count: int) -> int:
    """Unlink stale or overlarge numbered backups for one rotating log file.

    Args:
        base_path: Live log file path whose numbered backups are scanned.
        max_bytes: Per-file cap used by the active rotating handler.
        backup_count: Highest numbered backup retained by the active handler.

    Returns:
        Count of backup files unlinked.
    """
    removed = 0
    for index, backup_path in _iter_numbered_log_backups(base_path):
        should_remove = index > backup_count
        if not should_remove:
            try:
                should_remove = backup_path.stat().st_size > max_bytes
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.warning(
                    f"_sweep_rotating_log_backups stat-failed path={backup_path.name!r} "
                    f"error={exc!s}"
                )
                continue
        if not should_remove:
            continue
        try:
            backup_path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning(
                f"_sweep_rotating_log_backups unlink-failed path={backup_path.name!r} error={exc!s}"
            )
            continue
        removed += 1
    return removed


def _sweep_daemon_log_backups(log_file: Path, *, max_bytes: int, backup_count: int) -> int:
    """Sweep orphaned rotating backups for daemon stdout and stderr logs."""
    return sum(
        _sweep_rotating_log_backups(path, max_bytes=max_bytes, backup_count=backup_count)
        for path in (log_file, _err_log_path(log_file))
    )


def _build_rotating_log_handler(
    path: Path, *, fmt: str, max_bytes: int, backup_count: int
) -> logging.handlers.RotatingFileHandler:
    """Build a scrubbed rotating file handler for one daemon log file."""
    handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(fmt))
    handler.addFilter(SensitiveScrubber())
    return handler


def _build_watchdog(ctx: MethodContext, idle_timeout_seconds: float) -> IdleTimeoutWatchdog:
    """Construct an :class:`IdleTimeoutWatchdog` wired to *ctx*.

    Args:
        ctx: Live :class:`MethodContext` whose ``last_activity`` field
            the dispatcher refreshes per non-subscribe RPC.
        idle_timeout_seconds: Idle window in seconds.

    Returns:
        Watchdog instance ready for ``await watchdog.run(event)``.
    """

    def _last_activity() -> float:
        return ctx.last_activity

    def _has_subscribers() -> bool:
        if ctx.bus is not None and hasattr(ctx.bus, "active_subscriptions"):
            return int(ctx.bus.active_subscriptions) > 0
        return ctx.active_subscriptions > 0

    def _in_flight() -> int:
        # Count live background work the RPC dispatcher never refreshes
        # last_activity for: a headless fleet drive (or research campaign) runs
        # on a daemon thread that neither bumps last_activity nor increments
        # in_flight_mutations, so without counting it here the watchdog
        # self-kills a subscriber-less headless drive mid-spawn at the idle
        # timeout. Function-local imports keep the fleet/research modules off
        # main's import path (they never import main; this stays cycle-free).
        from eawf.runtime.daemon.methods.fleet import drive_in_flight
        from eawf.runtime.daemon.methods.research import research_run_in_flight

        return (
            ctx.in_flight_mutations
            + (1 if drive_in_flight() else 0)
            + (1 if research_run_in_flight() else 0)
        )

    return IdleTimeoutWatchdog(
        idle_timeout_seconds=idle_timeout_seconds,
        last_activity=_last_activity,
        has_subscribers=_has_subscribers,
        in_flight=_in_flight,
    )


def _shutdown_background_drives() -> None:
    """Signal + join any background fleet drive on daemon shutdown.

    The fleet drive runs on a daemon thread that is not an asyncio task, so the
    serve loop's task-cancel teardown never reaps it. Cancel + join it here so a
    mid-drive shutdown stops claiming new waves and does not exit while a drain
    is mid-write. Research runs carry no cancel signal, so they are left for the
    process exit / a later reattach; the idle-watchdog interlock already keeps
    the daemon alive while either is in flight. Function-local import stays
    cycle-free (fleet never imports main).
    """
    from eawf.runtime.daemon.methods.fleet import shutdown_drive

    shutdown_drive()


#: Soft alarm ceiling for one in-flight mutation (seconds). Mirrors the
#: portalock hold ceiling: past it the watchdog logs a structured alarm.
_MUTATION_ALARM_SECONDS: float = 120.0

#: Hard abort limit (seconds). Past it the watchdog cancels the mutation
#: task so the daemon recovers WITHOUT the manual pkill + rm ceremony
#: (ZD-R6). Overridable via ``EAWF_MUTATION_HARD_LIMIT_SECONDS``.
#:
#: MUST exceed the verdict-tier bound: a verdict-always wave close spawns
#: a real fresh-context auditor whose wall clock is bounded by
#: ``VerifyBlock.juror_wall_clock_seconds`` (default 600s) INSIDE the
#: watched mutation, so a limit below that bound hard-aborts every
#: legitimate verdict close (the W41 close-loop incident). 900 = the
#: 600s juror bound + a 300s commit/routing margin.
_MUTATION_HARD_LIMIT_SECONDS: float = 900.0


def _resolve_mutation_hard_limit() -> float:
    """Return the watchdog hard-abort limit in seconds."""
    raw = os.environ.get("EAWF_MUTATION_HARD_LIMIT_SECONDS", "")
    try:
        value = float(raw)
    except ValueError:
        return _MUTATION_HARD_LIMIT_SECONDS
    return value if value > 0 else _MUTATION_HARD_LIMIT_SECONDS


async def run_mutation_watchdog_loop(
    ctx: MethodContext,
    *,
    stop_event: asyncio.Event,
    tick_seconds: float = 15.0,
    alarm_seconds: float = _MUTATION_ALARM_SECONDS,
    hard_limit_seconds: float | None = None,
) -> None:
    """Sweep in-flight mutations: alarm past the ceiling, abort past the limit.

    The self-deadlock watchdog (P30-I23-W10). Each tick it walks
    ``ctx.in_flight_details``:

    * past *alarm_seconds* it logs a structured WARNING naming the
      mutation kind + held duration (observability; no action);
    * past the hard limit it CANCELS the mutation's asyncio task —
      unwinding the task releases the portalock through the context
      manager — and emits a typed ``mutation_watchdog_abort`` incident
      event, retiring the manual pkill + rm recovery ceremony;
    * belt-and-braces with the W04 ticker, it heartbeats the registered
      ``ctx.active_lock_handle`` so a live hold never reads stale.

    Args:
        ctx: Live server context carrying the in-flight registry.
        stop_event: Daemon shutdown event; ends the loop.
        tick_seconds: Sweep cadence.
        alarm_seconds: Soft alarm ceiling per mutation.
        hard_limit_seconds: Hard abort limit; ``None`` resolves the env
            override / default.
    """
    limit = hard_limit_seconds if hard_limit_seconds is not None else _resolve_mutation_hard_limit()
    while not stop_event.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=tick_seconds)
        if stop_event.is_set():
            return
        handle = ctx.active_lock_handle
        if handle is not None and hasattr(handle, "heartbeat"):
            # Suppress broadly: a heartbeat on a stale/closed handle raises
            # ValueError (closed file), and ANY escape here kills the
            # watchdog task permanently — the W35 review blocker chain.
            with contextlib.suppress(Exception):
                handle.heartbeat()
        now = time.monotonic()
        for mutation_id, entry in list(ctx.in_flight_details.items()):
            held = now - entry.started_at_monotonic
            if held < alarm_seconds:
                continue
            if held < limit:
                logger.warning(
                    f"mutation_watchdog alarm mutation_id={mutation_id!r} "
                    f"kind={entry.kind} duration_s={held:.1f} limit_s={limit:.0f}"
                )
                continue
            logger.error(
                f"mutation_watchdog abort mutation_id={mutation_id!r} "
                f"kind={entry.kind} duration_s={held:.1f} limit_s={limit:.0f}"
            )
            task = entry.task
            if task is not None and not task.done():
                task.cancel()
            ctx.in_flight_details.pop(mutation_id, None)
            _publish_watchdog_abort(ctx, mutation_id=mutation_id, kind=entry.kind, held=held)


def _publish_watchdog_abort(
    ctx: MethodContext,
    *,
    mutation_id: str,
    kind: str,
    held: float,
) -> None:
    """Emit the typed ``mutation_watchdog_abort`` incident event."""
    now = datetime.now(UTC)
    summary = f"mutation_watchdog_abort kind={kind} duration_s={held:.1f}"
    payload = EventPayload(
        timestamp=now,
        event_type="mutation_watchdog_abort",
        actor="daemon",
        command="mutation_watchdog",
        args_hash="",
        before_state_version=None,
        after_state_version=None,
        status="error",
        message=summary,
        extras={"mutation_id": mutation_id, "kind": kind, "duration_s": round(held, 1)},
    ).model_dump(mode="json")
    envelope = Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=kind,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )
    if ctx.event_path is not None:
        with contextlib.suppress(OSError):
            append_envelope(Path(ctx.event_path), envelope)
    if ctx.bus is not None and hasattr(ctx.bus, "publish"):
        ctx.bus.publish(envelope)
    ctx.last_event_id = envelope.id


def _schedule_mutation_watchdog(ctx: MethodContext) -> asyncio.Task[None] | None:
    """Schedule the self-deadlock mutation watchdog on the running loop."""
    assert isinstance(ctx.shutdown_event, asyncio.Event)
    return asyncio.create_task(run_mutation_watchdog_loop(ctx, stop_event=ctx.shutdown_event))


def _schedule_session_ttl_sweep(ctx: MethodContext) -> asyncio.Task[None] | None:
    """Schedule the session-handle TTL sweep loop on the running loop.

    The sweep walks ``state.json`` once per interval, plans evictions
    for attempts whose ``ended_at + ttl < now``, and publishes a
    ``session_handle_pruned`` envelope per eviction. Returns ``None``
    when the context lacks a state path (unit-test daemonless paths)
    so the caller can elide the teardown step.

    Args:
        ctx: Live :class:`MethodContext` with a wired ``shutdown_event``
            and (optionally) ``bus`` + ``state_path``.

    Returns:
        The scheduled task, or ``None`` when no state path is available
        for sweeping.
    """
    if ctx.state_path is None:
        return None
    ttl_seconds = _resolve_session_ttl_seconds()
    publish = ctx.bus.publish if ctx.bus is not None else None
    assert isinstance(ctx.shutdown_event, asyncio.Event)
    return asyncio.create_task(
        run_sweep_loop(
            state_path=ctx.state_path,
            ttl_seconds=ttl_seconds,
            publish=publish,
            stop_event=ctx.shutdown_event,
        )
    )


def _schedule_stale_wave_sweep(ctx: MethodContext) -> asyncio.Task[None] | None:
    """Schedule the stale-wave detector loop on the running loop."""
    if ctx.state_path is None:
        return None
    absolute_backstop_seconds = _resolve_stale_wave_seconds()

    def _publish(envelope: Envelope) -> None:
        if ctx.bus is not None and hasattr(ctx.bus, "publish"):
            ctx.bus.publish(envelope)
        ctx.last_event_id = envelope.id

    assert isinstance(ctx.shutdown_event, asyncio.Event)
    return asyncio.create_task(
        run_stale_wave_loop(
            state_path=ctx.state_path,
            event_path=Path(ctx.event_path) if ctx.event_path is not None else None,
            absolute_backstop_seconds=absolute_backstop_seconds,
            publish=_publish,
            stop_event=ctx.shutdown_event,
        )
    )


def _schedule_wal_gc_sweep(ctx: MethodContext) -> asyncio.Task[None] | None:
    """Schedule the WAL garbage-collection sweep loop on the running loop.

    The sweep unlinks fsynced WAL records older than the retention window
    once per interval so the WAL does not grow unbounded. Returns ``None``
    when the context lacks a WAL directory (unit-test daemonless paths) so
    the caller can elide the teardown step.

    Args:
        ctx: Live :class:`MethodContext` with a wired ``shutdown_event``
            and (optionally) ``wal_dir``.

    Returns:
        The scheduled task, or ``None`` when no WAL directory is wired.
    """
    if ctx.wal_dir is None:
        return None
    assert isinstance(ctx.shutdown_event, asyncio.Event)
    return asyncio.create_task(
        run_wal_gc_loop(
            wal_dir=Path(ctx.wal_dir),
            retention_seconds=resolve_wal_retention_seconds(),
            interval_seconds=resolve_wal_gc_interval_seconds(),
            stop_event=ctx.shutdown_event,
        )
    )


def _write_pid_file(path: Path, pid: int, started_at: str) -> None:
    """Atomically write the daemon PID file.

    Args:
        path: Destination PID file path.
        pid: Process id to record.
        started_at: ISO-8601 boot timestamp.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(f"{pid}\n{PROTOCOL_VERSION}\n{started_at}\n", encoding="utf-8")
    tmp.replace(path)


def _configure_logging(foreground: bool) -> None:
    """Wire stderr-or-file logging for the daemon.

    Both branches attach a :class:`~eawf.observability.logging.scrub.SensitiveScrubber`
    so neither the foreground stderr stream nor the ``eawfd.log`` file
    ever serialises raw machine paths, IP addresses, or secret-shaped
    tokens (an unscrubbed ``error_detail`` / ``session_log_path`` would
    otherwise leak the operator's absolute paths into the log).

    The file branch uses :class:`logging.handlers.RotatingFileHandler`
    instances so ``eawfd.log`` and ``eawfd.err`` roll at
    ``EAWF_DAEMON_LOG_MAX_BYTES`` (default 16 MiB) and retain
    ``EAWF_DAEMON_LOG_BACKUP_COUNT`` backups (default 5) instead of
    growing unbounded across a long-lived daemon.

    Args:
        foreground: When True, logs go to stderr; otherwise to
            ``<runtime_dir>/eawfd.log``.
    """
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    if foreground:
        handler: logging.Handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(logging.Formatter(fmt))
        handler.addFilter(SensitiveScrubber())
        logging.basicConfig(level=logging.INFO, handlers=[handler])
        return
    log_file = log_path()
    # The log lives directly under the runtime dir, and this branch is the
    # earliest runtime-dir materialiser on a non-foreground boot. Create
    # the parent then retighten it to owner-only (0700) so the first log
    # write never lands in a group/other-traversable directory (the dir
    # holds the PID file, socket, log, and WAL — all embed operator paths).
    log_file.parent.mkdir(parents=True, exist_ok=True)
    harden_runtime_dir(log_file.parent)
    max_bytes = _resolve_log_max_bytes()
    backup_count = _resolve_log_backup_count()
    _sweep_daemon_log_backups(log_file, max_bytes=max_bytes, backup_count=backup_count)
    handler = _build_rotating_log_handler(
        log_file,
        fmt=fmt,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )
    err_handler = _build_rotating_log_handler(
        _err_log_path(log_file),
        fmt=fmt,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )
    err_handler.setLevel(logging.ERROR)
    root = logging.getLogger()  # noqa: EAWF003 (root-logger handler config, not library acquisition)
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(err_handler)


async def _run_server(sock_path: Path, ctx: MethodContext, expected_uid: int | None) -> None:
    """Start the JSON-RPC server and wait for shutdown (POSIX).

    Args:
        sock_path: UDS bind path.
        ctx: Server context (with wired ``shutdown_event``).
        expected_uid: Peer-cred match target, or ``None`` to skip.
    """
    server = await serve_unix(str(sock_path), ctx, expected_uid=expected_uid)
    loop = asyncio.get_running_loop()

    def _request_shutdown() -> None:
        logger.info("_request_shutdown signal-received")
        if isinstance(ctx.shutdown_event, asyncio.Event):
            ctx.shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_shutdown)

    assert isinstance(ctx.shutdown_event, asyncio.Event)
    idle_timeout = _resolve_idle_timeout()
    watchdog = _build_watchdog(ctx, idle_timeout)
    watchdog_task = asyncio.create_task(watchdog.run(ctx.shutdown_event))
    ttl_task = _schedule_session_ttl_sweep(ctx)
    stale_wave_task = _schedule_stale_wave_sweep(ctx)
    wal_gc_task = _schedule_wal_gc_sweep(ctx)
    mutation_watchdog_task = _schedule_mutation_watchdog(ctx)
    try:
        await ctx.shutdown_event.wait()
    finally:
        watchdog_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog_task
        if ttl_task is not None:
            ttl_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ttl_task
        if stale_wave_task is not None:
            stale_wave_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stale_wave_task
        if wal_gc_task is not None:
            wal_gc_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await wal_gc_task
        if mutation_watchdog_task is not None:
            mutation_watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await mutation_watchdog_task
        _shutdown_background_drives()
        server.close()
        await server.wait_closed()


async def _run_windows_server(ctx: MethodContext) -> None:
    """Start the named-pipe listener and wait for shutdown (Windows).

    The :class:`eawf.runtime.daemon.windows_pipe.WindowsPipeServer` owns a
    dedicated listener thread + an asyncio dispatch task. Shutdown
    flows from ``daemon.shutdown`` (sets ``ctx.shutdown_event``) or
    from the parent harness setting it directly; the pipe server's
    ``stop()`` then unblocks the listener thread.

    Args:
        ctx: Server context (with wired ``shutdown_event``).
    """
    # The Windows transport lives behind the import-guarded module so
    # the POSIX dev loop is not contaminated. The outer ``run()``
    # already gates on ``sys.platform`` before reaching here.
    from eawf.runtime.daemon.windows_pipe import (
        WindowsPipeServer,
        make_bus_subscribe_router,
    )

    loop = asyncio.get_running_loop()

    async def _handler(payload: bytes) -> bytes:
        return await process_frame_bytes(payload, ctx)

    # The subscribe router bridges a pipe subscribe frame to the live
    # event bus so the TUI receives ``event.push`` frames over the pipe;
    # the listener keeps serving other RPCs while a subscription streams.
    pipe_server = WindowsPipeServer(
        loop,
        _handler,
        subscribe_router=make_bus_subscribe_router(loop, ctx),
    )
    pipe_server.start()
    logger.info(f"_run_windows_server bound pipe={pipe_server.pipe_path!r}")

    assert isinstance(ctx.shutdown_event, asyncio.Event)
    idle_timeout = _resolve_idle_timeout()
    watchdog = _build_watchdog(ctx, idle_timeout)
    watchdog_task = asyncio.create_task(watchdog.run(ctx.shutdown_event))
    ttl_task = _schedule_session_ttl_sweep(ctx)
    stale_wave_task = _schedule_stale_wave_sweep(ctx)
    wal_gc_task = _schedule_wal_gc_sweep(ctx)
    mutation_watchdog_task = _schedule_mutation_watchdog(ctx)
    try:
        await ctx.shutdown_event.wait()
    finally:
        watchdog_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog_task
        if ttl_task is not None:
            ttl_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ttl_task
        if stale_wave_task is not None:
            stale_wave_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stale_wave_task
        if wal_gc_task is not None:
            wal_gc_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await wal_gc_task
        if mutation_watchdog_task is not None:
            mutation_watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await mutation_watchdog_task
        _shutdown_background_drives()
        pipe_server.stop()


def run(*, foreground: bool = True) -> int:
    """Run the daemon to completion.

    Args:
        foreground: When True, log to stderr and stay in the foreground
            (the supervisor / operator owns process lifecycle). When
            False, log to ``<runtime_dir>/eawfd.log``.

    Returns:
        Process exit code — ``0`` on clean shutdown.

    Raises:
        ImportError: When invoked on Windows without the optional
            ``windows`` extras installed (``pip install eawf[windows]``
            or equivalent). The named-pipe transport requires pywin32.
    """
    _configure_logging(foreground)
    # Create + retighten the runtime dir to owner-only (0700) on every boot:
    # it holds the PID file, socket, log, and WAL, all of which embed the
    # operator's cwd / state paths, so other local users must not traverse it.
    rt_dir = ensure_runtime_dir()
    if foreground:
        _sweep_daemon_log_backups(
            rt_dir / "eawfd.log",
            max_bytes=_resolve_log_max_bytes(),
            backup_count=_resolve_log_backup_count(),
        )
    try:
        with acquire_daemon_singleton(rt_dir):
            pid_file = pid_path()

            started_at = datetime.now(UTC).isoformat()
            pid = os.getpid()
            _write_pid_file(pid_file, pid, started_at)

            # Resolve the project's state path so the daemon's ``state.*``
            # mutator can locate ``state.json`` + ``event.jsonl``. Precedence
            # is the same as the CLI resolver — ``EA_STATE`` env var wins,
            # otherwise pwd-upward from the spawn cwd. Registry-driven repo
            # resolution lands in W10 alongside the layered-config writer.
            project_state_path, _state_reason = resolve_with_reason(workspace=None)
            project_event_path = store_path(project_state_path, StoreKind.EVENT)
            daemon_wal_dir = rt_dir / "wal"
            daemon_wal_dir.mkdir(parents=True, exist_ok=True)

            # Startup WAL replay: walk the WAL once before the listener starts
            # accepting connections so any post-apply outcome record from a
            # prior unclean exit (SIGKILL between event-append and fsync-rename)
            # gets reconciled against the event log. Idempotent on subsequent
            # boots — fully-replayed records rename to ``.fsynced.json`` and
            # the next pass is a no-op.
            replay_report = replay_wal(
                daemon_wal_dir,
                state_path=project_state_path,
                event_path=project_event_path,
            )
            logger.info(
                f"run wal-replay pending={replay_report.pending_count} "
                f"applied={replay_report.applied_count} "
                f"fsynced={replay_report.fsynced_count} "
                f"poisoned={replay_report.poisoned_count} "
                f"replayed={replay_report.replayed_event_count}"
            )
            if replay_report.poisoned_count > 0:
                logger.warning(
                    f"run wal-replay poisoned-present count={replay_report.poisoned_count}; "
                    f"operator should run 'eawf daemon replay-wal --inspect'"
                )

            # Reconcile orphaned agent sessions: a prior daemon's spawned
            # children died with it, but their AgentSession rows stay ACTIVE in
            # state.json and render as zombie live agents on the Watch surface.
            # A fresh daemon owns no live children, so every ACTIVE session at
            # boot is orphaned -- flip them all to STALE. Single-threaded here,
            # pre-listener, so there is no contention on the state lock.
            orphaned = reconcile_orphaned_sessions(project_state_path, project_event_path)
            if orphaned:
                logger.info(f"run reconciled-orphan-sessions flipped={orphaned}")

            ctx = MethodContext(
                started_at=started_at,
                pid=pid,
                protocol_version=PROTOCOL_VERSION,
                version=__version__,
                shutdown_event=asyncio.Event(),
                bus=EventBus(),
                event_path=project_event_path,
                state_path=project_state_path,
                wal_dir=daemon_wal_dir,
                idempotency_cache={},
            )

            logger.info(f"run boot pid={pid} version={__version__!r} protocol={PROTOCOL_VERSION!r}")
            sock_path = socket_path() if sys.platform != "win32" else None
            try:
                if sys.platform == "win32":
                    asyncio.run(_run_windows_server(ctx))
                else:
                    assert sock_path is not None
                    if sock_path.exists():
                        # Stale socket from a prior unclean exit — unlink before bind.
                        sock_path.unlink()
                    asyncio.run(_run_server(sock_path, ctx, expected_uid=os.geteuid()))
            finally:
                if sock_path is not None:
                    with contextlib.suppress(FileNotFoundError):
                        sock_path.unlink()
                with contextlib.suppress(FileNotFoundError):
                    pid_file.unlink()
                logger.info("run exit")
    except DaemonAlreadyRunningError:
        logger.info(f"run duplicate-daemon runtime={rt_dir.name!r}")
        return 0
    return 0


def main() -> None:
    """CLI script entry: ``python -m eawf.runtime.daemon.main`` calls this.

    Defaults to non-foreground so auto-spawned daemons write to the
    runtime log file (``<runtime_dir>/eawfd.log``). Pass
    ``--foreground`` to keep logs on stderr instead — used by
    ``eawf daemon run --foreground`` and the per-OS service templates
    (systemd/launchd both pipe stdout/stderr to the same log path).
    """
    foreground = "--foreground" in sys.argv[1:]
    sys.exit(run(foreground=foreground))


if __name__ == "__main__":
    main()
