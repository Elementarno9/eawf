"""End-to-end coverage for the W02 Windows named-pipe transport.

Every test is gated on ``sys.platform == "win32"`` because the only
useful way to verify the transport is to drive the actual pywin32
``CreateNamedPipe`` + ``CreateFile`` round-trip. On macOS / Linux the
suite reports SKIPPED for each case.

Coverage:

- ``WindowsPipeServer.start()`` + ``ping`` client round-trip via
  :func:`win32file.CreateFile` / :func:`win32file.WriteFile` /
  :func:`win32file.ReadFile`.
- ``stop()`` wakes the listener thread within a short bound.
- Post-connect SID mismatch closes the connection with a
  ``-32000 unauthorized`` envelope (covered with a mocked verifier
  to avoid spawning a second user).
- Malformed JSON returns the JSON-RPC ``-32700 parse error``
  envelope through the same pipe.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from typing import Any

import orjson
import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-only transport",
)


def _unique_pipe_name(stem: str) -> str:
    return rf"\\.\pipe\eawfd-test-{stem}-{uuid.uuid4().hex[:8]}"


def _build_ctx() -> Any:
    """Return a :class:`MethodContext` wired with a fresh shutdown event."""
    from eawf import __version__
    from eawf.runtime.daemon import PROTOCOL_VERSION
    from eawf.runtime.daemon.methods import MethodContext

    return MethodContext(
        started_at="2026-05-19T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
    )


def _client_round_trip(pipe_name: str, payload: bytes) -> bytes:
    """Open a client handle, write *payload*, read one frame back.

    Args:
        pipe_name: Pipe path created by the server under test.
        payload: Raw bytes to write (one JSON-RPC frame).

    Returns:
        Raw bytes returned by the server (one JSON-RPC response frame).
    """
    import win32file  # type: ignore[import-not-found]
    import win32pipe  # type: ignore[import-not-found]

    # Wait briefly for the server to bind the pipe.
    win32pipe.WaitNamedPipe(pipe_name, 5000)

    handle = win32file.CreateFile(
        pipe_name,
        win32file.GENERIC_READ | win32file.GENERIC_WRITE,
        0,
        None,
        win32file.OPEN_EXISTING,
        0,
        None,
    )
    try:
        win32file.WriteFile(handle, payload)
        _hr, response = win32file.ReadFile(handle, 65536)
        return bytes(response)
    finally:
        win32file.CloseHandle(handle)


def test_ping_round_trip_via_named_pipe() -> None:
    """`daemon.ping` client → server → response over the named pipe."""
    from eawf.runtime.daemon.server import process_frame_bytes
    from eawf.runtime.daemon.windows_pipe import WindowsPipeServer

    pipe_name = _unique_pipe_name("ping")
    ctx = _build_ctx()
    result: dict[str, Any] = {}

    async def runner() -> None:
        loop = asyncio.get_running_loop()

        async def handler(payload: bytes) -> bytes:
            return await process_frame_bytes(payload, ctx)

        server = WindowsPipeServer(
            loop,
            handler,
            pipe_name=pipe_name,
            verify_sid_enabled=False,
        )
        server.start()
        try:
            request = orjson.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "1",
                    "method": "daemon.ping",
                    "params": {},
                }
            )
            response_bytes = await asyncio.to_thread(_client_round_trip, pipe_name, request)
            result["payload"] = orjson.loads(response_bytes.rstrip(b"\n"))
        finally:
            server.stop()
            # Allow the listener thread to drain.
            await asyncio.sleep(0.05)

    asyncio.run(runner())
    payload = result["payload"]
    assert "error" not in payload, payload
    assert payload["result"]["pid"] == os.getpid()


def test_stop_unblocks_listener_thread() -> None:
    """``stop()`` wakes the listener thread within 2 s."""
    from eawf.runtime.daemon.windows_pipe import WindowsPipeServer

    pipe_name = _unique_pipe_name("stop")

    async def runner() -> None:
        loop = asyncio.get_running_loop()

        async def handler(payload: bytes) -> bytes:
            return b'{"jsonrpc":"2.0","id":null,"result":{}}\n'

        server = WindowsPipeServer(
            loop,
            handler,
            pipe_name=pipe_name,
            verify_sid_enabled=False,
        )
        server.start()
        await asyncio.sleep(0.1)
        listener = server._listener_thread  # type: ignore[attr-defined]
        assert listener is not None and listener.is_alive()
        server.stop()
        deadline = time.monotonic() + 2.0
        while listener.is_alive() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert not listener.is_alive(), "listener thread did not exit after stop()"

    asyncio.run(runner())


def test_malformed_frame_returns_parse_error_envelope() -> None:
    """Garbage bytes produce a ``-32700 parse error`` response frame."""
    from eawf.runtime.daemon.server import process_frame_bytes
    from eawf.runtime.daemon.windows_pipe import WindowsPipeServer

    pipe_name = _unique_pipe_name("parse")
    ctx = _build_ctx()
    result: dict[str, Any] = {}

    async def runner() -> None:
        loop = asyncio.get_running_loop()

        async def handler(payload: bytes) -> bytes:
            return await process_frame_bytes(payload, ctx)

        server = WindowsPipeServer(
            loop,
            handler,
            pipe_name=pipe_name,
            verify_sid_enabled=False,
        )
        server.start()
        try:
            response_bytes = await asyncio.to_thread(_client_round_trip, pipe_name, b"{not-json\n")
            result["payload"] = orjson.loads(response_bytes.rstrip(b"\n"))
        finally:
            server.stop()
            await asyncio.sleep(0.05)

    asyncio.run(runner())
    assert result["payload"]["error"]["code"] == -32700


def test_dacl_post_connect_sid_check_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second user (mocked SID) is rejected post-connect.

    Spawning a real second user inside CI is impractical; instead we
    monkeypatch :func:`verify_peer_sid` to raise so the listener path
    is exercised end-to-end. The DACL itself is constructed against
    the running user's SID and is exercised in
    :mod:`test_windows_pipe_design_only`.
    """
    from eawf.runtime.daemon.windows_pipe import WindowsPipeServer
    from eawf.runtime.daemon.windows_security import WindowsAuthError

    pipe_name = _unique_pipe_name("sid")
    result: dict[str, Any] = {}

    def _fake_verify(handle: Any, expected: Any) -> None:
        raise WindowsAuthError("peer sid mismatch: expected S-1-1, got S-2-2")

    monkeypatch.setattr(
        "eawf.runtime.daemon.windows_security.verify_peer_sid",
        _fake_verify,
        raising=True,
    )

    async def runner() -> None:
        loop = asyncio.get_running_loop()

        async def handler(payload: bytes) -> bytes:
            # Should never be reached when the SID check rejects.
            return b'{"jsonrpc":"2.0","id":null,"result":{"unexpected":true}}\n'

        server = WindowsPipeServer(
            loop,
            handler,
            pipe_name=pipe_name,
            verify_sid_enabled=True,
        )
        server.start()
        try:
            response_bytes = await asyncio.to_thread(
                _client_round_trip,
                pipe_name,
                orjson.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "1",
                        "method": "daemon.ping",
                        "params": {},
                    }
                ),
            )
            result["payload"] = orjson.loads(response_bytes.rstrip(b"\n"))
        finally:
            server.stop()
            await asyncio.sleep(0.05)

    asyncio.run(runner())
    assert result["payload"]["error"]["code"] == -32000
    assert "unexpected" not in str(result["payload"])


def test_state_mutate_round_trips_via_pipe_client_call() -> None:
    """``pipe_client_call`` drives a full request/response round-trip.

    Exercises the W02 synchronous client transport (the one the CLI
    ``DaemonClient`` uses on Windows) against a live ``WindowsPipeServer``,
    proving the WaitNamedPipe -> CreateFile -> WriteFile -> read-loop path
    works for a non-ping method.
    """
    from eawf.runtime.daemon.server import process_frame_bytes
    from eawf.runtime.daemon.windows_pipe import WindowsPipeServer, pipe_client_call

    pipe_name = _unique_pipe_name("mutate")
    ctx = _build_ctx()
    result: dict[str, Any] = {}

    async def runner() -> None:
        loop = asyncio.get_running_loop()

        async def handler(payload: bytes) -> bytes:
            return await process_frame_bytes(payload, ctx)

        server = WindowsPipeServer(loop, handler, pipe_name=pipe_name, verify_sid_enabled=False)
        server.start()
        try:
            request = orjson.dumps(
                {"jsonrpc": "2.0", "id": "1", "method": "daemon.status", "params": {}}
            )
            response_bytes = await asyncio.to_thread(pipe_client_call, pipe_name, request + b"\n")
            result["payload"] = orjson.loads(response_bytes.rstrip(b"\n"))
        finally:
            server.stop()
            await asyncio.sleep(0.05)

    asyncio.run(runner())
    assert "error" not in result["payload"], result["payload"]
    assert "active_subscriptions" in result["payload"]["result"]


def test_large_frame_reassembles_via_more_data_loop() -> None:
    """A response larger than the pipe buffer reassembles, never truncates.

    The handler returns a payload several times ``_PIPE_BUFFER_BYTES`` so a
    single ``ReadFile`` cannot hold it; ``pipe_client_call`` must drain the
    ``ERROR_MORE_DATA`` tail. A truncated read would fail JSON parse or
    drop the marker, so byte-exact reassembly is the proof.
    """
    from eawf.runtime.daemon.windows_pipe import (
        _PIPE_BUFFER_BYTES,
        WindowsPipeServer,
        pipe_client_call,
    )

    pipe_name = _unique_pipe_name("bigframe")
    # ~3.5 buffers of filler so the message spans multiple MORE_DATA chunks.
    big_value = "z" * (_PIPE_BUFFER_BYTES * 3 + 1234)
    result: dict[str, Any] = {}

    async def runner() -> None:
        loop = asyncio.get_running_loop()

        async def handler(payload: bytes) -> bytes:
            # Echo a large success envelope regardless of the request body.
            return (
                orjson.dumps({"jsonrpc": "2.0", "id": "1", "result": {"blob": big_value}}) + b"\n"
            )

        server = WindowsPipeServer(loop, handler, pipe_name=pipe_name, verify_sid_enabled=False)
        server.start()
        try:
            request = orjson.dumps(
                {"jsonrpc": "2.0", "id": "1", "method": "daemon.ping", "params": {}}
            )
            response_bytes = await asyncio.to_thread(pipe_client_call, pipe_name, request + b"\n")
            result["payload"] = orjson.loads(response_bytes.rstrip(b"\n"))
        finally:
            server.stop()
            await asyncio.sleep(0.05)

    asyncio.run(runner())
    assert result["payload"]["result"]["blob"] == big_value
    assert len(result["payload"]["result"]["blob"]) == _PIPE_BUFFER_BYTES * 3 + 1234


def test_large_request_frame_reassembles_server_side() -> None:
    """A REQUEST larger than the pipe buffer reassembles on the server read.

    Mirrors the response-side reassembly for the inbound direction: the
    client sends a frame several buffers long; the listener's
    ``_read_first_chunk`` + MORE_DATA drain must hand the handler the full
    payload. The handler echoes back the received byte length so a
    truncated server read surfaces as a mismatch.
    """
    from eawf.runtime.daemon.windows_pipe import (
        _PIPE_BUFFER_BYTES,
        WindowsPipeServer,
        pipe_client_call,
    )

    pipe_name = _unique_pipe_name("bigreq")
    filler = "q" * (_PIPE_BUFFER_BYTES * 2 + 77)
    request = orjson.dumps(
        {"jsonrpc": "2.0", "id": "1", "method": "daemon.ping", "params": {"pad": filler}}
    )
    expected_len = len(request + b"\n")
    result: dict[str, Any] = {}

    async def runner() -> None:
        loop = asyncio.get_running_loop()

        async def handler(payload: bytes) -> bytes:
            return (
                orjson.dumps(
                    {"jsonrpc": "2.0", "id": "1", "result": {"received_len": len(payload)}}
                )
                + b"\n"
            )

        server = WindowsPipeServer(loop, handler, pipe_name=pipe_name, verify_sid_enabled=False)
        server.start()
        try:
            response_bytes = await asyncio.to_thread(pipe_client_call, pipe_name, request + b"\n")
            result["payload"] = orjson.loads(response_bytes.rstrip(b"\n"))
        finally:
            server.stop()
            await asyncio.sleep(0.05)

    asyncio.run(runner())
    assert result["payload"]["result"]["received_len"] == expected_len


def test_pipe_ready_probe_does_not_consume_an_instance() -> None:
    """``pipe_ready`` reports availability without stealing a pipe instance.

    The per-user pipe is a singleton listener with one free instance at a
    time. If the readiness probe consumed an instance, the following real
    RPC would block / fail. This test probes readiness, then immediately
    drives a full ``pipe_client_call`` round-trip on the SAME instance and
    asserts it still succeeds -- proving the probe did not consume it.
    """
    from eawf.runtime.daemon.windows_pipe import (
        WindowsPipeServer,
        pipe_client_call,
        pipe_ready,
    )

    pipe_name = _unique_pipe_name("ready")
    result: dict[str, Any] = {}

    async def runner() -> None:
        loop = asyncio.get_running_loop()

        async def handler(payload: bytes) -> bytes:
            return b'{"jsonrpc":"2.0","id":"1","result":{"ok":true}}\n'

        server = WindowsPipeServer(loop, handler, pipe_name=pipe_name, verify_sid_enabled=False)
        server.start()
        try:

            def _probe_then_call() -> tuple[bool, bytes]:
                ready = pipe_ready(pipe_name, 5000)
                request = orjson.dumps(
                    {"jsonrpc": "2.0", "id": "1", "method": "daemon.ping", "params": {}}
                )
                return ready, pipe_client_call(pipe_name, request + b"\n")

            ready, response_bytes = await asyncio.to_thread(_probe_then_call)
            result["ready"] = ready
            result["payload"] = orjson.loads(response_bytes.rstrip(b"\n"))
        finally:
            server.stop()
            await asyncio.sleep(0.05)

    asyncio.run(runner())
    assert result["ready"] is True
    assert result["payload"]["result"]["ok"] is True
