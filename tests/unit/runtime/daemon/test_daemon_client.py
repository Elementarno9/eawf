"""Tests for :class:`eawf.surfaces.cli._daemon_client.DaemonClient`.

The tests spin up a real :func:`eawf.runtime.daemon.server.serve_unix` on a
per-test temp UDS path, then drive the synchronous client against it
from a background thread (the asyncio loop owns the server; the
client uses blocking sockets). This exercises the full wire format
end-to-end without requiring the cold-spawn fork+exec path.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from eawf import __version__
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.lifecycle import DaemonLifecycleError, DaemonLifecycleResult
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.server import serve_unix
from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError, release_order
from eawf.surfaces.cli.errors import DaemonVersionMismatch

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

    def __init__(self, runtime_dir: Path, *, version: str = __version__) -> None:
        self.runtime_dir = runtime_dir
        self.version = version
        self.sock_path = runtime_dir / "eawfd.sock"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.Server | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._pid_file = runtime_dir / "eawfd.pid"

    def start(self) -> None:
        self._ready.clear()
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
            version=self.version,
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


def test_client_round_trips_config_unset_over_real_rpc(
    server: _ServerHandle, tmp_path: Path
) -> None:
    """The typed client method reaches the daemon writer and returns the unset envelope."""
    repo = tmp_path / "repo"
    config_path = repo / ".ea" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "flow:\n  advance_after:\n    audit: true\n",
        encoding="utf-8",
    )

    with DaemonClient(runtime_dir=server.runtime_dir) as client:
        result = client.config_unset_layer_value(
            layer="repo",
            key_path=["flow", "advance_after", "audit"],
            repo_root=str(repo),
        )

    assert result["removed"] is True
    assert result["envelope"]["payload"]["operation"] == "unset"
    assert result["envelope"]["payload"]["value"] is None
    assert config_path.read_text(encoding="utf-8") == "{}\n"


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


@pytest.fixture
def stale_server() -> Iterator[_ServerHandle]:
    """A daemon left running by the previous release, reporting ``0.6.8``."""
    handle = _ServerHandle(_short_runtime_dir(), version="0.6.8")
    handle.start()
    try:
        yield handle
    finally:
        handle.stop()


def _restart_into(handle: _ServerHandle, version: str) -> Callable[..., DaemonLifecycleResult]:
    """Return a ``restart_daemon`` double that swaps *handle* for a *version* daemon."""

    def _restart(*, runtime_dir: Path | None = None, **_: Any) -> DaemonLifecycleResult:
        assert runtime_dir == handle.runtime_dir
        handle.stop()
        handle.version = version
        handle.start()
        return DaemonLifecycleResult(
            action="restarted", pid=os.getpid(), previous_pid=os.getpid(), supervisor="none"
        )

    return _restart


def test_client_restarts_an_older_daemon_before_the_call(
    stale_server: _ServerHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 0.6.8 daemon is replaced before the first call reaches it."""
    monkeypatch.setattr(
        "eawf.runtime.daemon.lifecycle.restart_daemon",
        _restart_into(stale_server, __version__),
    )
    with caplog.at_level(logging.INFO), DaemonClient(runtime_dir=stale_server.runtime_dir) as c:
        result = c.call("daemon.ping")
    assert result["version"] == __version__
    assert "daemon_version='0.6.8'" in caplog.text
    assert "restarted" in caplog.text


def test_client_refuses_a_newer_daemon_without_restarting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon from a later release is refused, never replaced by an older one."""
    handle = _ServerHandle(_short_runtime_dir(), version="99.0.0")
    handle.start()
    restarts: list[object] = []
    monkeypatch.setattr(
        "eawf.runtime.daemon.lifecycle.restart_daemon", lambda **kw: restarts.append(kw)
    )
    try:
        with pytest.raises(DaemonVersionMismatch) as excinfo:
            DaemonClient(runtime_dir=handle.runtime_dir).__enter__()
    finally:
        handle.stop()
    message = str(excinfo.value)
    assert "99.0.0" in message
    assert __version__ in message
    assert "eawf daemon restart" in message
    assert restarts == []


def test_client_refuses_when_the_restart_fails(
    stale_server: _ServerHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed replacement surfaces both versions instead of calling the old daemon."""

    def _fail(**_: Any) -> DaemonLifecycleResult:
        raise DaemonLifecycleError("daemon did not stop within 30.0s pid=1")

    monkeypatch.setattr("eawf.runtime.daemon.lifecycle.restart_daemon", _fail)
    with pytest.raises(DaemonVersionMismatch) as excinfo:
        DaemonClient(runtime_dir=stale_server.runtime_dir).__enter__()
    message = str(excinfo.value)
    assert "0.6.8" in message
    assert __version__ in message
    assert "did not stop" in message
    assert "eawf daemon restart" in message


def test_client_refuses_when_the_restarted_daemon_is_still_stale(
    stale_server: _ServerHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A supervisor that restarts the old binary again is refused, not looped on."""
    monkeypatch.setattr(
        "eawf.runtime.daemon.lifecycle.restart_daemon", _restart_into(stale_server, "0.6.8")
    )
    with pytest.raises(DaemonVersionMismatch, match=r"0\.6\.8"):
        DaemonClient(runtime_dir=stale_server.runtime_dir).__enter__()


def test_read_only_attach_also_replaces_an_older_daemon(
    stale_server: _ServerHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read verb that only attaches still never talks to a stale daemon."""
    monkeypatch.setattr(
        "eawf.runtime.daemon.lifecycle.restart_daemon",
        _restart_into(stale_server, __version__),
    )
    with DaemonClient(runtime_dir=stale_server.runtime_dir, spawn=False) as client:
        assert client.call("daemon.ping")["version"] == __version__


@pytest.mark.parametrize(
    ("daemon", "client", "order"),
    [
        ("0.6.8", "0.7.0rc1", -1),
        ("0.7.0.dev5", "0.7.0rc1", -1),
        ("0.7.0rc1", "0.7.0", -1),
        ("0.7.0a1", "0.7.0b1", -1),
        ("0.7.0rc1.dev2", "0.7.0rc1", -1),
        ("0.7.0.dev9", "0.7.0a1", -1),
        ("0.7", "0.7.0", 0),
        ("0.7.0rc1", "0.7.0rc1", 0),
        ("0.7.1", "0.7.0", 1),
        ("1.0.0", "0.99.99", 1),
        ("0.7.0rc2", "0.7.0rc1", 1),
    ],
)
def test_release_order_follows_pep440(daemon: str, client: str, order: int) -> None:
    """Release, pre-release and dev segments order as PEP 440 does."""
    assert release_order(daemon, client) == order


@pytest.mark.parametrize("version", ["", "test", "0.7.0+local", "v0.7.0", "0.7.0post1"])
def test_release_order_is_none_for_an_unorderable_version(version: str) -> None:
    """A version outside the release grammar cannot be ordered, so it is never restarted."""
    assert release_order(version, "0.7.0") is None
