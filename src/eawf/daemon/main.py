"""eawfd entry point.

Resolves :func:`eawf.daemon.runtime_dir.runtime_dir`, materialises the
PID file, builds the asyncio loop, and serves JSON-RPC until
``daemon.shutdown`` is received or the OS signals graceful stop.

POSIX listens on a Unix domain socket. Windows listens on the
per-user named pipe ``\\\\.\\pipe\\eawfd-<username>`` via
:class:`eawf.daemon.windows_pipe.WindowsPipeServer`, bridged into the
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
from eawf.daemon import PROTOCOL_VERSION
from eawf.daemon.bus import EventBus
from eawf.daemon.methods import MethodContext
from eawf.daemon.runtime_dir import log_path, pid_path, runtime_dir, socket_path
from eawf.daemon.server import process_frame_bytes, serve_unix
from eawf.state.enums import StoreKind
from eawf.store.paths import store_path

logger = logging.getLogger(__name__)


# Idle skeleton — placeholder so the daemon does not run forever during
# development. W08 owns the configurable shape + cold-spawn benchmark.
_IDLE_TIMEOUT_SECONDS = 300


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

    Args:
        foreground: When True, logs go to stderr; otherwise to
            ``<runtime_dir>/eawfd.log``.
    """
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    if foreground:
        logging.basicConfig(level=logging.INFO, format=fmt, stream=sys.stderr)
        return
    log_file = log_path()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter(fmt))
    root = logging.getLogger()
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
    try:
        # Idle skeleton: stop the daemon if no traffic + no shutdown for
        # five minutes. W08 replaces this with a config-driven watchdog.
        try:
            await asyncio.wait_for(ctx.shutdown_event.wait(), timeout=_IDLE_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.info(f"_run_server idle-timeout seconds={_IDLE_TIMEOUT_SECONDS}")
    finally:
        server.close()
        await server.wait_closed()


async def _run_windows_server(ctx: MethodContext) -> None:
    """Start the named-pipe listener and wait for shutdown (Windows).

    The :class:`eawf.daemon.windows_pipe.WindowsPipeServer` owns a
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
    from eawf.daemon.windows_pipe import WindowsPipeServer

    loop = asyncio.get_running_loop()

    async def _handler(payload: bytes) -> bytes:
        return await process_frame_bytes(payload, ctx)

    pipe_server = WindowsPipeServer(loop, _handler)
    pipe_server.start()
    logger.info(f"_run_windows_server bound pipe={pipe_server.pipe_path!r}")

    assert isinstance(ctx.shutdown_event, asyncio.Event)
    try:
        try:
            await asyncio.wait_for(
                ctx.shutdown_event.wait(),
                timeout=_IDLE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.info(f"_run_windows_server idle-timeout seconds={_IDLE_TIMEOUT_SECONDS}")
    finally:
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
    rt_dir = runtime_dir()
    rt_dir.mkdir(parents=True, exist_ok=True)
    pid_file = pid_path()

    started_at = datetime.now(UTC).isoformat()
    pid = os.getpid()
    _write_pid_file(pid_file, pid, started_at)

    # W06 wires the bus into every connection. W09's mutator hook
    # supplies the project-level ``event_path`` once registry
    # resolution lands; main.py keeps a runtime_dir-relative default
    # for the daemon's own bookkeeping.
    ctx = MethodContext(
        started_at=started_at,
        pid=pid,
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        event_path=store_path(rt_dir / "state.json", StoreKind.EVENT),
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
    return 0


def main() -> None:
    """CLI script entry: ``python -m eawf.daemon.main`` calls this."""
    sys.exit(run(foreground=True))


if __name__ == "__main__":
    main()
