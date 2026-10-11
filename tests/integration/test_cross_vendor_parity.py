"""Integration: the egress proxy records a denied host at parity on every lane.

The egress proxy is the shared enforcement seam each vendor's jailed child
routes outbound through; a denied host is refused (never opens outbound)
and recorded as an ``egress-block`` event regardless of the auth lane.

Every test drives async coroutines via ``asyncio.run`` inside plain sync
``def test_`` bodies (the suite has no ``pytest-asyncio`` dep) and NEVER
touches the real network -- the outbound connector is always a fake.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.sandbox.egress_proxy import (
    SandboxEnforcementEvent,
    start_egress_proxy,
)

pytestmark = pytest.mark.integration

_SESSION_ID = "SES-executor"


@pytest.fixture
def short_socket_dir() -> Iterator[Path]:
    """Yield a short-path 0700 dir for AF_UNIX binds (macOS sun_path cap)."""
    import shutil

    socket_dir = Path(tempfile.mkdtemp(prefix="egr", dir="/tmp"))
    socket_dir.chmod(0o700)
    try:
        yield socket_dir
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


@pytest.mark.skipif(sys.platform == "win32", reason="UDS proxy is POSIX-only")
@pytest.mark.parametrize("lane", ["claude", "codex"])
def test_egress_block_recorded_per_lane(short_socket_dir: Path, lane: str) -> None:
    """A denied host is refused + recorded for every auth lane (egress parity).

    The egress proxy is the shared enforcement seam each vendor's jailed child
    routes outbound through; a denied host is refused (never opens outbound) and
    recorded as an ``egress-block`` event regardless of the auth lane.
    """
    socket_path = short_socket_dir / "egress.sock"
    recorded: list[SandboxEnforcementEvent] = []

    async def _connect(host: str, port: int) -> Any:  # pragma: no cover - never called
        raise AssertionError("denied host must never reach the network")

    async def _run() -> bytes:
        server = await start_egress_proxy(
            socket_path,
            lane=lane,
            connector=_connect,
            session=_SESSION_ID,
            sink=recorded.append,
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
    assert len(recorded) == 1
    event = recorded[0]
    assert event.kind == "egress-block"
    assert event.target == "evil.test:443"
    assert event.session == _SESSION_ID
    assert event.severity == "block"
