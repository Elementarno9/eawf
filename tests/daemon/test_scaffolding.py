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
import stat
import subprocess
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
from eawf.daemon.runtime_dir import (
    RUNTIME_DIR_MODE,
    ensure_runtime_dir,
    harden_runtime_dir,
    pid_path,
    runtime_dir,
    socket_path,
)
from eawf.daemon.server import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SOCKET_FILE_MODE,
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


def _mode(path: Path) -> int:
    """Return the permission bits (``S_IMODE``) of *path*."""
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.mark.skipif(os.name == "nt", reason="chmod perms semantics differ on windows")
def test_ensure_runtime_dir_creates_owner_only_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """:func:`ensure_runtime_dir` materialises the dir at ``0o700``."""
    target = tmp_path / "rt"
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(target))
    created = ensure_runtime_dir()
    assert created == target
    assert created.is_dir()
    assert _mode(created) == RUNTIME_DIR_MODE
    assert RUNTIME_DIR_MODE == 0o700


@pytest.mark.skipif(os.name == "nt", reason="chmod perms semantics differ on windows")
def test_ensure_runtime_dir_retightens_permissive_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing world-traversable dir is tightened back to ``0o700``."""
    target = tmp_path / "rt"
    target.mkdir()
    os.chmod(target, 0o755)
    assert _mode(target) == 0o755
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(target))
    ensure_runtime_dir()
    assert _mode(target) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="chmod perms semantics differ on windows")
def test_harden_runtime_dir_sets_owner_only(tmp_path: Path) -> None:
    """:func:`harden_runtime_dir` chmods an existing dir to ``0o700``."""
    target = tmp_path / "rt"
    target.mkdir(mode=0o777)
    harden_runtime_dir(target)
    assert _mode(target) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="chmod perms semantics differ on windows")
def test_harden_runtime_dir_missing_path_raises(tmp_path: Path) -> None:
    """Hardening a non-existent path surfaces the OS error (fail-fast)."""
    with pytest.raises(FileNotFoundError):
        harden_runtime_dir(tmp_path / "does-not-exist")


@pytest.mark.skipif(os.name == "nt", reason="UDS perms semantics differ on windows")
def test_serve_unix_socket_is_owner_only() -> None:
    """:func:`serve_unix` chmods the bound socket node to ``0o600``."""
    sock = _short_sock_path()

    async def body(_ctx: MethodContext) -> None:
        assert _mode(sock) == SOCKET_FILE_MODE
        assert SOCKET_FILE_MODE == 0o600

    _with_server(sock, body)


def _repo_root() -> Path:
    """Return the worktree root (two levels above ``tests/daemon/``)."""
    return Path(__file__).resolve().parents[2]


def _check_ignored(path: str) -> bool:
    """Return True when *path* is matched by the repo's gitignore rules."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", path],
        cwd=_repo_root(),
        check=False,
    )
    return result.returncode == 0


def test_gitignore_matches_telemetry_db() -> None:
    """``telemetry.db`` under ``.ea/`` is gitignored (PII / cwd leak guard)."""
    assert _check_ignored(".ea/telemetry.db")


def test_gitignore_matches_any_db_glob() -> None:
    """The bare ``*.db`` glob covers db artifacts written anywhere."""
    assert _check_ignored("telemetry.db")
    assert _check_ignored("scratch/probe.db")
