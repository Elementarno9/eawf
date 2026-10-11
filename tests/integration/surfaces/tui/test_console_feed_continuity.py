"""The console's live feed loses no commit: not to an idle link, not to a reconnect.

A real daemon connection handler serves a socket in a temporary tree, and the
console's binder subscribes to it through the production client with its
production one-second call timeout. Commits are appended to the firehose and
published on the bus, as the transaction does.

The suite pins the two halves of continuity. An idle stream stays open: a read
that times out kills its socket reader, so a timeout carried into the stream
ended it after one quiet second and dropped every commit until the next
subscribe. A broken stream resumes: the binder re-subscribes from the last
event a patch came from and the daemon replays the gap, and where it cannot --
the log no longer holds the cursor, or the gap is past the catch-up bound -- the
binder opens afresh and asks for a full re-read instead of drawing stale rows.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest

import eawf.runtime.daemon.methods.event as event_methods
import eawf.runtime.daemon.server as server_mod
from eawf.kernel.projection.compute import KeyedPatch, build_route_projection, patches_for_event
from eawf.kernel.projection.connection import read_method
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.runtime.daemon.bus import EventBus, Subscriber
from eawf.runtime.daemon.methods.state_subscribe import PROJECTION_SUBSCRIBE_METHOD
from eawf.surfaces.cli._daemon_client import DaemonClient
from eawf.surfaces.tui.chassis.state_binding import StateBinding, StateBindingCallbacks
from eawf.surfaces.tui.console.seam import ProjectionSeam
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    MILESTONE_URN,
    method_context,
)

pytestmark = pytest.mark.integration

#: How long a delivery already in flight is waited for. A failure guard, never
#: the thing that orders the run.
DELIVERY_TIMEOUT_SECONDS = 10.0

#: Longer than three production call timeouts, so a stream the timeout kills is
#: dead well before the first commit.
IDLE_SECONDS = 3.5

#: The production client's call timeout, which the binder must not carry into the stream.
CALL_TIMEOUT_SECONDS = 1.0

#: The route the seam case binds.
ROUTE = "roadmap"

AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


class _RecordingBus(EventBus):
    """The daemon's bus, recording the cursor every subscribe registered with."""

    def __init__(self) -> None:
        super().__init__()
        self.cursors: list[str | None] = []

    def register(self, **kwargs: Any) -> Subscriber:
        """Record the cursor, then register as the daemon does."""
        self.cursors.append(kwargs.get("since_event_id"))
        return super().register(**kwargs)


class _SocketClient(DaemonClient):
    """The production client, connected to the test socket without a spawn or a ping."""

    def __enter__(self) -> _SocketClient:
        self._connect_socket()
        return self


class _Feed:
    """One daemon, one console binder, and everything the binder delivered."""

    def __init__(self, root: Path, runtime: Path) -> None:
        self.ctx = method_context(root / "runtime")
        self.bus = _RecordingBus()
        self.ctx.bus = self.bus
        self.event_path = root / "store" / "event.jsonl"
        self.event_path.parent.mkdir(parents=True)
        self.ctx.event_path = self.event_path
        self.runtime = runtime
        self.patches: list[int] = []
        self.resyncs = 0
        self.expected: list[int] = []
        self.binding = StateBinding(
            None,
            StateBindingCallbacks(
                on_state=self._ignore,
                on_degraded=self._ignore,
                on_patch=self._on_patch,
                on_resync=self._on_resync,
            ),
            poll_interval_s=3600.0,
            daemon_probe_interval_s=3600.0,
            daemon_client_factory=lambda: _SocketClient(
                runtime_dir=runtime, call_timeout_seconds=CALL_TIMEOUT_SECONDS
            ),
            subscribe_method=PROJECTION_SUBSCRIBE_METHOD,
        )

    async def _ignore(self, _value: object) -> None:
        return None

    async def _on_patch(self, patch: KeyedPatch) -> None:
        self.patches.append(patch.canonical_sequence)

    async def _on_resync(self) -> None:
        self.resyncs += 1

    def commit(self, sequence: int, *, published: bool = True) -> Envelope:
        """Append one committed Milestone move to the firehose and publish it."""
        envelope = Envelope(
            id=f"evt-feed-{sequence:04d}",
            kind=StoreKind.EVENT,
            scope_id=MILESTONE_URN,
            created_at=AT,
            summary=f"domain.milestone.update MLS-0030 sequence {sequence}",
            payload={
                "entity_ref": MILESTONE_URN,
                "to_status": "ACTIVE",
                "revision_after": sequence + 1,
                "canonical_sequence": sequence,
            },
        )
        with self.event_path.open("ab") as handle:
            handle.write(orjson.dumps(envelope.model_dump(mode="json")) + b"\n")
        if published:
            self.bus.publish(envelope)
        return envelope

    def owe(self, *envelopes: Envelope) -> None:
        """Expect every patch *envelopes* produce, in order."""
        for envelope in envelopes:
            self.expected += [patch.canonical_sequence for patch in patches_for_event(envelope)]

    async def delivered(self) -> None:
        """Wait until every expected patch arrived, then assert nothing else did."""
        await _until(lambda: len(self.patches) >= len(self.expected))
        await asyncio.sleep(0.2)
        assert self.patches == self.expected

    async def subscribed(self, count: int) -> None:
        """Wait until *count* subscribes have registered, the latest one live."""
        await _until(lambda: len(self.bus.cursors) >= count and self.bus.active_subscriptions == 1)
        assert len(self.bus.cursors) == count

    async def kill_stream(self) -> None:
        """Break the live connection under the binder, as a dropped link does."""
        sock = self.binding._subscription_sock
        assert sock is not None
        sock.shutdown(socket.SHUT_RDWR)
        task = self.binding._subscribe_task
        assert task is not None
        await asyncio.wait_for(task, timeout=DELIVERY_TIMEOUT_SECONDS)
        await _until(lambda: self.bus.active_subscriptions == 0)

    async def reconnect(self) -> None:
        """Re-subscribe now, as the probe loop does once its throttle allows."""
        self.binding._last_subscribe_monotonic = 0.0
        await self.binding._start_subscribe_loop()


async def _until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + DELIVERY_TIMEOUT_SECONDS
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(0.02)


@contextlib.contextmanager
def _runtime_dir() -> Iterator[Path]:
    """Yield a runtime directory short enough for an AF_UNIX path (macOS: 104)."""
    path = Path(tempfile.gettempdir()) / f"eawf-feed-{uuid.uuid4().hex[:8]}"
    path.mkdir()
    try:
        yield path
    finally:
        with contextlib.suppress(OSError):
            (path / "eawfd.sock").unlink()
        with contextlib.suppress(OSError):
            path.rmdir()


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: Callable[[_Feed], Any]) -> None:
    """Serve one daemon socket, connect the console binder to it and run *body*."""
    monkeypatch.setattr(StateBinding, "_daemon_socket_available", lambda _self: True)

    async def main(runtime: Path) -> None:
        feed = _Feed(tmp_path, runtime)
        server = await asyncio.start_unix_server(
            lambda r, w: server_mod.handle_connection(r, w, feed.ctx),
            path=str(runtime / "eawfd.sock"),
        )
        try:
            await feed.binding.connect()
            await feed.subscribed(1)
            await body(feed)
        finally:
            await feed.binding.disconnect()
            server.close()
            await server.wait_closed()

    with _runtime_dir() as runtime:
        asyncio.run(main(runtime))


def test_an_idle_stream_delivers_every_later_commit_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quiet for longer than the call timeout, the stream still carries every commit."""

    async def body(feed: _Feed) -> None:
        await asyncio.sleep(IDLE_SECONDS)
        for sequence in (1, 2, 3):
            feed.owe(feed.commit(sequence))
            await asyncio.sleep(0.3)
        await feed.delivered()
        assert feed.bus.cursors == [None]
        assert feed.resyncs == 0

    _run(tmp_path, monkeypatch, body)


def test_an_idle_stream_with_no_commits_stays_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: nothing committed, so nothing delivered and nothing re-subscribed."""

    async def body(feed: _Feed) -> None:
        await asyncio.sleep(IDLE_SECONDS)
        task = feed.binding._subscribe_task
        assert task is not None
        assert not task.done()
        assert feed.patches == []
        assert feed.bus.cursors == [None]
        assert feed.resyncs == 0

    _run(tmp_path, monkeypatch, body)


def test_a_reconnect_replays_the_commits_the_gap_missed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stream resumes from the last patch's event; the daemon replays the rest."""

    async def body(feed: _Feed) -> None:
        first = feed.commit(1)
        feed.owe(first)
        await feed.delivered()

        await feed.kill_stream()
        feed.owe(feed.commit(2), feed.commit(3))
        await feed.reconnect()
        await feed.subscribed(2)
        feed.owe(feed.commit(4))

        await feed.delivered()
        assert feed.bus.cursors == [None, first.id]
        assert feed.resyncs == 0

    _run(tmp_path, monkeypatch, body)


def test_a_cursor_the_log_no_longer_holds_forces_a_full_re_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cursor beyond the backlog: the daemon refuses it and the binder re-reads."""

    async def body(feed: _Feed) -> None:
        first = feed.commit(1)
        feed.owe(first)
        await feed.delivered()

        await feed.kill_stream()
        feed.event_path.write_bytes(b"")
        feed.commit(2)
        await feed.reconnect()
        await feed.subscribed(3)
        feed.owe(feed.commit(3))

        await feed.delivered()
        assert feed.bus.cursors == [None, first.id, None]
        assert feed.resyncs == 1

    _run(tmp_path, monkeypatch, body)


def test_a_gap_past_the_catch_up_bound_forces_a_full_re_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off-by-one past the bound: two missed commits against a bound of one."""
    monkeypatch.setattr(event_methods, "CATCH_UP_MAX", 1)

    async def body(feed: _Feed) -> None:
        first = feed.commit(1)
        feed.owe(first)
        await feed.delivered()

        await feed.kill_stream()
        feed.commit(2)
        feed.commit(3)
        await feed.reconnect()
        await feed.subscribed(3)

        await feed.delivered()
        assert feed.bus.cursors == [None, first.id, None]
        assert feed.resyncs == 1

    _run(tmp_path, monkeypatch, body)


def test_a_gap_at_the_catch_up_bound_is_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: exactly as many missed commits as the bound replays, no re-read."""
    monkeypatch.setattr(event_methods, "CATCH_UP_MAX", 1)

    async def body(feed: _Feed) -> None:
        first = feed.commit(1)
        feed.owe(first)
        await feed.delivered()

        await feed.kill_stream()
        feed.owe(feed.commit(2))
        await feed.reconnect()
        await feed.subscribed(2)

        await feed.delivered()
        assert feed.bus.cursors == [None, first.id]
        assert feed.resyncs == 0

    _run(tmp_path, monkeypatch, body)


def test_a_reconnect_with_no_cursor_forces_a_full_re_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing delivered before the break, so nothing to resume from: re-read instead."""

    async def body(feed: _Feed) -> None:
        assert feed.resyncs == 0
        await feed.kill_stream()
        feed.commit(1)
        await feed.reconnect()
        await feed.subscribed(2)
        await _until(lambda: feed.resyncs == 1)
        feed.owe(feed.commit(2))

        await feed.delivered()
        assert feed.bus.cursors == [None, None]
        assert feed.resyncs == 1

    _run(tmp_path, monkeypatch, body)


class _ReadClient:
    """A client answering route reads from a queue; a subscribe is never asked."""

    def __init__(self, answers: list[dict[str, Any] | Exception]) -> None:
        self.answers = answers

    def __enter__(self) -> _ReadClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, _params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Answer the next queued read of the bound route.

        Raises:
            Exception: The queued answer is one.
        """
        assert method == read_method(ROUTE)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _route_answer(cursor: int) -> dict[str, Any]:
    return build_route_projection(
        route=ROUTE,
        document={"milestone": {"MLS-0030": {"urn": MILESTONE_URN, "revision": 1}}},
        cursor=cursor,
        scope_id="EAWF",
        generated_at=AT,
    ).model_dump(mode="json")


def _seam(answers: list[dict[str, Any] | Exception]) -> ProjectionSeam:
    return ProjectionSeam(
        route=ROUTE,
        scope_id="EAWF",
        state_path=None,
        daemon_client_factory=lambda: _ReadClient(answers),
    )


def test_the_seam_resync_reads_the_visible_route_again_and_owes_the_rest() -> None:
    """The console's re-read holds the route at the daemon's head and repaints it."""
    answers: list[dict[str, Any] | Exception] = [_route_answer(1), _route_answer(7)]
    seam = _seam(answers)
    heard: list[tuple[str, ...]] = []
    seam.watch(heard.append)

    async def body() -> None:
        await seam.load()
        await seam.resync()

    asyncio.run(body())

    projection = seam.projection
    assert projection is not None
    assert int(projection.header.source_cursor) == 7
    assert seam.held_routes == (ROUTE,)
    assert heard == [(ROUTE,)]
    assert answers == []


def test_a_seam_resync_whose_read_fails_leaves_the_route_owed() -> None:
    """Error path: a failed re-read drops the pre-gap rows rather than drawing them."""
    seam = _seam([_route_answer(1), ConnectionError("daemon went away")])
    heard: list[tuple[str, ...]] = []
    seam.watch(heard.append)

    async def body() -> None:
        await seam.load()
        await seam.resync()

    asyncio.run(body())

    assert seam.projection is None
    assert seam.held_routes == ()
    assert heard == [(ROUTE,)]


def test_a_seam_resync_with_nothing_held_reads_nothing() -> None:
    """Boundary: a seam that never loaded has nothing stale; the watchers still hear it."""
    answers: list[dict[str, Any] | Exception] = []
    seam = _seam(answers)
    heard: list[tuple[str, ...]] = []
    seam.watch(heard.append)

    asyncio.run(seam.resync())

    assert seam.projection is None
    assert heard == [(ROUTE,)]


def test_the_projection_patch_frame_names_the_event_it_came_from() -> None:
    """The resume cursor rides every patch frame, since a patch carries only an ordinal."""
    moved = Envelope(
        id="evt-cursor-0001",
        kind=StoreKind.EVENT,
        scope_id=MILESTONE_URN,
        created_at=AT,
        summary="domain.milestone.activate MLS-0030 PLANNED -> ACTIVE",
        payload={
            "entity_ref": MILESTONE_URN,
            "to_status": "ACTIVE",
            "revision_after": 2,
            "canonical_sequence": 1,
        },
    )
    frames = [orjson.loads(frame) for frame in server_mod._projection_frames(moved)]

    assert frames
    assert {frame["params"]["event_id"] for frame in frames} == {"evt-cursor-0001"}
