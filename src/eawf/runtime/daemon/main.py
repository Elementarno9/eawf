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
import os
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

from eawf import __version__
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.resolve import resolve_with_reason
from eawf.kernel.store.envelope import Envelope
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

logger = logging.getLogger(__name__)


#: Default idle window before the watchdog signals shutdown. Aligned
#: with the Anthropic prompt-cache TTL so a CLI that consults the
#: daemon every five minutes keeps it warm.
DEFAULT_IDLE_TIMEOUT_SECONDS: float = 300.0


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
        return ctx.in_flight_mutations

    return IdleTimeoutWatchdog(
        idle_timeout_seconds=idle_timeout_seconds,
        last_activity=_last_activity,
        has_subscribers=_has_subscribers,
        in_flight=_in_flight,
    )


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
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter(fmt))
    handler.addFilter(SensitiveScrubber())
    root = logging.getLogger()  # noqa: EAWF003 (root-logger handler config, not library acquisition)
    root.setLevel(logging.INFO)
    root.addHandler(handler)


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
    from eawf.runtime.daemon.windows_pipe import WindowsPipeServer

    loop = asyncio.get_running_loop()

    async def _handler(payload: bytes) -> bytes:
        return await process_frame_bytes(payload, ctx)

    pipe_server = WindowsPipeServer(loop, _handler)
    pipe_server.start()
    logger.info(f"_run_windows_server bound pipe={pipe_server.pipe_path!r}")

    assert isinstance(ctx.shutdown_event, asyncio.Event)
    idle_timeout = _resolve_idle_timeout()
    watchdog = _build_watchdog(ctx, idle_timeout)
    watchdog_task = asyncio.create_task(watchdog.run(ctx.shutdown_event))
    ttl_task = _schedule_session_ttl_sweep(ctx)
    stale_wave_task = _schedule_stale_wave_sweep(ctx)
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
