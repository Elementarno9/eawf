"""One socket carries the projection feed: the ruled transport adds no second.

``projection.subscribe`` is a third name on the streaming hook ``state.subscribe``
and ``event.subscribe`` already ride. It registers the same subscriber on the same
bus over the same connection handler, and differs only in what the streamer writes
per envelope. Nothing else about the transport moves, which is the whole point of
ruling it this way: a console that already holds a push stream and an always-on
mtime-poll backstop gains a projection feed without a second connection to open,
authorise, probe or reconnect.

The suite therefore proves two halves. The daemon half: one server, one bus, one
publish, and both verbs are served from the one hook, each with its own frame. The
console half: the poll backstop still refreshes a binder whose push stream dropped,
and the drop cost exactly one subscribe attempt on one socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest

import eawf.runtime.daemon.server as server_mod
from eawf.kernel.projection.compute import patches_for_event
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.envelope import Envelope
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import dispatch
from eawf.runtime.daemon.methods.state_subscribe import (
    PROJECTION_PUSH_METHOD,
    PROJECTION_SUBSCRIBE_METHOD,
    SUBSCRIBE_METHODS,
)
from eawf.runtime.daemon.server import handle_connection
from eawf.surfaces.tui.chassis.state_binding import StateBinding, StateBindingCallbacks
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    MILESTONE_URN,
    method_context,
)
from tests.integration.runtime.daemon.test_close_lock_split import _state_payload, _write_state

pytestmark = pytest.mark.integration

#: How long a frame or a refresh that is already in flight is waited for. A
#: failure guard, never the thing that orders the run.
FRAME_TIMEOUT_SECONDS = 10.0

#: The title the rewritten state file carries, so a delivered refresh is
#: distinguishable from the initial load.
REFRESHED_TITLE = "refreshed by the poll backstop"


def _socket_path() -> str:
    """Return a socket path under the AF_UNIX length ceiling (macOS: 104)."""
    return os.path.join(tempfile.gettempdir(), f"eawf-reuse-{uuid.uuid4().hex[:8]}.sock")


def _subscribe_frame(method: str) -> bytes:
    return orjson.dumps({"jsonrpc": "2.0", "id": "sub-1", "method": method, "params": {}}) + b"\n"


def _transition_envelope() -> Envelope:
    """One committed Milestone move, as the transaction publishes it."""
    return Envelope(
        id="evt-reuse-0001",
        kind=StoreKind.EVENT,
        scope_id=MILESTONE_URN,
        created_at=datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
        summary="domain.milestone.activate MLS-0030 PLANNED -> ACTIVE",
        payload={
            "entity_ref": MILESTONE_URN,
            "to_status": "ACTIVE",
            "revision_after": 2,
            "canonical_sequence": 1,
        },
    )


async def _subscribe(sock_path: str, method: str) -> tuple[asyncio.StreamReader, Any]:
    """Open one connection, subscribe with *method*, and await its ack."""
    reader, writer = await asyncio.open_unix_connection(sock_path)
    writer.write(_subscribe_frame(method))
    await writer.drain()
    ack = orjson.loads(await asyncio.wait_for(reader.readline(), timeout=FRAME_TIMEOUT_SECONDS))
    assert ack["result"]["ok"] is True, ack
    return reader, writer


async def _next_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    line = await asyncio.wait_for(reader.readline(), timeout=FRAME_TIMEOUT_SECONDS)
    return dict(orjson.loads(line))


def test_both_verbs_are_served_by_the_one_streaming_hook(tmp_path: Path) -> None:
    """One server, one bus, one publish: an envelope feed and a patch feed."""
    ctx = method_context(tmp_path / "runtime")
    ctx.bus = EventBus()
    sock_path = _socket_path()

    async def body() -> None:
        server = await asyncio.start_unix_server(
            lambda r, w: handle_connection(r, w, ctx), path=sock_path
        )
        try:
            events, event_writer = await _subscribe(sock_path, "state.subscribe")
            patches, patch_writer = await _subscribe(sock_path, PROJECTION_SUBSCRIBE_METHOD)
            assert isinstance(ctx.bus, EventBus)
            ctx.bus.publish(_transition_envelope())

            pushed, patched = await _next_frame(events), await _next_frame(patches)

            assert pushed["method"] == "event.push"
            assert pushed["params"]["event"]["id"] == "evt-reuse-0001"
            assert patched["method"] == PROJECTION_PUSH_METHOD
            assert patched["params"]["patch"]["canonical_sequence"] == 1
            for writer in (event_writer, patch_writer):
                writer.close()
                with contextlib.suppress(ConnectionResetError, BrokenPipeError):
                    await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()
            with contextlib.suppress(OSError):
                os.unlink(sock_path)

    asyncio.run(body())


def test_every_subscribe_verb_has_exactly_one_frame_builder() -> None:
    """The hook is keyed by the verb set, so no verb can reach it unrouted."""
    assert PROJECTION_SUBSCRIBE_METHOD in SUBSCRIBE_METHODS
    assert set(server_mod._SUBSCRIBE_FRAMES) == SUBSCRIBE_METHODS
    assert (
        server_mod._SUBSCRIBE_FRAMES[PROJECTION_SUBSCRIBE_METHOD] is server_mod._projection_frames
    )
    assert server_mod._SUBSCRIBE_FRAMES["state.subscribe"] is server_mod._event_frames


def test_projection_subscribe_has_no_request_response_handler(tmp_path: Path) -> None:
    """Reaching the sentinel means the verb was dispatched off the streaming hook."""
    ctx = method_context(tmp_path / "runtime")

    with pytest.raises(RuntimeError, match="streaming hook"):
        asyncio.run(dispatch(PROJECTION_SUBSCRIBE_METHOD, ctx, {}))


def test_an_unpatchable_envelope_yields_no_frame_and_keeps_the_stream() -> None:
    """One malformed transition must not end every projection subscriber's feed."""
    broken = Envelope(
        id="evt-broken",
        kind=StoreKind.EVENT,
        scope_id="P33",
        created_at=datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
        summary="an ordinal with nothing to patch",
        payload={"canonical_sequence": 4},
    )

    moved = _transition_envelope()

    assert server_mod._projection_frames(broken) == ()
    assert len(server_mod._projection_frames(moved)) == len(patches_for_event(moved))
    assert server_mod._projection_frames(moved)
    assert len(server_mod._event_frames(broken)) == 1


class _DroppingClient:
    """A daemon client whose subscribe call fails: the push stream drops.

    Attributes:
        attempts: Every method the binder called, in order, across the life of
            every client this stands in for.
    """

    def __init__(self, attempts: list[str]) -> None:
        self.attempts = attempts

    def __enter__(self) -> _DroppingClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, _params: dict[str, object]) -> dict[str, object]:
        """Record the attempt and fail it, as a dropped stream does.

        Raises:
            ConnectionError: Always.
        """
        self.attempts.append(method)
        raise ConnectionError("push stream dropped")


def _rewrite_state(state_path: Path) -> None:
    """Rewrite the state file with a distinguishable title and a later mtime.

    The mtime is advanced explicitly rather than left to the clock: the poll is
    mtime-gated, and two writes inside one filesystem tick would otherwise be
    indistinguishable on a coarse-grained mount.
    """
    payload = _state_payload()
    payload["phases"]["P30"]["title"] = REFRESHED_TITLE
    state_path.write_text(State.model_validate(payload).model_dump_json(), encoding="utf-8")
    stamp = state_path.stat().st_mtime + 10.0
    os.utime(state_path, (stamp, stamp))


def test_poll_backstop_refreshes_a_client_whose_push_stream_drops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The console keeps one socket: when its push dies, the backstop carries it."""
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    attempts: list[str] = []
    degraded: list[bool] = []

    async def body() -> None:
        delivered: asyncio.Queue[State] = asyncio.Queue()

        async def on_state(state: State) -> None:
            await delivered.put(state)

        async def on_degraded(flag: bool) -> None:
            degraded.append(flag)

        binding = StateBinding(
            state_path,
            StateBindingCallbacks(on_state=on_state, on_degraded=on_degraded),
            poll_interval_s=0.0,
            daemon_probe_interval_s=3600.0,
            daemon_failure_threshold=1,
            daemon_client_factory=lambda: _DroppingClient(attempts),
        )
        try:
            await binding.connect()
            assert binding._subscribe_task is not None
            await binding._subscribe_task

            # One socket was attempted, for the one stream the console holds.
            assert attempts == ["state.subscribe"]
            assert degraded == [True]
            initial = await asyncio.wait_for(delivered.get(), timeout=FRAME_TIMEOUT_SECONDS)
            assert initial.phases["P30"].title != REFRESHED_TITLE

            _rewrite_state(state_path)
            refreshed = await asyncio.wait_for(delivered.get(), timeout=FRAME_TIMEOUT_SECONDS)

            assert refreshed.phases["P30"].title == REFRESHED_TITLE
            assert attempts == ["state.subscribe"]
        finally:
            await binding.disconnect()

    monkeypatch.setattr(StateBinding, "_daemon_socket_available", lambda _self: True)
    asyncio.run(body())
