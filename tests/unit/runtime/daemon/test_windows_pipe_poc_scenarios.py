"""WIN-P6 Windows-only PoC verification set with per-test pipe isolation.

The five PoC scenarios the Windows named-pipe transport must satisfy,
each as a self-contained win32-marked test:

1. **RPC round-trip** -- ping / status / state.mutate-shaped request over
   the per-user pipe via ``pipe_client_call``.
2. **Subscribe streaming** -- a live ``event.push`` over a kept-open pipe
   for the bound scope_id.
3. **Teardown unblock** -- a blocked streaming read returns promptly after
   ``cancel_pending_read`` (CancelIoEx).
4. **Idle reap** -- a subscription whose client vanishes is reaped off the
   bus (``active_subscriptions`` drops) via the PeekNamedPipe heartbeat.
5. **Cold-start budget** -- a freshly spawned daemon answers a pipe ping
   within the pinned 20 s readiness budget.

Isolation: each test binds a UNIQUE pipe name and a per-test
``EAWF_RUNTIME_DIR`` so the per-user singleton pipe + runtime dir of one
test never collide with another's (the default-name daemon is a
singleton; parallel default-name daemons are forbidden).

The whole module skips cleanly on POSIX (``sys.platform != 'win32'``).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import orjson
import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only PoC scenarios")

#: Pinned cold-start readiness budget (seconds). A freshly spawned win32
#: daemon must answer a pipe ping within this window; tuned generously
#: above the POSIX 5 s default because a Windows cold process start +
#: pythonw spawn + pipe bind is slower than a UDS bind.
COLD_START_READINESS_BUDGET_SECONDS: float = 20.0


def _unique_pipe_name(stem: str) -> str:
    return rf"\\.\pipe\eawfd-poc-{stem}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def isolated_runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test ``EAWF_RUNTIME_DIR`` so cold-start tests never collide.

    The per-user pipe + runtime dir are singletons; pinning a unique
    runtime dir per test keeps a cold-spawn from attaching to another
    test's daemon or PID file.
    """
    rt = tmp_path / f"eawfd-{uuid.uuid4().hex[:8]}"
    rt.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(rt))
    return rt


def _build_ctx(bus: Any | None = None) -> Any:
    from eawf import __version__
    from eawf.runtime.daemon import PROTOCOL_VERSION
    from eawf.runtime.daemon.methods import MethodContext

    ctx = MethodContext(
        started_at="2026-06-12T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
    )
    if bus is not None:
        ctx.bus = bus
        ctx.event_path = None
    return ctx


def _event_envelope(scope_id: str) -> Any:
    from datetime import UTC, datetime

    from eawf.kernel.state.enums import StoreKind
    from eawf.kernel.store.envelope import Envelope
    from eawf.kernel.store.kinds.event import EventPayload

    now = datetime.now(UTC)
    return Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=scope_id,
        created_at=now,
        updated_at=None,
        summary="poc",
        payload=EventPayload(
            timestamp=now,
            event_type="wave.close",
            actor="test",
            command="wave close",
            args_hash="",
            status="ok",
            message="poc",
        ).model_dump(mode="json"),
        blob_refs=[],
        artifact_ids=[],
    )


def test_poc1_rpc_round_trip_isolated_pipe() -> None:
    """Scenario 1: a request/reply RPC round-trips over an isolated pipe."""
    from eawf.runtime.daemon.server import process_frame_bytes
    from eawf.runtime.daemon.windows_pipe import WindowsPipeServer, pipe_client_call

    pipe_name = _unique_pipe_name("rpc")
    ctx = _build_ctx()
    result: dict[str, Any] = {}

    async def runner() -> None:
        loop = asyncio.get_running_loop()

        async def handler(payload: bytes) -> bytes:
            return await process_frame_bytes(payload, ctx)

        server = WindowsPipeServer(loop, handler, pipe_name=pipe_name, verify_sid_enabled=False)
        server.start()
        try:
            req = orjson.dumps({"jsonrpc": "2.0", "id": "1", "method": "daemon.ping", "params": {}})
            raw = await asyncio.to_thread(pipe_client_call, pipe_name, req + b"\n")
            result["payload"] = orjson.loads(raw.rstrip(b"\n"))
        finally:
            server.stop()
            await asyncio.sleep(0.05)

    asyncio.run(runner())
    assert result["payload"]["result"]["pid"] == os.getpid()


def test_poc2_subscribe_streaming_isolated_pipe() -> None:
    """Scenario 2: a subscription receives a live event.push for the scope."""
    from eawf.runtime.daemon.bus import EventBus
    from eawf.runtime.daemon.server import process_frame_bytes
    from eawf.runtime.daemon.windows_pipe import (
        PipeReader,
        WindowsPipeServer,
        close_pipe,
        make_bus_subscribe_router,
        open_subscription_pipe,
    )

    pipe_name = _unique_pipe_name("sub")
    scope_id = "EAWF:wave:P30-I19-W06"
    bus = EventBus()
    result: dict[str, Any] = {}

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        ctx = _build_ctx(bus)

        async def handler(payload: bytes) -> bytes:
            return await process_frame_bytes(payload, ctx)

        server = WindowsPipeServer(
            loop,
            handler,
            pipe_name=pipe_name,
            verify_sid_enabled=False,
            subscribe_router=make_bus_subscribe_router(loop, ctx),
        )
        server.start()

        def _sub_and_read() -> dict[str, Any]:
            req = orjson.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "1",
                    "method": "state.subscribe",
                    "params": {"kinds": ["event"], "scope_id": scope_id},
                }
            )
            handle = open_subscription_pipe(pipe_name, req + b"\n", wait_ms=5000)
            reader = PipeReader(handle)
            reader.read_message()  # ack
            loop.call_soon_threadsafe(bus.publish, _event_envelope(scope_id))
            push = orjson.loads(reader.read_message().rstrip(b"\n"))
            close_pipe(handle)
            return push

        try:
            result["push"] = await asyncio.to_thread(_sub_and_read)
        finally:
            server.stop()
            await asyncio.sleep(0.1)

    asyncio.run(runner())
    assert result["push"]["method"] == "event.push"
    assert result["push"]["params"]["event"]["scope_id"] == scope_id


def test_poc3_teardown_unblock_isolated_pipe() -> None:
    """Scenario 3: cancel_pending_read unblocks a parked streaming read."""
    from eawf.runtime.daemon.bus import EventBus
    from eawf.runtime.daemon.server import process_frame_bytes
    from eawf.runtime.daemon.windows_pipe import (
        PipeReader,
        WindowsPipeServer,
        cancel_pending_read,
        close_pipe,
        make_bus_subscribe_router,
        open_subscription_pipe,
    )

    pipe_name = _unique_pipe_name("teardown")
    state: dict[str, Any] = {}
    done = threading.Event()

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        ctx = _build_ctx(EventBus())

        async def handler(payload: bytes) -> bytes:
            return await process_frame_bytes(payload, ctx)

        server = WindowsPipeServer(
            loop,
            handler,
            pipe_name=pipe_name,
            verify_sid_enabled=False,
            subscribe_router=make_bus_subscribe_router(loop, ctx),
        )
        server.start()

        def _open_and_block() -> None:
            req = orjson.dumps(
                {"jsonrpc": "2.0", "id": "1", "method": "state.subscribe", "params": {}}
            )
            handle = open_subscription_pipe(pipe_name, req + b"\n", wait_ms=5000)
            state["handle"] = handle
            reader = PipeReader(handle)
            reader.read_message()  # ack
            try:
                reader.read_message()  # blocks until cancelled
            except Exception:
                state["cancelled"] = True
            finally:
                close_pipe(handle)
                done.set()

        worker = threading.Thread(target=_open_and_block, daemon=True)
        worker.start()
        try:
            await asyncio.sleep(0.3)
            assert "handle" in state
            cancel_pending_read(state["handle"])
            state["unblocked"] = await asyncio.to_thread(done.wait, 2.0)
        finally:
            server.stop()
            await asyncio.sleep(0.1)

    asyncio.run(runner())
    assert state.get("unblocked") is True


def test_poc4_idle_reap_drops_active_subscription() -> None:
    """Scenario 4: a vanished client is reaped off the bus.

    The client opens a subscription, reads the ack, then closes its pipe
    handle WITHOUT a clean unsubscribe. The streamer's PeekNamedPipe
    heartbeat detects the broken pipe and unregisters the subscriber, so
    ``active_subscriptions`` returns to 0.
    """
    from eawf.runtime.daemon.bus import EventBus
    from eawf.runtime.daemon.server import process_frame_bytes
    from eawf.runtime.daemon.windows_pipe import (
        PipeReader,
        WindowsPipeServer,
        close_pipe,
        make_bus_subscribe_router,
        open_subscription_pipe,
    )

    pipe_name = _unique_pipe_name("reap")
    bus = EventBus()
    result: dict[str, Any] = {}

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        ctx = _build_ctx(bus)

        async def handler(payload: bytes) -> bytes:
            return await process_frame_bytes(payload, ctx)

        server = WindowsPipeServer(
            loop,
            handler,
            pipe_name=pipe_name,
            verify_sid_enabled=False,
            subscribe_router=make_bus_subscribe_router(loop, ctx),
        )
        server.start()

        def _sub_then_vanish() -> None:
            req = orjson.dumps(
                {"jsonrpc": "2.0", "id": "1", "method": "state.subscribe", "params": {}}
            )
            handle = open_subscription_pipe(pipe_name, req + b"\n", wait_ms=5000)
            PipeReader(handle).read_message()  # ack
            close_pipe(handle)  # vanish without unsubscribe

        try:
            await asyncio.to_thread(_sub_then_vanish)
            # Wait past the heartbeat so the reap can fire.
            from eawf.runtime.daemon.windows_pipe import _IDLE_HEARTBEAT_SECONDS

            deadline = time.monotonic() + _IDLE_HEARTBEAT_SECONDS * 2 + 2.0
            while bus.active_subscriptions > 0 and time.monotonic() < deadline:
                await asyncio.sleep(0.2)
            result["active"] = bus.active_subscriptions
        finally:
            server.stop()
            await asyncio.sleep(0.1)

    asyncio.run(runner())
    assert result["active"] == 0


def test_poc5_cold_start_within_readiness_budget(isolated_runtime_dir: Path) -> None:
    """Scenario 5: a cold-spawned daemon answers a ping within 20 s.

    Cold-spawns the daemon into the isolated runtime dir, then opens a
    real pipe ping via DaemonClient. The whole readiness wait is bounded
    by ``COLD_START_READINESS_BUDGET_SECONDS`` so a regression that
    stalls the win32 bind reds the test instead of hanging CI.
    """
    from eawf.surfaces.cli._daemon_client import DaemonClient

    start = time.monotonic()
    with DaemonClient(
        runtime_dir=isolated_runtime_dir,
        call_timeout_seconds=COLD_START_READINESS_BUDGET_SECONDS,
    ) as client:
        result = client.call("daemon.ping")
        elapsed = time.monotonic() - start
        with contextlib.suppress(Exception):
            client.call("daemon.shutdown", {"drain": False, "timeout_seconds": 5})
    assert result["pid"] > 0
    assert elapsed <= COLD_START_READINESS_BUDGET_SECONDS, (
        f"cold start took {elapsed:.1f}s, over the {COLD_START_READINESS_BUDGET_SECONDS}s budget"
    )
