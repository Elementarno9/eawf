"""A stopping daemon exits promptly whatever its clients leave open.

Each test serves the daemon's real listener on a short socket path, opens the
connections a working session leaves behind -- an idle client and an event
subscription -- and then stops it, by ``daemon.shutdown`` or the way SIGTERM
does. The listener has to return within a bound: a client that never hangs up
must not keep the process, its lock and its socket alive.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
import weakref
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import orjson
import pytest

from eawf import __version__
from eawf.runtime.daemon import PROTOCOL_VERSION, main, run_supervisor
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.run_supervisor import RunSupervisor

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="the listener under test is the UDS one"),
]

#: How long a stop may take to return with nothing in flight.
EXIT_BOUND_SECONDS: Final = 3.0


@pytest.fixture
def sock_path() -> Iterator[Path]:
    """Return a socket path short enough for AF_UNIX, removed afterwards."""
    path = Path(tempfile.gettempdir()) / f"eawfd-{uuid.uuid4().hex[:8]}.sock"
    yield path
    path.unlink(missing_ok=True)


def _ctx() -> MethodContext:
    return MethodContext(
        started_at="2026-10-09T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
    )


def _frame(method: str, params: dict[str, Any], request_id: str) -> bytes:
    request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    return orjson.dumps(request) + b"\n"


async def _connect(sock_path: Path) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    for _ in range(200):
        if sock_path.exists():
            try:
                return await asyncio.open_unix_connection(str(sock_path))
            except OSError:
                pass
        await asyncio.sleep(0.01)
    raise AssertionError("the daemon never bound its socket")


async def _leave_clients_open(sock_path: Path) -> list[asyncio.StreamWriter]:
    """Open an idle client and an event subscription, as a working session leaves them."""
    _idle_reader, idle = await _connect(sock_path)
    sub_reader, sub = await _connect(sock_path)
    sub.write(_frame("event.subscribe", {}, "sub"))
    await sub.drain()
    await sub_reader.readline()
    return [idle, sub]


def _serve_then(
    sock_path: Path, ctx: MethodContext, stop: Any, *, clients: bool
) -> tuple[float, Any]:
    """Serve, optionally leave clients open, run *stop*, and time the listener's return."""

    async def scenario() -> tuple[float, Any]:
        server = asyncio.create_task(main._run_server(sock_path, ctx, expected_uid=None))
        writers = await _leave_clients_open(sock_path) if clients else []
        loop = asyncio.get_running_loop()
        started = loop.time()
        answer = await stop()
        try:
            await asyncio.wait_for(server, timeout=EXIT_BOUND_SECONDS + 2.0)
        except TimeoutError:
            return float("inf"), answer
        finally:
            for writer in writers:
                writer.close()
        return loop.time() - started, answer

    return asyncio.run(scenario())


def _shutdown_call(sock_path: Path) -> Any:
    async def stop() -> dict[str, Any]:
        reader, writer = await _connect(sock_path)
        writer.write(_frame("daemon.shutdown", {"drain": True, "timeout_seconds": 30}, "stop"))
        await writer.drain()
        line = await reader.readline()
        writer.close()
        return dict(orjson.loads(line))

    return stop


def test_a_stop_with_nothing_in_flight_exits_at_once(sock_path: Path) -> None:
    took, answer = _serve_then(sock_path, _ctx(), _shutdown_call(sock_path), clients=False)

    assert answer["result"]["drained"] is True
    assert took < EXIT_BOUND_SECONDS


def test_a_stop_exits_though_clients_stay_connected(sock_path: Path) -> None:
    took, answer = _serve_then(sock_path, _ctx(), _shutdown_call(sock_path), clients=True)

    assert answer["result"]["drained"] is True
    assert took < EXIT_BOUND_SECONDS


def test_a_signal_exits_though_clients_stay_connected(sock_path: Path) -> None:
    ctx = _ctx()

    async def stop() -> None:
        assert isinstance(ctx.shutdown_event, asyncio.Event)
        ctx.shutdown_event.set()

    took, _answer = _serve_then(sock_path, ctx, stop, clients=True)

    assert took < EXIT_BOUND_SECONDS


class _Root:
    """A stand-in for a root context, which the supervisor registry keys weakly."""


async def _parked_turn() -> None:
    """Stand in for a supervised turn that never ends on its own."""
    await asyncio.Event().wait()


def test_a_signal_during_a_stop_drain_exits_within_the_grace(
    sock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx()
    root = _Root()
    supervisors: weakref.WeakKeyDictionary[Any, RunSupervisor] = weakref.WeakKeyDictionary()
    monkeypatch.setattr(run_supervisor, "_SUPERVISORS", supervisors)
    supervisor = supervisors[root] = RunSupervisor()

    async def stop() -> None:
        supervisor.supervise("RUN-parked", _parked_turn())
        _reader, writer = await _connect(sock_path)
        writer.write(_frame("daemon.shutdown", {"drain": True, "timeout_seconds": 30}, "stop"))
        await writer.drain()
        await asyncio.sleep(0.3)
        assert isinstance(ctx.shutdown_event, asyncio.Event)
        assert not ctx.shutdown_event.is_set(), "the drain should still be waiting"
        ctx.shutdown_event.set()

    took, _answer = _serve_then(sock_path, ctx, stop, clients=False)

    assert took < EXIT_BOUND_SECONDS
