"""Unit tests for :mod:`eawf.runtime.sandbox.egress_proxy` (P29-I03-W02).

Pin the UDS egress-proxy enforcement seam:

- the Windows gap is surfaced honestly (``egress_proxy_supported`` is
  ``False`` and :func:`start_egress_proxy` raises the typed
  :class:`EgressUnavailableOnWindowsError`) rather than crashing on import;
- a DENIED host is refused WITHOUT opening any outbound connection (the
  injected connector is never called);
- an ALLOWED host opens outbound and tunnels bytes both ways;
- the socket-dir permission guard refuses a world-accessible directory.

Every async test drives the coroutine via ``asyncio.run`` inside a plain
sync ``def test_`` -- the suite has no ``pytest-asyncio`` dependency and
runs under ``--strict-markers`` (matching test_env_scrub.py). The
outbound-connect is ALWAYS a fake, so no real network is ever touched.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from eawf.runtime.sandbox import egress_proxy
from eawf.runtime.sandbox.egress_proxy import (
    EgressUnavailableOnWindowsError,
    egress_proxy_supported,
    handle_egress_connection,
    start_egress_proxy,
)

_CLAUDE: str = "claude"


@pytest.fixture
def short_socket_dir() -> Iterator[Path]:
    """Yield a short-path 0700 dir for AF_UNIX binds.

    The pytest ``tmp_path`` can exceed the ~104-char ``sun_path`` limit on
    macOS, so a real UDS bind under it raises ``OSError: AF_UNIX path too
    long``. A short ``/tmp`` dir keeps the socket path under the limit.
    """
    socket_dir = Path(tempfile.mkdtemp(prefix="egr", dir="/tmp"))
    socket_dir.chmod(0o700)
    try:
        yield socket_dir
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Fakes: an in-memory client stream pair + a recording outbound connector
# ---------------------------------------------------------------------------


class _FakeWriter:
    """A minimal :class:`asyncio.StreamWriter` stand-in.

    Records everything written, exposes a ``closed`` flag, and satisfies
    the proxy's ``write`` / ``drain`` / ``close`` / ``is_closing`` calls.
    """

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed


class _FakeReader:
    """A reader that yields one queued request line then EOF."""

    def __init__(self, line: bytes = b"") -> None:
        self._line = line
        self._drained = False

    async def readline(self) -> bytes:
        if self._drained:
            return b""
        self._drained = True
        return self._line

    async def read(self, _n: int = -1) -> bytes:
        return b""


def _connector_spy() -> tuple[egress_proxy.OutboundConnector, list[tuple[str, int]]]:
    """Return a fake outbound connector plus the list it records into.

    The connector records each ``(host, port)`` it is asked to dial and
    returns an empty in-memory upstream pair. If the proxy ever calls it
    for a denied host, the recorded list makes the leak visible.
    """
    calls: list[tuple[str, int]] = []

    async def _connect(host: str, port: int) -> tuple[_FakeReader, _FakeWriter]:
        calls.append((host, port))
        return _FakeReader(), _FakeWriter()

    return _connect, calls  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Windows gap: honest typed signal, no crash
# ---------------------------------------------------------------------------


def test_egress_proxy_supported_true_on_posix() -> None:
    """The proxy is supported on the POSIX CI host this suite runs on."""
    assert egress_proxy_supported() is True


def test_egress_proxy_supported_false_on_windows_os_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``os.name == 'nt'`` flips the predicate to ``False``."""
    monkeypatch.setattr(egress_proxy.os, "name", "nt")
    assert egress_proxy_supported() is False


def test_egress_proxy_supported_false_on_windows_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sys.platform == 'win32'`` flips the predicate to ``False``."""
    monkeypatch.setattr(egress_proxy.sys, "platform", "win32")
    assert egress_proxy_supported() is False


def test_start_egress_proxy_raises_typed_error_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On Windows the proxy raises the typed gap signal, not a crash.

    The guard fires synchronously before any ``await``, so the coroutine
    is stepped directly via ``send(None)`` rather than ``asyncio.run`` --
    spinning an event loop under a faked ``sys.platform == 'win32'`` would
    itself crash importing the Windows event-loop policy, masking the
    behaviour under test.
    """
    monkeypatch.setattr(egress_proxy.os, "name", "nt")
    monkeypatch.setattr(egress_proxy.sys, "platform", "win32")
    socket_path = tmp_path / "egress.sock"

    coro = start_egress_proxy(socket_path, lane=_CLAUDE)
    try:
        with pytest.raises(EgressUnavailableOnWindowsError, match="windows"):
            coro.send(None)
    finally:
        coro.close()


# ---------------------------------------------------------------------------
# Per-connection enforcement: deny never opens outbound; allow tunnels
# ---------------------------------------------------------------------------


def test_handle_connection_denied_host_never_opens_outbound() -> None:
    """A denied host is refused WITHOUT calling the outbound connector."""
    connector, calls = _connector_spy()
    reader = _FakeReader(b"CONNECT evil.test:443\n")
    writer = _FakeWriter()

    decision = asyncio.run(
        handle_egress_connection(
            reader,  # type: ignore[arg-type]
            writer,  # type: ignore[arg-type]
            lane=_CLAUDE,
            connector=connector,
        )
    )

    assert decision.allowed is False
    assert decision.reason == "default-deny"
    assert calls == []  # outbound NEVER opened for a denied host
    assert bytes(writer.buffer).startswith(b"DENY")
    assert writer.closed is True


def test_handle_connection_null_byte_host_never_opens_outbound() -> None:
    """A null-byte canonicalization-bypass host is refused, no outbound."""
    connector, calls = _connector_spy()
    reader = _FakeReader(b"CONNECT api.anthropic.com\x00.evil.com:443\n")
    writer = _FakeWriter()

    decision = asyncio.run(
        handle_egress_connection(
            reader,  # type: ignore[arg-type]
            writer,  # type: ignore[arg-type]
            lane=_CLAUDE,
            connector=connector,
        )
    )

    assert decision.allowed is False
    assert decision.reason == "non-dns-char"
    assert calls == []


def test_handle_connection_malformed_request_never_opens_outbound() -> None:
    """A request that is not ``CONNECT host:port`` is refused, no outbound."""
    connector, calls = _connector_spy()
    reader = _FakeReader(b"GARBAGE\n")
    writer = _FakeWriter()

    decision = asyncio.run(
        handle_egress_connection(
            reader,  # type: ignore[arg-type]
            writer,  # type: ignore[arg-type]
            lane=_CLAUDE,
            connector=connector,
        )
    )

    assert decision.allowed is False
    assert decision.reason == "malformed-request"
    assert calls == []
    assert bytes(writer.buffer).startswith(b"DENY")


def test_handle_connection_allowed_host_opens_outbound_and_tunnels() -> None:
    """An allowed host opens outbound exactly once and writes OK first."""
    calls: list[tuple[str, int]] = []
    upstream_payload = b"hello-from-upstream"

    async def _connect(host: str, port: int) -> tuple[_FakeReader, _FakeWriter]:
        calls.append((host, port))
        return _FakeReader(upstream_payload), _FakeWriter()

    reader = _FakeReader(b"CONNECT api.anthropic.com:443\n")
    writer = _FakeWriter()

    decision = asyncio.run(
        handle_egress_connection(
            reader,  # type: ignore[arg-type]
            writer,  # type: ignore[arg-type]
            lane=_CLAUDE,
            connector=_connect,  # type: ignore[arg-type]
        )
    )

    assert decision.allowed is True
    assert calls == [("api.anthropic.com", 443)]
    # The status line precedes the tunnelled upstream bytes.
    assert bytes(writer.buffer).startswith(b"OK\n")


# ---------------------------------------------------------------------------
# Live UDS bind: a real socket, a real client, a DENIED host, no outbound
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="UDS proxy is POSIX-only")
def test_live_uds_proxy_refuses_denied_host_without_outbound(short_socket_dir: Path) -> None:
    """Bind a real UDS, connect a real client, request a DENIED host.

    Asserts the proxy replies ``DENY`` and never invokes the (faked)
    outbound connector -- the end-to-end enforcement seam W04 wires into.
    """
    socket_path = short_socket_dir / "egress.sock"
    connector, calls = _connector_spy()

    async def _run() -> bytes:
        server = await start_egress_proxy(
            socket_path,
            lane=_CLAUDE,
            connector=connector,
        )
        try:
            reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
            writer.write(b"CONNECT evil.test:443\n")
            await writer.drain()
            status = await reader.readline()
            writer.close()
            return status
        finally:
            server.close()
            await server.wait_closed()

    status = asyncio.run(_run())
    assert status.startswith(b"DENY")
    assert calls == []  # denied host never reached the network


@pytest.mark.skipif(sys.platform == "win32", reason="UDS proxy is POSIX-only")
def test_live_uds_proxy_allows_listed_host_via_fake_outbound(short_socket_dir: Path) -> None:
    """An allowed host gets ``OK`` and the faked outbound is dialled once."""
    socket_path = short_socket_dir / "egress.sock"
    calls: list[tuple[str, int]] = []

    async def _connect(host: str, port: int) -> tuple[_FakeReader, _FakeWriter]:
        calls.append((host, port))
        return _FakeReader(b""), _FakeWriter()

    async def _run() -> bytes:
        server = await start_egress_proxy(
            socket_path,
            lane=_CLAUDE,
            connector=_connect,  # type: ignore[arg-type]
        )
        try:
            reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
            writer.write(b"CONNECT api.anthropic.com:443\n")
            await writer.drain()
            status = await reader.readline()
            writer.close()
            return status
        finally:
            server.close()
            await server.wait_closed()

    status = asyncio.run(_run())
    assert status.startswith(b"OK")
    assert calls == [("api.anthropic.com", 443)]


# ---------------------------------------------------------------------------
# Socket-dir guards: containment + permission
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="UDS proxy is POSIX-only")
def test_start_egress_proxy_rejects_world_accessible_socket_dir(tmp_path: Path) -> None:
    """A socket dir more permissive than 0700 is refused."""
    socket_dir = tmp_path / "rt"
    socket_dir.mkdir(mode=0o755)
    # mkdir mode is umask-masked; force the world bits on explicitly.
    socket_dir.chmod(0o755)
    socket_path = socket_dir / "egress.sock"

    async def _run() -> None:
        await start_egress_proxy(socket_path, lane=_CLAUDE)

    with pytest.raises(PermissionError, match="more permissive"):
        asyncio.run(_run())
