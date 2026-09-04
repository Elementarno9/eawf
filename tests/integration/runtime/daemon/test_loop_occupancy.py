"""Head-of-line blocking on the daemon's dispatch path.

A handler that does blocking IO plus validation without ever awaiting owns
the event loop for its whole duration, and while it does, no other
connection's frame is read, parsed or answered. The symptom is not "one
slow read": it is a daemon that looks DEAD to every other caller, because
the readiness probe on a second connection cannot get an answer inside its
budget and reports the daemon absent.

The two cases below are the same ``state.read`` call over the same socket,
differing only in whether ``state.read`` is in
:data:`eawf.runtime.daemon.server.OFFLOADED_METHODS`. The inline case pins
the defect (a concurrent ping stalls for the whole hold); the offloaded case
pins the fix (the ping answers inside the readiness budget).

The daemon runs on its own thread with its own loop, and the clients are
plain blocking sockets on the main thread — a single-loop harness cannot
observe head-of-line blocking, because the client coroutine is stalled by
the very loop it is trying to measure.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import orjson
import pytest

import eawf.runtime.daemon.methods.state as state_mod
import eawf.runtime.daemon.server as server_mod
from eawf.kernel.state.models import State
from eawf.runtime.daemon.limits import READINESS_BUDGET_SECONDS
from eawf.runtime.daemon.methods import MethodContext, MethodNotFoundError
from eawf.runtime.daemon.server import handle_connection
from tests.integration.runtime.daemon.test_close_lock_split import _build_ctx, _write_state

pytestmark = pytest.mark.integration

#: Synchronous hold injected into ``state.read``. Long enough that a stall
#: cannot be confused with scheduling jitter and that a concurrent probe
#: would blow through :data:`READINESS_BUDGET_SECONDS` several times over.
HOLD_SECONDS = 2.0

#: Ceiling on any client socket operation, so a regression fails the test
#: instead of hanging the suite.
CLIENT_TIMEOUT_SECONDS = 30.0


def short_socket_path(tag: str) -> str:
    """Return a socket path under the AF_UNIX length ceiling (macOS: 104)."""
    return os.path.join(tempfile.gettempdir(), f"eawf-{tag}-{uuid.uuid4().hex[:8]}.sock")


@contextlib.contextmanager
def serve_on_thread(ctx: MethodContext, sock_path: str) -> Iterator[str]:
    """Run a real JSON-RPC daemon on its own thread + loop for the block.

    Args:
        ctx: Server context handed to every connection.
        sock_path: UDS path to bind.

    Yields:
        The bound socket path.
    """
    ready = threading.Event()
    loops: list[asyncio.AbstractEventLoop] = []

    def _serve() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loops.append(loop)
        server = loop.run_until_complete(
            asyncio.start_unix_server(
                lambda r, w: handle_connection(r, w, ctx),
                path=sock_path,
            )
        )
        ready.set()
        try:
            loop.run_forever()
        finally:
            # Cancel the connection handlers BEFORE awaiting the server:
            # a handler transport still alive when ``wait_closed`` returns
            # is finalised against a server that has dropped its waiter
            # set, which surfaces as an unraisable TypeError at GC.
            server.close()
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(server.wait_closed())
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    thread = threading.Thread(target=_serve, name="eawfd-occupancy-test", daemon=True)
    thread.start()
    assert ready.wait(timeout=CLIENT_TIMEOUT_SECONDS), "test daemon never bound its socket"
    try:
        yield sock_path
    finally:
        loops[0].call_soon_threadsafe(loops[0].stop)
        thread.join(timeout=CLIENT_TIMEOUT_SECONDS)
        with contextlib.suppress(OSError):
            os.unlink(sock_path)


def request_frame(method: str, params: dict[str, Any] | None = None) -> bytes:
    """Return one newline-terminated JSON-RPC request frame."""
    payload = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": method,
        "params": params or {},
    }
    return orjson.dumps(payload) + b"\n"


@contextlib.contextmanager
def client(sock_path: str) -> Iterator[socket.socket]:
    """Open one blocking client connection to *sock_path*."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(CLIENT_TIMEOUT_SECONDS)
    try:
        conn.connect(sock_path)
        yield conn
    finally:
        conn.close()


def read_response(conn: socket.socket) -> dict[str, Any]:
    """Read exactly one response frame off *conn*."""
    reader = conn.makefile("rb")
    try:
        line = reader.readline()
    finally:
        reader.close()
    assert line, "peer closed without answering"
    decoded: dict[str, Any] = orjson.loads(line)
    return decoded


def install_read_hold(
    monkeypatch: pytest.MonkeyPatch,
    started: threading.Event,
    *,
    hold_seconds: float = HOLD_SECONDS,
) -> None:
    """Make ``state.read`` hold synchronously for *hold_seconds*.

    Stands in for the blocking IO + Pydantic validation the real handler
    does, scaled up so the stall is unambiguous.
    """
    real_read_state = state_mod._read_state

    def _held_read_state(state_path: Path) -> tuple[State, dict[str, Any]]:
        started.set()
        time.sleep(hold_seconds)
        return real_read_state(state_path)

    monkeypatch.setattr(state_mod, "_read_state", _held_read_state)


def _ping_latency_during_hold(
    ctx: MethodContext,
    monkeypatch: pytest.MonkeyPatch,
    tag: str,
) -> tuple[float, dict[str, Any]]:
    """Time a ``daemon.ping`` issued while a ``state.read`` hold is running.

    Returns:
        ``(ping_latency_seconds, state_read_response)``.
    """
    started = threading.Event()
    install_read_hold(monkeypatch, started)
    with serve_on_thread(ctx, short_socket_path(tag)) as sock_path, client(sock_path) as holder:
        holder.sendall(request_frame("state.read"))
        assert started.wait(timeout=CLIENT_TIMEOUT_SECONDS), "the held read never started"
        with client(sock_path) as prober:
            before = time.monotonic()
            prober.sendall(request_frame("daemon.ping"))
            ping_response = read_response(prober)
            latency = time.monotonic() - before
        assert "result" in ping_response, ping_response
        return latency, read_response(holder)


def test_inline_handler_stalls_an_unrelated_ping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR RUN-054: with ``state.read`` on the loop, a ping on a second
    connection waits out the whole hold — head-of-line blocking, and the
    reason a busy daemon reads as an absent one."""
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    ctx = _build_ctx(tmp_path, state_path)
    monkeypatch.setattr(server_mod, "OFFLOADED_METHODS", frozenset())

    latency, read_response_payload = _ping_latency_during_hold(ctx, monkeypatch, "occ-inline")

    assert latency > READINESS_BUDGET_SECONDS, (
        f"ping answered in {latency:.3f}s with the loop held — the harness is not "
        f"reproducing head-of-line blocking, so the offloaded case proves nothing"
    )
    assert latency >= HOLD_SECONDS / 2
    assert "result" in read_response_payload, read_response_payload


def test_offloaded_handler_leaves_the_ping_within_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR RUN-054: the same two-second hold, offloaded, no longer stalls the
    ping — it answers inside the shared readiness budget."""
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    ctx = _build_ctx(tmp_path, state_path)
    assert "state.read" in server_mod.OFFLOADED_METHODS

    latency, read_response_payload = _ping_latency_during_hold(ctx, monkeypatch, "occ-offload")

    assert latency < READINESS_BUDGET_SECONDS, (
        f"ping took {latency:.3f}s while an offloaded read was in flight — "
        f"something is still holding the loop"
    )
    assert "result" in read_response_payload, read_response_payload


def test_offloaded_read_returns_the_same_payload_as_an_inline_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offloading changes where the handler runs, not what it answers."""
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    ctx = _build_ctx(tmp_path, state_path)

    offloaded = asyncio.run(server_mod._dispatch_method("state.read", ctx, {}))
    monkeypatch.setattr(server_mod, "OFFLOADED_METHODS", frozenset())
    inline = asyncio.run(server_mod._dispatch_method("state.read", ctx, {}))

    assert offloaded == inline
    assert offloaded["version"]


def test_dispatch_method_raises_method_not_found_for_an_unknown_method(
    tmp_path: Path,
) -> None:
    """Error path: an unknown method faults the same way on either route."""
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    ctx = _build_ctx(tmp_path, state_path)

    with pytest.raises(MethodNotFoundError):
        asyncio.run(server_mod._dispatch_method("state.no_such_method", ctx, {}))


def test_dispatch_method_propagates_handler_errors_across_the_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Error path: a handler exception raised on the worker thread reaches
    the loop unchanged, so the server still maps it to its wire code."""
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    ctx = _build_ctx(tmp_path, state_path)

    def _corrupt_read_state(state_path: Path) -> tuple[State, dict[str, Any]]:
        raise ValueError("state schema invalid: injected")

    monkeypatch.setattr(state_mod, "_read_state", _corrupt_read_state)

    with pytest.raises(ValueError, match="state schema invalid"):
        asyncio.run(server_mod._dispatch_method("state.read", ctx, {}))
