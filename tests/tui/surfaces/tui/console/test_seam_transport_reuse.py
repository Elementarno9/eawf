"""The console seam rides the binder it was given, and moves nothing that was on it.

The seam owns no transport. It holds one
:class:`~eawf.surfaces.tui.chassis.state_binding.StateBinding` and uses its socket push, its
always-on poll backstop, its resume cursor and its client for request/response calls.
A second socket would be a second thing to authorise, probe, throttle and reconnect,
and the console would then hold two answers to "am I live" that could disagree.

The other half of the claim matters as much: the epoch-1 feed was already on this
binder, and it must be exactly where it was. So the suite drives a default-constructed
binder beside the seam's and pins that it still subscribes with the epoch-1 verb, still
refreshes state from an ``event.push`` frame, still advances the event resume cursor
and never advances the projection one -- and that a patch frame arriving at it is
dropped rather than delivered anywhere.

Nothing here waits on a clock. Push frames are handed to the decoder directly, the
subscribe task is awaited rather than slept past, and the one file the poll backstop
watches has its mtime advanced explicitly so two writes inside one filesystem tick
cannot be mistaken for one.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.kernel.projection.compute import KeyedPatch, RouteProjection, build_route_projection
from eawf.kernel.projection.connection import ConnectionValue
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.runtime.daemon.methods.state_subscribe import (
    PROJECTION_PUSH_METHOD,
    PROJECTION_SUBSCRIBE_METHOD,
)
from eawf.surfaces.tui.chassis.state_binding import (
    DEFAULT_SUBSCRIBE_METHOD,
    StateBinding,
    StateBindingCallbacks,
)
from eawf.surfaces.tui.console.seam import ProjectionSeam
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import MILESTONE_URN
from tests.integration.runtime.daemon.test_close_lock_split import _state_payload, _write_state

#: How long a delivery that is already in flight is waited for. A failure guard,
#: never the thing that orders the run.
DELIVERY_TIMEOUT_SECONDS = 10.0

#: The route the suite binds, and the collection key its rows live under.
ROUTE = "roadmap"

#: The title the rewritten state file carries, so a backstop delivery is
#: distinguishable from the initial load.
REFRESHED_TITLE = "refreshed by the poll backstop"

#: When a rebuilt projection is stamped. The digest does not cover the stamp, so a
#: fixed clock only keeps the suite's own output reproducible.
AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


class _RecordingClient:
    """A client that records every method and drops the subscribe stream.

    Attributes:
        calls: Every method asked over every client this factory produced, in order.
    """

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def __enter__(self) -> _RecordingClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, _params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record the method; fail a subscribe and answer anything else.

        Raises:
            ConnectionError: The call is a subscribe, which stands in for a push
                stream the daemon dropped.
        """
        self.calls.append(method)
        if method.endswith(".subscribe"):
            raise ConnectionError("push stream dropped")
        return {"ok": True}


def _projection(cursor: int, *, status: str = "PLANNED", revision: int = 1) -> RouteProjection:
    """Return one route projection over a single Milestone row."""
    return build_route_projection(
        route=ROUTE,
        document={
            "milestone": {
                "MLS-0030": {"urn": MILESTONE_URN, "revision": revision, "status": status},
            }
        },
        cursor=cursor,
        scope_id="EAWF",
        generated_at=AT,
    )


def _patch_frame(sequence: int) -> bytes:
    """Serialize one ``projection.patch`` notification, as the daemon writes it."""
    return orjson.dumps(
        {
            "jsonrpc": "2.0",
            "method": PROJECTION_PUSH_METHOD,
            "params": {
                "patch": {
                    "schema_version": "1.0",
                    "projection_kind": "roadmap_view",
                    "routes": [ROUTE],
                    "scope_id": "EAWF",
                    "canonical_sequence": sequence,
                    "entries": [
                        {
                            "key": "MLS-0030",
                            "urn": MILESTONE_URN,
                            "collection": "milestone",
                            "revision": 2,
                            "status": "ACTIVE",
                        }
                    ],
                }
            },
        }
    )


def _event_frame(event_id: str) -> bytes:
    """Serialize one ``event.push`` frame, as the epoch-1 feed receives it."""
    return orjson.dumps(
        {
            "method": "event.push",
            "params": {
                "event": {
                    "schema_version": "1.0",
                    "id": event_id,
                    "kind": StoreKind.EVENT.value,
                    "scope_id": None,
                    "created_at": "2026-09-17T12:00:00Z",
                    "summary": f"event {event_id}",
                    "payload": {},
                }
            },
        }
    )


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


def _patch_barrier(binding: StateBinding) -> asyncio.Event:
    """Return an event the suite awaits instead of sleeping for a delivery.

    The decoder hands a frame to the loop rather than applying it inline, so the
    barrier is what makes the assertion that follows deterministic: it is set by the
    real sink, after the real sink has run.
    """
    arrived = asyncio.Event()
    inner = binding._callbacks.on_patch
    assert inner is not None, "the seam registers a patch sink on its binder"

    async def sink(patch: KeyedPatch) -> None:
        await inner(patch)
        arrived.set()

    binding._callbacks = dataclasses.replace(binding._callbacks, on_patch=sink)
    return arrived


def _state_barrier(binding: StateBinding) -> asyncio.Event:
    """Return an event set once the binder has delivered one state refresh."""
    arrived = asyncio.Event()
    inner = binding._callbacks.on_state

    async def sink(state: State) -> None:
        await inner(state)
        arrived.set()

    binding._callbacks = dataclasses.replace(binding._callbacks, on_state=sink)
    return arrived


def _seam(state_path: Path | None, calls: list[str], **options: Any) -> ProjectionSeam:
    """Return a seam whose binder talks to the recording client."""
    return ProjectionSeam(
        route=ROUTE,
        scope_id="EAWF",
        state_path=state_path,
        clock=lambda: AT,
        daemon_client_factory=lambda: _RecordingClient(calls),
        **options,
    )


def test_the_seam_subscribes_with_the_projection_verb_on_the_one_binder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One socket, one subscribe, and the verb that makes it a projection feed."""
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    calls: list[str] = []

    async def body() -> None:
        seam = _seam(
            state_path,
            calls,
            poll_interval_s=0.0,
            daemon_probe_interval_s=3600.0,
            daemon_failure_threshold=1,
        )
        try:
            await seam.connect()
            assert seam.binding._subscribe_task is not None
            # The subscribe task is awaited, not slept past: it has returned by
            # the time the attempt list is read.
            await seam.binding._subscribe_task

            assert calls == [PROJECTION_SUBSCRIBE_METHOD]
            assert seam.binding._subscribe_method == PROJECTION_SUBSCRIBE_METHOD
        finally:
            await seam.disconnect()

    monkeypatch.setattr(StateBinding, "_daemon_socket_available", lambda _self: True)
    asyncio.run(body())


def test_the_seam_holds_exactly_one_transport(tmp_path: Path) -> None:
    """Every binding the seam holds is the one it exposes; there is no second."""
    calls: list[str] = []
    seam = _seam(None, calls)

    bindings = [value for value in vars(seam).values() if isinstance(value, StateBinding)]

    assert bindings == [seam.binding]
    assert isinstance(seam.binding, StateBinding)


def test_the_call_leg_runs_over_the_binders_own_client(tmp_path: Path) -> None:
    """A request/response call is the same transport, not a second one."""
    calls: list[str] = []
    seam = _seam(None, calls)

    answer = asyncio.run(seam.binding.call("daemon.ping", {}))

    assert answer == {"ok": True}
    assert calls == ["daemon.ping"]


def test_the_poll_backstop_still_refreshes_the_seam_when_push_drops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the projection push dies, the backstop the binder already ran carries on."""
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    calls: list[str] = []
    delivered: list[State] = []
    degraded: list[bool] = []

    async def body() -> None:
        arrivals: asyncio.Queue[State] = asyncio.Queue()

        async def on_state(state: State) -> None:
            delivered.append(state)
            await arrivals.put(state)

        async def on_degraded(flag: bool) -> None:
            degraded.append(flag)

        seam = _seam(
            state_path,
            calls,
            on_state=on_state,
            on_degraded=on_degraded,
            poll_interval_s=0.0,
            daemon_probe_interval_s=3600.0,
            daemon_failure_threshold=1,
        )
        try:
            await seam.connect()
            assert seam.binding._subscribe_task is not None
            await seam.binding._subscribe_task

            assert calls == [PROJECTION_SUBSCRIBE_METHOD]
            assert degraded == [True]
            assert seam.connection is ConnectionValue.DISCONNECTED
            initial = await asyncio.wait_for(arrivals.get(), timeout=DELIVERY_TIMEOUT_SECONDS)
            assert initial.phases["P30"].title != REFRESHED_TITLE

            _rewrite_state(state_path)
            refreshed = await asyncio.wait_for(arrivals.get(), timeout=DELIVERY_TIMEOUT_SECONDS)

            assert refreshed.phases["P30"].title == REFRESHED_TITLE
            # The drop cost exactly one subscribe on one socket.
            assert calls == [PROJECTION_SUBSCRIBE_METHOD]
            assert seam.backstop_ticks >= 2
        finally:
            await seam.disconnect()

    monkeypatch.setattr(StateBinding, "_daemon_socket_available", lambda _self: True)
    asyncio.run(body())


def test_a_pushed_patch_advances_the_binder_cursor_and_the_seam(tmp_path: Path) -> None:
    """The resume cursor the seam reconnects from is the binder's own."""
    calls: list[str] = []

    async def body() -> None:
        seam = _seam(None, calls)
        seam._projection = _projection(1)
        arrived = _patch_barrier(seam.binding)
        loop = asyncio.get_running_loop()

        assert seam.binding.resume_cursor == 0
        seam.binding._handle_push_line(loop, _patch_frame(2))
        await asyncio.wait_for(arrived.wait(), timeout=DELIVERY_TIMEOUT_SECONDS)

        assert seam.binding.resume_cursor == 2
        assert seam.cursor == 2
        held = seam.projection
        assert held is not None
        assert held.header.source_cursor == "2"
        assert [row.status.value for row in held.rows] == ["ACTIVE"]
        assert seam.connection is ConnectionValue.LIVE_COMPLETE

    asyncio.run(body())


def test_a_pushed_patch_leaves_the_seam_where_a_clean_read_would(tmp_path: Path) -> None:
    """A patched projection and a read at that cursor are one set of rows."""
    calls: list[str] = []

    async def body() -> None:
        seam = _seam(None, calls)
        seam._projection = _projection(1)
        arrived = _patch_barrier(seam.binding)
        loop = asyncio.get_running_loop()

        seam.binding._handle_push_line(loop, _patch_frame(2))
        await asyncio.wait_for(arrived.wait(), timeout=DELIVERY_TIMEOUT_SECONDS)

        held = seam.projection
        assert held is not None
        assert held.digest == _projection(2, status="ACTIVE", revision=2).digest

    asyncio.run(body())


def test_a_malformed_patch_frame_does_not_advance_the_cursor(tmp_path: Path) -> None:
    """One bad frame must not move a cursor the console would then claim."""
    calls: list[str] = []

    async def body() -> None:
        seam = _seam(None, calls)
        seam._projection = _projection(1)
        loop = asyncio.get_running_loop()

        broken = orjson.dumps(
            {"jsonrpc": "2.0", "method": PROJECTION_PUSH_METHOD, "params": {"patch": {}}}
        )
        # A frame that does not validate is dropped inside the decoder, so nothing
        # is scheduled and the assertions below read settled state.
        seam.binding._handle_push_line(loop, broken)

        assert seam.binding.resume_cursor == 0
        assert seam.cursor == 1

    asyncio.run(body())


def test_a_patch_for_another_route_is_ignored(tmp_path: Path) -> None:
    """The feed is one stream for every route the console holds."""
    calls: list[str] = []

    async def body() -> None:
        seam = ProjectionSeam(
            route="track",
            scope_id="EAWF",
            state_path=None,
            clock=lambda: AT,
            daemon_client_factory=lambda: _RecordingClient(calls),
        )
        held = build_route_projection(
            route="track",
            document={},
            cursor=1,
            scope_id="EAWF",
            generated_at=AT,
        )
        seam._projection = held
        arrived = _patch_barrier(seam.binding)
        loop = asyncio.get_running_loop()

        seam.binding._handle_push_line(loop, _patch_frame(2))
        await asyncio.wait_for(arrived.wait(), timeout=DELIVERY_TIMEOUT_SECONDS)

        assert seam.projection is held

    asyncio.run(body())


def test_the_epoch_one_feed_still_subscribes_with_its_own_verb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binder built the way the app builds it is exactly where it was."""
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    calls: list[str] = []
    delivered: list[State] = []

    async def body() -> None:
        async def on_state(state: State) -> None:
            delivered.append(state)

        async def on_degraded(_flag: bool) -> None:
            return None

        binding = StateBinding(
            state_path,
            StateBindingCallbacks(on_state=on_state, on_degraded=on_degraded),
            poll_interval_s=3600.0,
            daemon_probe_interval_s=3600.0,
            daemon_failure_threshold=1,
            daemon_client_factory=lambda: _RecordingClient(calls),
        )
        try:
            await binding.connect()
            assert binding._subscribe_task is not None
            await binding._subscribe_task

            assert binding._subscribe_method == DEFAULT_SUBSCRIBE_METHOD
            assert calls == ["state.subscribe"]
        finally:
            await binding.disconnect()

    monkeypatch.setattr(StateBinding, "_daemon_socket_available", lambda _self: True)
    asyncio.run(body())


def test_the_epoch_one_feed_still_refreshes_from_an_event_push(tmp_path: Path) -> None:
    """The event leg is untouched: same frame, same refresh, same resume cursor."""
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    delivered: list[State] = []
    calls: list[str] = []

    async def body() -> None:
        async def on_state(state: State) -> None:
            delivered.append(state)

        async def on_degraded(_flag: bool) -> None:
            return None

        binding = StateBinding(
            state_path,
            StateBindingCallbacks(on_state=on_state, on_degraded=on_degraded),
            daemon_client_factory=lambda: _RecordingClient(calls),
        )
        arrived = _state_barrier(binding)
        loop = asyncio.get_running_loop()

        binding._handle_push_line(loop, _event_frame("EV-001"))
        await asyncio.wait_for(arrived.wait(), timeout=DELIVERY_TIMEOUT_SECONDS)

        assert binding._last_event_id == "EV-001"
        assert binding._subscribe_params()["since_event_id"] == "EV-001"
        assert len(delivered) == 1
        # The projection cursor belongs to the other feed and never moved.
        assert binding.resume_cursor == 0

    asyncio.run(body())


def test_an_epoch_one_binder_drops_a_projection_patch_frame(tmp_path: Path) -> None:
    """A binder that registered no patch sink is not reached by a patch."""
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    delivered: list[State] = []
    calls: list[str] = []

    async def body() -> None:
        async def on_state(state: State) -> None:
            delivered.append(state)

        async def on_degraded(_flag: bool) -> None:
            return None

        binding = StateBinding(
            state_path,
            StateBindingCallbacks(on_state=on_state, on_degraded=on_degraded),
            daemon_client_factory=lambda: _RecordingClient(calls),
        )
        loop = asyncio.get_running_loop()

        # No patch sink is registered, so the decoder drops the frame inline and
        # schedules nothing for the loop to run later.
        binding._handle_push_line(loop, _patch_frame(4))

        assert delivered == []
        assert binding.resume_cursor == 0
        assert binding._last_event_id is None

    asyncio.run(body())
