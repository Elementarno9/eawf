"""End-to-end coverage for the W01 daemon scaffolding.

Tests boot the asyncio JSON-RPC server on a per-test UDS path, exercise
``daemon.ping``/``daemon.status``/``daemon.shutdown`` happy paths, then
walk the error envelopes for malformed frames, unknown methods, invalid
params, and peer-credential rejection.

The suite does not depend on ``pytest-asyncio``; every test that touches
the event loop wraps the async body in :func:`asyncio.run`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import sys
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf import __version__
from eawf.daemon import PROTOCOL_VERSION
from eawf.daemon.auth import UnauthorizedError, check_peer_uid
from eawf.daemon.methods import MethodContext, registered_methods
from eawf.daemon.runtime_dir import pid_path, runtime_dir, socket_path
from eawf.daemon.server import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    serve_unix,
)

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="UDS not available on windows; W02 wires the pipe",
)


def _short_sock_path() -> Path:
    """Return a unique UDS path short enough for AF_UNIX (104-byte cap on macOS).

    ``tmp_path`` lives under ``/var/folders/...`` which routinely exceeds
    the cap, so tests place the socket directly under ``$TMPDIR`` (or
    ``/tmp`` when unset) with a short uuid stem.
    """
    base = Path(tempfile.gettempdir())
    return base / f"eawfd-{uuid.uuid4().hex[:8]}.sock"


def _build_ctx() -> MethodContext:
    return MethodContext(
        started_at="2026-05-19T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
    )


async def _round_trip(
    sock_path: Path,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    raw_line: bytes | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Open one connection, send one frame, return the parsed response."""
    reader, writer = await asyncio.open_unix_connection(path=str(sock_path))
    try:
        if raw_line is not None:
            writer.write(raw_line)
        else:
            req: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": request_id or str(uuid.uuid4()),
                "method": method,
                "params": params or {},
            }
            writer.write(orjson.dumps(req) + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        assert line, "daemon closed without replying"
        payload = orjson.loads(line)
        assert isinstance(payload, dict)
        return payload
    finally:
        writer.close()
        with contextlib.suppress(ConnectionResetError, BrokenPipeError, OSError):
            await writer.wait_closed()


def _with_server(
    sock_path: Path,
    body: Callable[[MethodContext], Awaitable[None]],
    *,
    expected_uid: int | None = None,
) -> None:
    """Boot a server on *sock_path*, run *body* with the live context, tear down.

    Args:
        sock_path: Filesystem path for the per-test UDS bind address.
        body: Async callable invoked with the live :class:`MethodContext`.
        expected_uid: Optional peer-cred match target.
    """

    async def runner() -> None:
        ctx = _build_ctx()
        srv = await serve_unix(str(sock_path), ctx, expected_uid=expected_uid)
        try:
            await body(ctx)
        finally:
            srv.close()
            await srv.wait_closed()

    try:
        asyncio.run(runner())
    finally:
        with contextlib.suppress(FileNotFoundError):
            sock_path.unlink()


def test_protocol_version_is_string_1() -> None:
    assert PROTOCOL_VERSION == "1"


def test_method_registry_lists_daemon_namespace() -> None:
    methods = registered_methods()
    assert "daemon.ping" in methods
    assert "daemon.status" in methods
    assert "daemon.shutdown" in methods


def test_runtime_dir_returns_per_user_path() -> None:
    rt = runtime_dir()
    assert rt.is_absolute()
    assert socket_path().name == "eawfd.sock"
    assert pid_path().name == "eawfd.pid"


def test_ping_returns_pid_version_and_uptime() -> None:
    sock = _short_sock_path()

    async def body(_ctx: MethodContext) -> None:
        response = await _round_trip(sock, "daemon.ping", {})
        assert "error" not in response, response
        result = response["result"]
        assert result["pid"] == os.getpid()
        assert result["version"] == __version__
        assert result["protocol_version"] == PROTOCOL_VERSION
        assert isinstance(result["started_at"], str)
        assert isinstance(result["uptime_seconds"], int | float)
        assert result["uptime_seconds"] >= 0

    _with_server(sock, body)


def test_status_returns_zero_counters_on_warm_daemon() -> None:
    sock = _short_sock_path()

    async def body(_ctx: MethodContext) -> None:
        response = await _round_trip(sock, "daemon.status", {})
        assert "error" not in response, response
        result = response["result"]
        assert result["active_subscriptions"] == 0
        assert result["in_flight_mutations"] == 0
        assert result["last_event_id"] == ""
        assert result["pid"] == os.getpid()

    _with_server(sock, body)


def test_shutdown_sets_event_and_returns_envelope() -> None:
    sock = _short_sock_path()

    async def body(ctx: MethodContext) -> None:
        response = await _round_trip(sock, "daemon.shutdown", {"drain": True, "timeout_seconds": 5})
        assert "error" not in response, response
        result = response["result"]
        assert result["drained"] is True
        assert "shutdown_at" in result
        assert isinstance(ctx.shutdown_event, asyncio.Event)
        assert ctx.shutdown_event.is_set()

    _with_server(sock, body)


def test_shutdown_drain_false_returns_drained_false() -> None:
    sock = _short_sock_path()

    async def body(_ctx: MethodContext) -> None:
        response = await _round_trip(
            sock, "daemon.shutdown", {"drain": False, "timeout_seconds": 0}
        )
        assert response["result"]["drained"] is False

    _with_server(sock, body)


def test_malformed_json_returns_parse_error() -> None:
    sock = _short_sock_path()

    async def body(_ctx: MethodContext) -> None:
        response = await _round_trip(sock, "", raw_line=b"{not-json\n")
        assert response["error"]["code"] == PARSE_ERROR

    _with_server(sock, body)


def test_unknown_method_returns_method_not_found() -> None:
    sock = _short_sock_path()

    async def body(_ctx: MethodContext) -> None:
        response = await _round_trip(sock, "daemon.nope", {})
        assert response["error"]["code"] == METHOD_NOT_FOUND

    _with_server(sock, body)


def test_invalid_params_object_returns_invalid_params() -> None:
    sock = _short_sock_path()

    async def body(_ctx: MethodContext) -> None:
        response = await _round_trip(
            sock,
            "daemon.ping",
            raw_line=orjson.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "x",
                    "method": "daemon.ping",
                    "params": ["not", "an", "object"],
                }
            )
            + b"\n",
        )
        assert response["error"]["code"] == INVALID_PARAMS

    _with_server(sock, body)


def test_ping_with_extra_field_rejected() -> None:
    sock = _short_sock_path()

    async def body(_ctx: MethodContext) -> None:
        response = await _round_trip(sock, "daemon.ping", {"unexpected": 1})
        assert response["error"]["code"] == INVALID_PARAMS

    _with_server(sock, body)


def test_missing_jsonrpc_field_returns_invalid_request() -> None:
    sock = _short_sock_path()

    async def body(_ctx: MethodContext) -> None:
        response = await _round_trip(
            sock,
            "",
            raw_line=orjson.dumps({"id": "x", "method": "daemon.ping", "params": {}}) + b"\n",
        )
        assert response["error"]["code"] == INVALID_REQUEST

    _with_server(sock, body)


def test_peer_credential_mismatch_rejected() -> None:
    """Forged ``expected_uid`` → peer-cred check rejects with ``-32000``."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses peer-cred mismatch")
    sock = _short_sock_path()
    bogus_uid = os.geteuid() + 1

    async def body(_ctx: MethodContext) -> None:
        reader, writer = await asyncio.open_unix_connection(path=str(sock))
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            assert line, "daemon closed silently on peer-cred reject"
            payload = orjson.loads(line)
            assert payload["error"]["code"] == -32000
        finally:
            writer.close()
            with contextlib.suppress(ConnectionResetError, BrokenPipeError, OSError):
                await writer.wait_closed()

    _with_server(sock, body, expected_uid=bogus_uid)


def test_check_peer_uid_raises_on_mismatch() -> None:
    """:func:`check_peer_uid` raises :class:`UnauthorizedError` on UID skew."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses mismatch")
    s1, s2 = socket.socketpair()
    try:
        check_peer_uid(s2, os.geteuid())  # sanity: no-raise on match
        with pytest.raises(UnauthorizedError):
            check_peer_uid(s2, os.geteuid() + 1)
    finally:
        s1.close()
        s2.close()
