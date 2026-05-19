"""eawfd entry point.

Resolves :func:`eawf.daemon.runtime_dir.runtime_dir`, materialises the
PID file + Unix socket, builds the asyncio loop, and serves JSON-RPC
until ``daemon.shutdown`` is received or SIGTERM lands.

Windows path is deferred to W02 (named-pipe listener via pywin32). When
called on Windows today the entry point raises
:class:`NotImplementedError` so callers fail closed rather than launch
a half-configured daemon.
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
from eawf.daemon.methods import MethodContext
from eawf.daemon.runtime_dir import log_path, pid_path, runtime_dir, socket_path
from eawf.daemon.server import serve_unix

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
    """Start the JSON-RPC server and wait for shutdown.

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


def run(*, foreground: bool = True) -> int:
    """Run the daemon to completion.

    Args:
        foreground: When True, log to stderr and stay in the foreground
            (the supervisor / operator owns process lifecycle). When
            False, log to ``<runtime_dir>/eawfd.log``.

    Returns:
        Process exit code — ``0`` on clean shutdown.

    Raises:
        NotImplementedError: When invoked on Windows. W02 wires the
            named-pipe listener; until then the entry point refuses to
            run rather than start a half-configured daemon.
    """
    if sys.platform.startswith("win"):
        # W02 wires the pywin32 named-pipe listener + DACL gating.
        raise NotImplementedError("daemon on windows lands in W02")

    _configure_logging(foreground)
    rt_dir = runtime_dir()
    rt_dir.mkdir(parents=True, exist_ok=True)
    sock_path = socket_path()
    pid_file = pid_path()

    if sock_path.exists():
        # Stale socket from a prior unclean exit — unlink before bind.
        sock_path.unlink()

    started_at = datetime.now(UTC).isoformat()
    pid = os.getpid()
    _write_pid_file(pid_file, pid, started_at)

    ctx = MethodContext(
        started_at=started_at,
        pid=pid,
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
    )

    logger.info(f"run boot pid={pid} version={__version__!r} protocol={PROTOCOL_VERSION!r}")
    try:
        asyncio.run(_run_server(sock_path, ctx, expected_uid=os.geteuid()))
    finally:
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
