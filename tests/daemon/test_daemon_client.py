"""Tests for :class:`eawf.cli._daemon_client.DaemonClient`.

The tests spin up a real :func:`eawf.daemon.server.serve_unix` on a
per-test temp UDS path, then drive the synchronous client against it
from a background thread (the asyncio loop owns the server; the
client uses blocking sockets). This exercises the full wire format
end-to-end without requiring the cold-spawn fork+exec path.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from eawf import __version__
from eawf.cli._daemon_client import DaemonClient, DaemonRpcError
from eawf.daemon import PROTOCOL_VERSION
from eawf.daemon.bus import EventBus
from eawf.daemon.methods import MethodContext
from eawf.daemon.server import serve_unix

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX-only client transport; W09 wires the windows-pipe client",
)


def _short_runtime_dir() -> Path:
    """Return a per-test runtime dir short enough for AF_UNIX (104-byte cap)."""
    base = Path(tempfile.gettempdir())
    return base / f"eawfd-{uuid.uuid4().hex[:8]}"


class _ServerHandle:
    """Async server harness — boots a loop in a worker thread."""

    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir
        self.sock_path = runtime_dir / "eawfd.sock"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.Server | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._pid_file = runtime_dir / "eawfd.pid"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(timeout=5.0), "server failed to start within 5 s"
        # Write a pid file so :func:`auto_spawn_daemon` treats the
        # already-running daemon as healthy.
        self._pid_file.write_text(
            f"{os.getpid()}\n{PROTOCOL_VERSION}\n2026-05-19T00:00:00+00:00\n",
            encoding="utf-8",
        )

    def stop(self) -> None:
        self._stop.set()
        if self._loop is not None and self._server is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        with contextlib.suppress(FileNotFoundError):
            self.sock_path.unlink()
        with contextlib.suppress(FileNotFoundError):
            self._pid_file.unlink()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        ctx = MethodContext(
            started_at="2026-05-19T00:00:00+00:00",
            pid=os.getpid(),
            protocol_version=PROTOCOL_VERSION,
            version=__version__,
            shutdown_event=asyncio.Event(),
            bus=EventBus(),
        )

        async def _start() -> None:
            self._server = await serve_unix(str(self.sock_path), ctx, expected_uid=None)
            self._ready.set()

        loop.run_until_complete(_start())
        try:
            loop.run_forever()
        finally:
            if self._server is not None:
                self._server.close()
                loop.run_until_complete(self._server.wait_closed())
            loop.close()


@pytest.fixture
def server() -> Iterator[_ServerHandle]:
    handle = _ServerHandle(_short_runtime_dir())
    handle.start()
    try:
        yield handle
    finally:
        handle.stop()


def test_client_round_trips_daemon_ping(server: _ServerHandle) -> None:
    """``DaemonClient.call('daemon.ping')`` returns version + PID."""
    with DaemonClient(runtime_dir=server.runtime_dir) as client:
        result = client.call("daemon.ping")
    assert result["pid"] == os.getpid()
    assert result["version"] == __version__
    assert result["protocol_version"] == PROTOCOL_VERSION
    assert isinstance(result["started_at"], str)
    assert isinstance(result["uptime_seconds"], int | float)


def test_client_round_trips_daemon_status(server: _ServerHandle) -> None:
    """``daemon.status`` returns the warm counters."""
    with DaemonClient(runtime_dir=server.runtime_dir) as client:
        result = client.call("daemon.status")
    assert result["active_subscriptions"] == 0
    assert result["in_flight_mutations"] == 0
    assert result["last_event_id"] == ""


def test_client_raises_on_method_not_found(server: _ServerHandle) -> None:
    """JSON-RPC error envelope → :class:`DaemonRpcError`."""
    with (
        DaemonClient(runtime_dir=server.runtime_dir) as client,
        pytest.raises(DaemonRpcError) as excinfo,
    ):
        client.call("daemon.nope")
    assert excinfo.value.code == -32601
    assert "method not found" in excinfo.value.message


def test_client_raises_on_invalid_params(server: _ServerHandle) -> None:
    """Unknown field on the params object → ``-32602 invalid_params``."""
    with (
        DaemonClient(runtime_dir=server.runtime_dir) as client,
        pytest.raises(DaemonRpcError) as excinfo,
    ):
        client.call("daemon.ping", {"unexpected": "field"})
    assert excinfo.value.code == -32602


def test_client_multiple_calls_per_connection(server: _ServerHandle) -> None:
    """One client → many sequential ``call`` invocations."""
    with DaemonClient(runtime_dir=server.runtime_dir) as client:
        first = client.call("daemon.ping")
        second = client.call("daemon.ping")
    assert first["pid"] == second["pid"]


def test_client_pid_property_set_after_enter(server: _ServerHandle) -> None:
    """``client.pid`` reflects the daemon PID after the context opens."""
    with DaemonClient(runtime_dir=server.runtime_dir) as client:
        assert client.pid == os.getpid()


def test_client_call_outside_context_raises(server: _ServerHandle) -> None:
    """Calling ``call`` without entering raises a clean RuntimeError."""
    client = DaemonClient(runtime_dir=server.runtime_dir)
    with pytest.raises(RuntimeError, match="not connected"):
        client.call("daemon.ping")


def test_client_closes_socket_on_exit(server: _ServerHandle) -> None:
    """After exit the client cannot send further calls."""
    with DaemonClient(runtime_dir=server.runtime_dir) as client:
        client.call("daemon.ping")
    with pytest.raises(RuntimeError, match="not connected"):
        client.call("daemon.ping")


def test_rpc_error_carries_code_message_data() -> None:
    """:class:`DaemonRpcError` exposes code + message + data fields."""
    err = DaemonRpcError(code=-32008, message="catch up too large", data={"missed": 9000})
    assert err.code == -32008
    assert err.message == "catch up too large"
    assert err.data == {"missed": 9000}
    assert "-32008" in str(err)
