"""Keyed patches, streamed in the contiguous order the commits allocated.

``revision`` is per-entity and orders nothing across two records, so the only
number a console can lay several kinds of row out by is ``canonical_sequence`` --
and that number is allocated inside the committing transaction, which is why the
patches carrying it are published by the daemon rather than derived by a client.

This suite drives a real connection against a real server: it subscribes over the
socket, commits transitions through the same transaction the daemon commits them
through, publishes each receipt's envelope, and reads the frames back. Nothing here
waits on a clock. The subscribe ack proves the subscriber is registered before the
first commit, and every later frame is awaited on the socket, so the run is ordered
by the stream itself rather than by a sleep long enough to hope.
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

from eawf.kernel.projection.compute import KeyedPatch, patches_for_event
from eawf.kernel.projection.read_models import ReadModelKind
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.epoch2_transaction import TransitionRequest, run_transaction
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.state_subscribe import (
    PROJECTION_PUSH_METHOD,
    PROJECTION_SUBSCRIBE_METHOD,
)
from eawf.runtime.daemon.server import handle_connection
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    MILESTONE_URN,
    method_context,
    provision,
    rekeyed,
    root_context,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

ACTOR = "OP-0001"

#: How long a frame that should already be in flight is waited for. It is a
#: failure guard, not a synchronisation primitive: every frame the suite reads is
#: caused by a publish that has already returned.
FRAME_TIMEOUT_SECONDS = 10.0

#: Milestone keys the canary is seeded with, one per commit the run makes.
KEYS = ("MLS-0030", "MLS-0031", "MLS-0032")

#: The read models a Milestone move patches: the roadmap renders milestones, and
#: so does the scope home, so one commit fans out to both.
MILESTONE_READ_MODELS = (ReadModelKind.ROADMAP_VIEW, ReadModelKind.SCOPE_HOME_VIEW)

#: The system temp dir, captured before the canary fixture redirects
#: ``tempfile.tempdir`` at a deep per-test path. An AF_UNIX node has about 104
#: characters to live in, which a pytest tmp path alone can already exhaust.
SYSTEM_TEMP_DIR = tempfile.gettempdir()


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding one planned Milestone per seeded key."""
    provisioned = provision(tmp_path / "repo", code="SUBS")
    planned = seed_row("milestone", "PLANNED")
    seed(provisioned, {"milestone": {key: rekeyed(planned, key=key) for key in KEYS}})
    return provisioned


def _socket_path() -> str:
    """Return a socket path under the AF_UNIX length ceiling (macOS: 104)."""
    return os.path.join(SYSTEM_TEMP_DIR, f"eawf-proj-{uuid.uuid4().hex[:8]}.sock")


def _urn_for(key: str) -> str:
    return f"{MILESTONE_URN.rsplit('/', 1)[0]}/{key}"


def _subscribe_frame(method: str = PROJECTION_SUBSCRIBE_METHOD) -> bytes:
    return orjson.dumps({"jsonrpc": "2.0", "id": "sub-1", "method": method, "params": {}}) + b"\n"


async def _commit(canary: CanaryProvision, runtime: Path, key: str) -> Envelope:
    """Commit one Milestone activation off the loop and return its envelope."""
    committed = await asyncio.to_thread(
        run_transaction,
        context=root_context(canary, runtime),
        request=TransitionRequest.model_validate(
            {
                "urn": _urn_for(key),
                "to_status": "ACTIVE",
                "expected_revision": 1,
                "idempotency_key": f"req-{key}",
                "actor": ACTOR,
            }
        ),
        now=AT,
    )
    return committed.envelope


async def _next_patch(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Await one ``projection.patch`` notification and return its patch body."""
    line = await asyncio.wait_for(reader.readline(), timeout=FRAME_TIMEOUT_SECONDS)
    frame = orjson.loads(line)
    assert frame["method"] == PROJECTION_PUSH_METHOD, frame
    return dict(frame["params"]["patch"])


@contextlib.asynccontextmanager
async def _subscribed(ctx: MethodContext, *, method: str = PROJECTION_SUBSCRIBE_METHOD) -> Any:
    """Serve one connection and yield its reader, subscribed and acknowledged."""
    sock_path = _socket_path()
    server = await asyncio.start_unix_server(
        lambda r, w: handle_connection(r, w, ctx), path=sock_path
    )
    reader, writer = await asyncio.open_unix_connection(sock_path)
    try:
        writer.write(_subscribe_frame(method))
        await writer.drain()
        # The ack is the registration barrier: the subscriber is on the bus by
        # the time this line lands, so nothing published after it can be missed.
        ack = orjson.loads(await asyncio.wait_for(reader.readline(), timeout=FRAME_TIMEOUT_SECONDS))
        assert ack["result"]["ok"] is True, ack
        yield reader
    finally:
        writer.close()
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            await writer.wait_closed()
        server.close()
        await server.wait_closed()
        with contextlib.suppress(OSError):
            os.unlink(sock_path)


def _transition_envelope(*, sequence: int, urn: str, status: str, revision: int) -> Envelope:
    """Build a transition envelope by hand, for payloads no commit produces."""
    return Envelope(
        id=f"evt-{sequence:04d}",
        kind=StoreKind.EVENT,
        scope_id=urn,
        created_at=datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
        summary="hand-built transition",
        payload={
            "entity_ref": urn,
            "to_status": status,
            "revision_after": revision,
            "canonical_sequence": sequence,
        },
    )


def test_subscribe_streams_patches_in_contiguous_canonical_sequence_order(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """Each commit publishes patches at the next ordinal, with no hole and no repeat."""
    ctx = method_context(tmp_path / "runtime")
    ctx.bus = EventBus()

    async def body() -> None:
        async with _subscribed(ctx) as reader:
            observed: list[int] = []
            for key in KEYS:
                envelope = await _commit(canary, tmp_path / "runtime", key)
                assert isinstance(ctx.bus, EventBus)
                ctx.bus.publish(envelope)
                for _ in MILESTONE_READ_MODELS:
                    observed.append((await _next_patch(reader))["canonical_sequence"])

            assert sorted(set(observed)) == [1, 2, 3]
            assert observed == sorted(observed)

    asyncio.run(body())


def test_one_commit_patches_every_read_model_that_renders_its_collection(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """A Milestone move reaches the roadmap and the scope home at one ordinal."""
    ctx = method_context(tmp_path / "runtime")
    ctx.bus = EventBus()

    async def body() -> None:
        async with _subscribed(ctx) as reader:
            envelope = await _commit(canary, tmp_path / "runtime", KEYS[0])
            assert isinstance(ctx.bus, EventBus)
            ctx.bus.publish(envelope)
            patches = [await _next_patch(reader) for _ in MILESTONE_READ_MODELS]

            assert {patch["projection_kind"] for patch in patches} == {
                kind.value for kind in MILESTONE_READ_MODELS
            }
            assert {patch["canonical_sequence"] for patch in patches} == {1}
            entries = [entry for patch in patches for entry in patch["entries"]]
            assert {entry["key"] for entry in entries} == {KEYS[0]}
            assert {entry["status"] for entry in entries} == {"ACTIVE"}
            assert {entry["revision"] for entry in entries} == {2}

    asyncio.run(body())


def test_subscribe_pushes_nothing_for_an_envelope_that_patches_no_read_model(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """A projection feed carries patches only, on a bus that carries every kind."""
    ctx = method_context(tmp_path / "runtime")
    ctx.bus = EventBus()

    async def body() -> None:
        async with _subscribed(ctx) as reader:
            assert isinstance(ctx.bus, EventBus)
            ctx.bus.publish(
                Envelope(
                    id="evt-not-a-transition",
                    kind=StoreKind.EVENT,
                    scope_id="P33",
                    created_at=datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
                    summary="a published envelope that moves no record",
                    payload={"note": "no canonical_sequence here"},
                )
            )
            envelope = await _commit(canary, tmp_path / "runtime", KEYS[0])
            ctx.bus.publish(envelope)

            # The first frame that arrives is the commit's patch: the envelope
            # published before it produced no frame at all.
            assert (await _next_patch(reader))["canonical_sequence"] == 1

    asyncio.run(body())


def test_a_dropped_subscriber_does_not_stop_the_daemon_committing(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """A console that leaves mid-stream is a closed socket, never a stalled writer."""
    ctx = method_context(tmp_path / "runtime")
    ctx.bus = EventBus()

    async def body() -> None:
        async with _subscribed(ctx) as reader:
            envelope = await _commit(canary, tmp_path / "runtime", KEYS[0])
            assert isinstance(ctx.bus, EventBus)
            ctx.bus.publish(envelope)
            assert (await _next_patch(reader))["canonical_sequence"] == 1
        # The subscriber is gone with the context manager; the next commit and
        # publish must still return.
        later = await _commit(canary, tmp_path / "runtime", KEYS[1])
        assert isinstance(ctx.bus, EventBus)
        ctx.bus.publish(later)

        assert later.payload["canonical_sequence"] == 2

    asyncio.run(body())


def test_patches_for_event_ignores_an_envelope_carrying_no_ordinal() -> None:
    """The bus carries every kind, so most envelopes patch nothing."""
    envelope = Envelope(
        id="evt-plain",
        kind=StoreKind.EVENT,
        scope_id="P33",
        created_at=datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
        summary="no transition here",
        payload={},
    )

    assert patches_for_event(envelope) == ()


def test_patches_for_event_ignores_a_record_no_route_renders() -> None:
    """A moved Repository patches nothing, because no route renders repositories."""
    envelope = _transition_envelope(
        sequence=1,
        urn="eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF",
        status="ACTIVE",
        revision=2,
    )

    assert patches_for_event(envelope) == ()


def test_patches_for_event_builds_one_entry_at_the_events_ordinal() -> None:
    """The single boundary: one moved record becomes one entry per read model."""
    envelope = _transition_envelope(sequence=7, urn=_urn_for(KEYS[0]), status="ACTIVE", revision=2)

    patches = patches_for_event(envelope)

    assert len(patches) == len(MILESTONE_READ_MODELS)
    assert all(isinstance(patch, KeyedPatch) for patch in patches)
    assert {patch.canonical_sequence for patch in patches} == {7}
    assert [len(patch.entries) for patch in patches] == [1, 1]
    assert {route for patch in patches for route in patch.routes} == {"roadmap", "scope.home"}


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"canonical_sequence": 0}, "not a committed ordinal"),
        ({"canonical_sequence": "1"}, "not a committed ordinal"),
        ({"canonical_sequence": 1}, "names no entity_ref"),
        ({"canonical_sequence": 1, "entity_ref": "   "}, "names no entity_ref"),
        ({"canonical_sequence": 1, "entity_ref": _urn_for(KEYS[0])}, "states no to_status"),
        (
            {
                "canonical_sequence": 1,
                "entity_ref": _urn_for(KEYS[0]),
                "to_status": "ACTIVE",
                "revision_after": 0,
            },
            "states no positive revision_after",
        ),
        (
            {
                "canonical_sequence": 1,
                "entity_ref": "not-a-urn",
                "to_status": "ACTIVE",
                "revision_after": 2,
            },
            "not a qualified URN",
        ),
    ],
)
def test_patches_for_event_refuses_a_malformed_transition(
    payload: dict[str, Any], message: str
) -> None:
    """An ordinal with nothing to patch is a defect, not a silently dropped row."""
    envelope = Envelope(
        id="evt-broken",
        kind=StoreKind.EVENT,
        scope_id="P33",
        created_at=datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
        summary="a transition that states too little",
        payload=payload,
    )

    with pytest.raises(ValueError, match=message):
        patches_for_event(envelope)
