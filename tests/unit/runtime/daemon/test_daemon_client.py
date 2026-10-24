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
import os
import shutil
import sys
import tempfile
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import typer

from eawf import __version__
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.lifecycle import DaemonLifecycleResult
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.server import serve_unix
from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError, release_order
from eawf.surfaces.cli.errors import DaemonVersionMismatch
from eawf.surfaces.cli.flags import GlobalFlags

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

    def close(self) -> None:
        """Stop the server and remove its runtime dir with every lock file in it."""
        self.stop()
        shutil.rmtree(self.runtime_dir, ignore_errors=True)

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
        handle.close()


def test_repeated_spawn_path_leaves_no_runtime_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Driving the spawn path over and over must not grow the temp dir.

    Each cycle boots a server under a fresh short runtime dir and enters
    :class:`DaemonClient`, whose :func:`auto_spawn_daemon` takes the spawn
    lock and probes the singleton lock. Both lock files outlive the server,
    so the harness must remove the whole dir, not only the socket and PID
    file, or every run strands one ``eawfd-*`` dir per test.
    """
    base = Path(tempfile.mkdtemp(prefix="eawfg-"))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(base))
    try:
        for _ in range(5):
            handle = _ServerHandle(_short_runtime_dir())
            handle.start()
            try:
                with DaemonClient(runtime_dir=handle.runtime_dir) as client:
                    client.call("daemon.ping")
                assert (handle.runtime_dir / "eawfd.spawn.lock").exists()
            finally:
                handle.close()
            assert list(base.iterdir()) == []
    finally:
        shutil.rmtree(base, ignore_errors=True)


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
        "verify:\n  odr_blocking: true\n",
        encoding="utf-8",
    )

    with DaemonClient(runtime_dir=server.runtime_dir) as client:
        result = client.config_unset_layer_value(
            layer="repo",
            key_path=["verify", "odr_blocking"],
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
        handle.close()


@pytest.fixture
def restarts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record every ``lifecycle.restart_daemon`` call instead of performing it."""
    calls: list[dict[str, Any]] = []

    def _record(**kwargs: Any) -> DaemonLifecycleResult:
        calls.append(kwargs)
        raise AssertionError("the client must never restart the daemon itself")

    monkeypatch.setattr("eawf.runtime.daemon.lifecycle.restart_daemon", _record)
    return calls


def test_client_refuses_an_older_daemon_without_restarting(
    stale_server: _ServerHandle, restarts: list[dict[str, Any]]
) -> None:
    """An older daemon may serve live Runs, so the client names the restart verb instead."""
    with pytest.raises(DaemonVersionMismatch) as excinfo:
        DaemonClient(runtime_dir=stale_server.runtime_dir).__enter__()
    message = str(excinfo.value)
    assert "0.6.8" in message
    assert __version__ in message
    assert "older than" in message
    assert "eawf daemon restart" in message
    assert restarts == []


def test_client_refuses_a_newer_daemon_without_restarting(
    restarts: list[dict[str, Any]],
) -> None:
    """A daemon from a later release is refused, never replaced by an older one."""
    handle = _ServerHandle(_short_runtime_dir(), version="99.0.0")
    handle.start()
    try:
        with pytest.raises(DaemonVersionMismatch) as excinfo:
            DaemonClient(runtime_dir=handle.runtime_dir).__enter__()
    finally:
        handle.close()
    message = str(excinfo.value)
    assert "99.0.0" in message
    assert __version__ in message
    assert "older than" not in message
    assert "eawf daemon restart" in message
    assert restarts == []


@pytest.mark.parametrize("version", ["", "test"])
def test_client_refuses_an_unorderable_daemon_version(
    version: str, restarts: list[dict[str, Any]]
) -> None:
    """An empty or non-release version is a mismatch too, never mistaken for current."""
    handle = _ServerHandle(_short_runtime_dir(), version=version)
    handle.start()
    try:
        with pytest.raises(DaemonVersionMismatch, match="eawf daemon restart"):
            DaemonClient(runtime_dir=handle.runtime_dir).__enter__()
    finally:
        handle.close()
    assert restarts == []


def test_read_only_attach_also_refuses_an_older_daemon(
    stale_server: _ServerHandle, restarts: list[dict[str, Any]]
) -> None:
    """A read verb that only attaches never talks to a stale daemon either."""
    with pytest.raises(DaemonVersionMismatch, match="eawf daemon restart"):
        DaemonClient(runtime_dir=stale_server.runtime_dir, spawn=False).__enter__()
    assert restarts == []


def test_refused_client_holds_no_connection(
    stale_server: _ServerHandle, restarts: list[dict[str, Any]]
) -> None:
    """After the refusal no call can reach the older daemon through the client."""
    client = DaemonClient(runtime_dir=stale_server.runtime_dir)
    with pytest.raises(DaemonVersionMismatch):
        client.__enter__()
    with pytest.raises(RuntimeError, match="not connected"):
        client.call("daemon.ping")


def test_unchecked_client_talks_to_an_older_daemon(
    stale_server: _ServerHandle, restarts: list[dict[str, Any]]
) -> None:
    """``check_version=False`` attaches to the older daemon as it is."""
    with DaemonClient(runtime_dir=stale_server.runtime_dir, check_version=False) as client:
        assert client.call("daemon.ping")["version"] == "0.6.8"
    assert restarts == []


@pytest.fixture
def daemon_cli_env(stale_server: _ServerHandle, monkeypatch: pytest.MonkeyPatch) -> _ServerHandle:
    """Point the ``eawf daemon`` verbs at the older daemon, with no OS supervisor."""
    from eawf.runtime.daemon.service_install import SupervisedAgentReport

    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(stale_server.runtime_dir))
    monkeypatch.setattr(
        "eawf.runtime.daemon.service_install.detect_supervised_agent",
        lambda **_: SupervisedAgentReport(
            supervisor="none",
            label="",
            installed=False,
            loaded=False,
            program=None,
            drift=False,
            rival_pid=None,
        ),
    )
    return stale_server


def _daemon_ctx() -> typer.Context:
    """Return the context a ``daemon`` verb reads: only ``obj`` carries flags."""
    return cast(typer.Context, SimpleNamespace(obj=GlobalFlags()))


def test_daemon_stop_reaches_an_older_daemon(
    daemon_cli_env: _ServerHandle, capsys: pytest.CaptureFixture[str]
) -> None:
    """``eawf daemon stop`` shuts an older daemon down instead of refusing it."""
    from eawf.surfaces.cli.commands.daemon import stop_cmd

    stop_cmd(_daemon_ctx(), no_drain=True, timeout=1, evict_service=False)
    out = capsys.readouterr()
    assert "daemon shutting down" in out.out
    assert "eawf daemon restart" not in out.out + out.err


def test_pipe_transport_shutdown_reaches_an_older_daemon(daemon_cli_env: _ServerHandle) -> None:
    """The win32 transport of the daemon verbs skips the version check too."""
    from eawf.surfaces.cli.commands.daemon import _rpc_call_pipe

    response = _rpc_call_pipe("daemon.shutdown", {"drain": False, "timeout_seconds": 1}, spawn=True)
    assert "error" not in response
    assert "shutdown_at" in response["result"]


def test_daemon_restart_replaces_an_older_daemon(
    daemon_cli_env: _ServerHandle,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``eawf daemon restart`` shuts the older daemon down and brings this release up."""
    from eawf.surfaces.cli.commands.daemon import restart_cmd

    handle = daemon_cli_env
    new_pid = os.getpid() + 1

    def _stopped(runtime_dir: Path, **_: Any) -> None:
        assert runtime_dir == handle.runtime_dir
        handle.stop()

    def _spawn(runtime_dir: Path) -> int:
        assert runtime_dir == handle.runtime_dir
        handle.version = __version__
        handle.start()
        return new_pid

    monkeypatch.setattr("eawf.runtime.daemon.lifecycle.wait_for_daemon_stopped", _stopped)
    monkeypatch.setattr("eawf.runtime.daemon.lifecycle.auto_spawn_daemon", _spawn)
    restart_cmd(_daemon_ctx(), no_drain=False, timeout=1)
    assert f"daemon restarted pid={new_pid}" in capsys.readouterr().out
    with DaemonClient(runtime_dir=handle.runtime_dir) as client:
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
