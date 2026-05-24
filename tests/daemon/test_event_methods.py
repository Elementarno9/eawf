"""Tests for the ``event.*`` JSON-RPC handlers and the subscribe streamer.

The bounded-read handlers (``event.list``, ``event.show``) are tested
in-process via the module-level functions. The streaming path
(``event.subscribe`` / ``state.subscribe``) is exercised end-to-end
through :func:`eawf.daemon.server.handle_connection` driven by a
``socket.socketpair`` so we hit the real JSON-RPC framing code.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest
from pydantic import ValidationError

from eawf import __version__
from eawf.daemon import PROTOCOL_VERSION
from eawf.daemon.bus import EventBus
from eawf.daemon.methods import MethodContext
from eawf.daemon.methods.event import list_events, show_event
from eawf.daemon.server import handle_connection
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope

pytestmark = pytest.mark.unit


def _build_envelope(env_id: str, *, scope_id: str | None = None) -> Envelope:
    return Envelope(
        id=env_id,
        kind=StoreKind.EVENT,
        scope_id=scope_id,
        created_at=datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC),
        summary=f"summary {env_id}",
        payload={
            "timestamp": "2026-05-19T12:00:00+00:00",
            "event_type": "test",
            "actor": "test",
            "command": "noop",
            "args_hash": "0",
            "status": "ok",
            "message": "test",
        },
    )


def _write_event_jsonl(path: Path, envelopes: list[Envelope]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        for env in envelopes:
            fh.write(orjson.dumps(env.model_dump(mode="json")) + b"\n")


def _build_ctx(*, event_path: Path | None, bus: EventBus | None = None) -> MethodContext:
    return MethodContext(
        started_at="2026-05-19T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=bus,
        event_path=event_path,
    )


def _run(body: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(body())


def test_event_list_returns_all_events_within_limit(tmp_path: Path) -> None:
    event_path = tmp_path / "event.jsonl"
    _write_event_jsonl(event_path, [_build_envelope(f"E-{i}") for i in range(3)])
    ctx = _build_ctx(event_path=event_path)

    async def body() -> None:
        result = await list_events(ctx, {})
        assert [e["id"] for e in result["events"]] == ["E-0", "E-1", "E-2"]

    _run(body)


def test_event_list_paginates_by_limit(tmp_path: Path) -> None:
    event_path = tmp_path / "event.jsonl"
    _write_event_jsonl(event_path, [_build_envelope(f"E-{i}") for i in range(5)])
    ctx = _build_ctx(event_path=event_path)

    async def body() -> None:
        result = await list_events(ctx, {"limit": 2})
        assert [e["id"] for e in result["events"]] == ["E-0", "E-1"]

    _run(body)


def test_event_list_seeks_to_since(tmp_path: Path) -> None:
    event_path = tmp_path / "event.jsonl"
    _write_event_jsonl(event_path, [_build_envelope(f"E-{i}") for i in range(5)])
    ctx = _build_ctx(event_path=event_path)

    async def body() -> None:
        result = await list_events(ctx, {"since": "E-1"})
        assert [e["id"] for e in result["events"]] == ["E-2", "E-3", "E-4"]

    _run(body)


def test_event_list_stops_at_until(tmp_path: Path) -> None:
    event_path = tmp_path / "event.jsonl"
    _write_event_jsonl(event_path, [_build_envelope(f"E-{i}") for i in range(5)])
    ctx = _build_ctx(event_path=event_path)

    async def body() -> None:
        result = await list_events(ctx, {"until": "E-2"})
        assert [e["id"] for e in result["events"]] == ["E-0", "E-1", "E-2"]

    _run(body)


def test_event_list_filters_by_scope(tmp_path: Path) -> None:
    event_path = tmp_path / "event.jsonl"
    _write_event_jsonl(
        event_path,
        [
            _build_envelope("E-0", scope_id="repo:foo"),
            _build_envelope("E-1", scope_id="repo:bar"),
            _build_envelope("E-2", scope_id="repo:foo"),
        ],
    )
    ctx = _build_ctx(event_path=event_path)

    async def body() -> None:
        result = await list_events(ctx, {"scope_id": "repo:foo"})
        assert [e["id"] for e in result["events"]] == ["E-0", "E-2"]

    _run(body)


def test_event_list_rejects_unknown_field(tmp_path: Path) -> None:
    event_path = tmp_path / "event.jsonl"
    _write_event_jsonl(event_path, [_build_envelope("E-0")])
    ctx = _build_ctx(event_path=event_path)

    async def body() -> None:
        with pytest.raises(ValidationError):
            await list_events(ctx, {"unknown_field": "x"})

    _run(body)


def test_event_show_returns_match(tmp_path: Path) -> None:
    event_path = tmp_path / "event.jsonl"
    _write_event_jsonl(event_path, [_build_envelope(f"E-{i}") for i in range(3)])
    ctx = _build_ctx(event_path=event_path)

    async def body() -> None:
        result = await show_event(ctx, {"event_id": "E-1"})
        assert result["event"]["id"] == "E-1"

    _run(body)


def test_event_show_raises_for_unknown_id(tmp_path: Path) -> None:
    event_path = tmp_path / "event.jsonl"
    _write_event_jsonl(event_path, [_build_envelope("E-0")])
    ctx = _build_ctx(event_path=event_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="unknown event_id"):
            await show_event(ctx, {"event_id": "missing"})

    _run(body)


def test_event_show_requires_event_path() -> None:
    ctx = _build_ctx(event_path=None)

    async def body() -> None:
        with pytest.raises(RuntimeError, match="event_path"):
            await show_event(ctx, {"event_id": "E-1"})

    _run(body)


# ---------- end-to-end subscribe over socketpair --------------------------


pytest_subscribe_skip = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="streamer test uses POSIX socket.socketpair",
)


async def _open_stream_from_socket(
    sock: socket.socket,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Wrap a connected socket as an asyncio StreamReader/Writer pair."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
    transport, _ = await loop.connect_accepted_socket(lambda: protocol, sock)
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    return reader, writer


@pytest_subscribe_skip
def test_subscribe_streamer_pushes_published_event(tmp_path: Path) -> None:
    """End-to-end: subscribe, publish via the bus, receive ``event.push``."""
    bus = EventBus()
    event_path = tmp_path / "event.jsonl"
    ctx = _build_ctx(event_path=event_path, bus=bus)

    async def body() -> None:
        server_sock, client_sock = socket.socketpair()
        try:
            client_sock.setblocking(False)
            server_sock.setblocking(False)
            server_reader, server_writer = await _open_stream_from_socket(server_sock)
            client_reader, client_writer = await _open_stream_from_socket(client_sock)
            handler = asyncio.create_task(handle_connection(server_reader, server_writer, ctx))
            # Send subscribe request.
            request = {
                "jsonrpc": "2.0",
                "id": "sub-1",
                "method": "event.subscribe",
                "params": {},
            }
            client_writer.write(orjson.dumps(request) + b"\n")
            await client_writer.drain()
            # Expect ack frame.
            ack_line = await asyncio.wait_for(client_reader.readline(), timeout=2.0)
            ack = orjson.loads(ack_line)
            assert ack["id"] == "sub-1"
            assert ack["result"] == {"ok": True, "backlog_count": 0}
            assert bus.active_subscriptions == 1
            # Publish via the bus.
            envelope = _build_envelope("E-live")
            bus.publish(envelope)
            push_line = await asyncio.wait_for(client_reader.readline(), timeout=2.0)
            push = orjson.loads(push_line)
            assert push["method"] == "event.push"
            assert push["params"]["event"]["id"] == "E-live"
            # Tear down: close the client side; handler unregisters.
            client_writer.close()
            with contextlib.suppress(Exception):
                await client_writer.wait_closed()
            await asyncio.wait_for(handler, timeout=2.0)
            assert bus.active_subscriptions == 0
        finally:
            with contextlib.suppress(OSError):
                server_sock.close()
            with contextlib.suppress(OSError):
                client_sock.close()

    _run(body)


@pytest_subscribe_skip
def test_subscribe_streamer_replays_backlog(tmp_path: Path) -> None:
    """Subscriber with ``since_event_id`` receives catch-up before live."""
    bus = EventBus()
    event_path = tmp_path / "event.jsonl"
    _write_event_jsonl(event_path, [_build_envelope(f"E-{i}") for i in range(4)])
    ctx = _build_ctx(event_path=event_path, bus=bus)

    async def body() -> None:
        server_sock, client_sock = socket.socketpair()
        try:
            client_sock.setblocking(False)
            server_sock.setblocking(False)
            server_reader, server_writer = await _open_stream_from_socket(server_sock)
            client_reader, client_writer = await _open_stream_from_socket(client_sock)
            handler = asyncio.create_task(handle_connection(server_reader, server_writer, ctx))
            request = {
                "jsonrpc": "2.0",
                "id": "sub-1",
                "method": "event.subscribe",
                "params": {"since_event_id": "E-1"},
            }
            client_writer.write(orjson.dumps(request) + b"\n")
            await client_writer.drain()
            ack_line = await asyncio.wait_for(client_reader.readline(), timeout=2.0)
            ack = orjson.loads(ack_line)
            assert ack["result"] == {"ok": True, "backlog_count": 2}
            # Backlog: E-2 and E-3.
            ids: list[str] = []
            for _ in range(2):
                push_line = await asyncio.wait_for(client_reader.readline(), timeout=2.0)
                push = orjson.loads(push_line)
                assert push["method"] == "event.push"
                ids.append(push["params"]["event"]["id"])
            assert ids == ["E-2", "E-3"]
            # Tear down.
            client_writer.close()
            with contextlib.suppress(Exception):
                await client_writer.wait_closed()
            await asyncio.wait_for(handler, timeout=2.0)
        finally:
            with contextlib.suppress(OSError):
                server_sock.close()
            with contextlib.suppress(OSError):
                client_sock.close()

    _run(body)


@pytest_subscribe_skip
def test_state_subscribe_alias_routes_to_streamer(tmp_path: Path) -> None:
    """``state.subscribe`` is a synonym for ``event.subscribe`` (C02 §5.3.1)."""
    bus = EventBus()
    event_path = tmp_path / "event.jsonl"
    ctx = _build_ctx(event_path=event_path, bus=bus)

    async def body() -> None:
        server_sock, client_sock = socket.socketpair()
        try:
            client_sock.setblocking(False)
            server_sock.setblocking(False)
            server_reader, server_writer = await _open_stream_from_socket(server_sock)
            client_reader, client_writer = await _open_stream_from_socket(client_sock)
            handler = asyncio.create_task(handle_connection(server_reader, server_writer, ctx))
            request = {
                "jsonrpc": "2.0",
                "id": "sub-1",
                "method": "state.subscribe",
                "params": {},
            }
            client_writer.write(orjson.dumps(request) + b"\n")
            await client_writer.drain()
            ack_line = await asyncio.wait_for(client_reader.readline(), timeout=2.0)
            ack = orjson.loads(ack_line)
            assert ack["id"] == "sub-1"
            assert "result" in ack
            assert bus.active_subscriptions == 1
            client_writer.close()
            with contextlib.suppress(Exception):
                await client_writer.wait_closed()
            await asyncio.wait_for(handler, timeout=2.0)
        finally:
            with contextlib.suppress(OSError):
                server_sock.close()
            with contextlib.suppress(OSError):
                client_sock.close()

    _run(body)


def test_subscribe_sentinel_handler_raises_when_dispatched_directly() -> None:
    """The non-streaming dispatch path must not silently accept subscribe."""
    from eawf.daemon.methods.state_subscribe import (
        event_subscribe,
        state_subscribe,
    )

    ctx = _build_ctx(event_path=None, bus=EventBus())

    async def body() -> None:
        with pytest.raises(RuntimeError, match="streaming hook"):
            await state_subscribe(ctx, {})
        with pytest.raises(RuntimeError, match="streaming hook"):
            await event_subscribe(ctx, {})

    _run(body)
