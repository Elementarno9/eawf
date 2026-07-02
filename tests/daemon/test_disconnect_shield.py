"""Client-disconnect shielding for in-flight mutations (P30-I23-W52).

The W48 close loop: a wave close whose gates outlast the CLI's RPC
ceiling saw the client disconnect mid-commit, the handler die with it,
and the outcome (success OR refusal) vanish into the closed socket. The
fix is twofold — the dispatch is shielded so the daemon FINISHES the
mutation after the peer leaves, and an undeliverable response is logged
instead of lost.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

import orjson
import pytest

import eawf.runtime.daemon.server as server_mod
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.runtime.daemon.server import handle_connection
from tests.daemon.test_close_lock_split import _build_ctx, _write_state

pytestmark = pytest.mark.integration


def _short_socket_path() -> str:
    """Return a socket path under the AF_UNIX length ceiling (macOS: 104)."""
    return os.path.join(tempfile.gettempdir(), f"eawf-shield-{uuid.uuid4().hex[:8]}.sock")


def _mutation_frame() -> bytes:
    mutation = Mutation(
        kind=MutationKind.ROADMAP_REVISE,
        scope_id="P30",
        mutation_id=uuid.uuid4().hex,
        params={"op": "retitle", "wave_id": "P30-I23-W99", "title": "retitled by shield test"},
    )
    frame = {
        "jsonrpc": "2.0",
        "id": "shield-1",
        "method": "state.mutate",
        "params": {"mutation": mutation.model_dump(mode="json")},
    }
    return orjson.dumps(frame) + b"\n"


def test_mutation_survives_client_disconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CR: the peer leaves mid-mutation; the daemon still lands the commit
    and the undeliverable response is logged rather than lost."""
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    ctx = _build_ctx(tmp_path, state_path)

    stalled = asyncio.Event()
    release = asyncio.Event()
    real_process = server_mod._process_frame

    async def _gated_process(line: bytes, inner_ctx: Any) -> dict[str, Any]:
        stalled.set()
        await release.wait()
        return await real_process(line, inner_ctx)

    monkeypatch.setattr(server_mod, "_process_frame", _gated_process)

    sock_path = _short_socket_path()

    async def body() -> None:
        server = await asyncio.start_unix_server(
            lambda r, w: handle_connection(r, w, ctx), path=sock_path
        )
        try:
            _reader, writer = await asyncio.open_unix_connection(sock_path)
            writer.write(_mutation_frame())
            await writer.drain()
            await asyncio.wait_for(stalled.wait(), timeout=5)

            # The client gives up mid-mutation — the W48 scenario.
            writer.close()
            await writer.wait_closed()
            release.set()

            # The shielded dispatch must still land the commit.
            for _ in range(50):
                payload = orjson.loads(state_path.read_bytes())
                if payload["waves"]["P30-I23-W99"]["title"] == "retitled by shield test":
                    return
                await asyncio.sleep(0.1)
            raise AssertionError(
                "the client disconnect killed the in-flight mutation — the shield regressed"
            )
        finally:
            server.close()
            await server.wait_closed()
            with contextlib.suppress(OSError):
                os.unlink(sock_path)

    with caplog.at_level(logging.WARNING, logger="eawf.runtime.daemon.server"):
        asyncio.run(body())

    # The response could not be delivered; it must be logged, not lost.
    assert any("undeliverable-response" in record.message for record in caplog.records)


def test_delivered_response_logs_nothing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Boundary: a connected client gets its response; no warning fires."""
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    ctx = _build_ctx(tmp_path, state_path)

    sock_path = _short_socket_path()

    async def body() -> None:
        server = await asyncio.start_unix_server(
            lambda r, w: handle_connection(r, w, ctx), path=sock_path
        )
        try:
            reader, writer = await asyncio.open_unix_connection(sock_path)
            writer.write(_mutation_frame())
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            response = orjson.loads(line)
            assert "result" in response, response
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()
            with contextlib.suppress(OSError):
                os.unlink(sock_path)

    with caplog.at_level(logging.WARNING, logger="eawf.runtime.daemon.server"):
        asyncio.run(body())
    assert not any("undeliverable-response" in record.message for record in caplog.records)
