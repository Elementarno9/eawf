"""A reconnect either replays the exact range, or refuses and names it.

The question a returning console cannot answer for itself is whether the range it
missed is still on the daemon's disk. This suite drives both answers against a real
daemon over a real socket: the same canary, the same transaction, the same firehose,
and a retention window the test moves by dropping rows from the front of that
firehose the way retention does.

The property worth the whole seam is the last one here. A console that was replayed
and a console that was made to fetch a whole projection must hold the same rows at
one ``canonical_sequence``, and they must be able to prove it by digest rather than
by inspection. Both paths therefore end on a digest comparison against a clean read
at that cursor.

Nothing here waits on a clock. Every commit is awaited before the read that follows
it, and every daemon answer is a request/response over the socket, so the ordering
is carried by the calls themselves.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import tempfile
import uuid
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.kernel.projection.connection import (
    ConnectionValue,
    ReconnectDisposition,
    ReconnectNegotiation,
    negotiate_reconnect,
)
from eawf.kernel.projection.truth import ConnectionState
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.epoch2_transaction import TransitionRequest, run_transaction
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.server import handle_connection
from eawf.surfaces.tui.console.seam import ProjectionSeam
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    MILESTONE_URN,
    method_context,
    provision,
    rekeyed,
    root_context,
    seed,
    seed_row,
    tree_root,
)

ACTOR = "OP-0001"

#: The route the suite reconnects for. A Milestone move reaches it, so each commit
#: produces a patch the replay has to carry.
ROUTE = "roadmap"

#: Milestone keys the canary is seeded with, one per commit the run makes.
KEYS = ("MLS-0030", "MLS-0031", "MLS-0032")

#: How long a socket answer that has already been asked for is waited for. A failure
#: guard, never the thing that orders the run.
CALL_TIMEOUT_SECONDS = 10.0

#: The system temp dir, captured before the canary fixture redirects
#: ``tempfile.tempdir``. An AF_UNIX node has about 104 characters to live in.
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
    provisioned = provision(tmp_path / "repo", code="GAPS")
    planned = seed_row("milestone", "PLANNED")
    seed(provisioned, {"milestone": {key: rekeyed(planned, key=key) for key in KEYS}})
    return provisioned


class _LoopbackClient:
    """A daemon client that speaks to the suite's own server over one socket.

    Stands in for :class:`~eawf.surfaces.cli._daemon_client.DaemonClient` so the
    binding's transport reaches the real connection handler without the suite
    having to place a socket at the daemon's well-known runtime path.

    Attributes:
        calls: Every method asked over every client this factory produced, in order.
    """

    def __init__(self, sock_path: str, calls: list[str]) -> None:
        self._sock_path = sock_path
        self.calls = calls
        self._sock: socket.socket | None = None

    def __enter__(self) -> _LoopbackClient:
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(CALL_TIMEOUT_SECONDS)
        self._sock.connect(self._sock_path)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one request frame and return the result the daemon answered with.

        Raises:
            RuntimeError: The daemon answered with a JSON-RPC error.
        """
        assert self._sock is not None, "a call is made inside the client's context"
        self.calls.append(method)
        frame = orjson.dumps(
            {"jsonrpc": "2.0", "id": "seam-1", "method": method, "params": params or {}}
        )
        self._sock.sendall(frame + b"\n")
        buffer = b""
        while b"\n" not in buffer:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise RuntimeError(f"the daemon closed the socket during {method}")
            buffer += chunk
        reply = orjson.loads(buffer.split(b"\n", 1)[0])
        if "error" in reply:
            raise RuntimeError(str(reply["error"].get("message", reply["error"])))
        return dict(reply["result"])


class _FailingClient:
    """A client whose every call fails, standing in for a transfer that dies."""

    def __enter__(self) -> _FailingClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, _method: str, _params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fail the call.

        Raises:
            ConnectionError: Always.
        """
        raise ConnectionError("the snapshot transfer died")


def _socket_path() -> str:
    """Return a socket path under the AF_UNIX length ceiling (macOS: 104)."""
    return os.path.join(SYSTEM_TEMP_DIR, f"eawf-gap-{uuid.uuid4().hex[:8]}.sock")


def _urn_for(key: str) -> str:
    return f"{MILESTONE_URN.rsplit('/', 1)[0]}/{key}"


def _firehose(canary: CanaryProvision) -> Path:
    """Return the canary's firehose, which is the daemon's retention window."""
    return store_path(tree_root(canary) / "state.json", StoreKind.EVENT)


def _drop_from_retention(canary: CanaryProvision, *, sequence: int) -> None:
    """Rewrite the firehose without *sequence*, as retention moving past it does."""
    path = _firehose(canary)
    kept = [
        line
        for line in path.read_bytes().splitlines()
        if line.strip()
        and Envelope.model_validate(orjson.loads(line)).payload.get("canonical_sequence")
        != sequence
    ]
    path.write_bytes(b"".join(line + b"\n" for line in kept))


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


@contextlib.asynccontextmanager
async def _served(ctx: MethodContext) -> Any:
    """Serve the daemon on a socket and yield a client factory bound to it."""
    sock_path = _socket_path()
    server = await asyncio.start_unix_server(
        lambda r, w: handle_connection(r, w, ctx), path=sock_path
    )
    calls: list[str] = []
    try:
        yield lambda: _LoopbackClient(sock_path, calls), calls
    finally:
        server.close()
        await server.wait_closed()
        with contextlib.suppress(OSError):
            os.unlink(sock_path)


def _seam(canary: CanaryProvision, factory: Any) -> ProjectionSeam:
    """Return a seam bound to the canary, over the suite's loopback transport."""
    return ProjectionSeam(
        route=ROUTE,
        scope_id="EAWF",
        state_path=None,
        repo_root=canary.root,
        daemon_client_factory=factory,
    )


async def _ask(factory: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Ask the reconnect verb directly, off the loop the daemon is served on.

    The client is synchronous, so calling it on the loop would block the very
    connection handler that has to answer it.
    """

    def _blocking() -> dict[str, Any]:
        with factory() as client:
            return client.call(f"projection.{ROUTE}.reconnect", params)

    return await asyncio.to_thread(_blocking)


async def _clean_digest(canary: CanaryProvision, factory: Any) -> tuple[str, str]:
    """Return the digest and cursor of a clean read of the route right now."""
    fresh = _seam(canary, factory)
    projection = await fresh.load()
    return projection.digest, projection.header.source_cursor


def test_replay_inside_retention_closes_the_exact_gap(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """A cursor retention still covers is replayed over the range the daemon names."""
    ctx = method_context(tmp_path / "runtime")

    async def body() -> None:
        async with _served(ctx) as (factory, _calls):
            await _commit(canary, tmp_path / "runtime", KEYS[0])
            seam = _seam(canary, factory)
            held = await seam.load()
            assert held.header.source_cursor == "1"

            await _commit(canary, tmp_path / "runtime", KEYS[1])
            await _commit(canary, tmp_path / "runtime", KEYS[2])
            outcome = await seam.reconnect()

            assert outcome.negotiation.disposition is ReconnectDisposition.REPLAY
            assert outcome.negotiation.gap is not None
            assert (
                outcome.negotiation.gap.first_sequence,
                outcome.negotiation.gap.last_sequence,
            ) == (2, 3)
            assert outcome.negotiation.gap.length == 2
            assert outcome.applied == 2
            assert seam.cursor == 3
            assert seam.connection is ConnectionValue.LIVE_COMPLETE

    asyncio.run(body())


def test_a_replayed_projection_digests_like_a_clean_read_at_one_cursor(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """Step seven: the replay must land on the rows a fresh read would have given."""
    ctx = method_context(tmp_path / "runtime")

    async def body() -> None:
        async with _served(ctx) as (factory, _calls):
            await _commit(canary, tmp_path / "runtime", KEYS[0])
            seam = _seam(canary, factory)
            stale = await seam.load()

            await _commit(canary, tmp_path / "runtime", KEYS[1])
            await _commit(canary, tmp_path / "runtime", KEYS[2])
            await seam.reconnect()
            clean_digest, clean_cursor = await _clean_digest(canary, factory)

            replayed = seam.projection
            assert replayed is not None
            assert replayed.header.source_cursor == clean_cursor == "3"
            assert replayed.digest == clean_digest
            # The point of the comparison: the held projection really had moved.
            assert stale.digest != clean_digest

    asyncio.run(body())


def test_a_cursor_outside_retention_demands_a_snapshot_and_names_the_gap(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """A refusal states the exact range it could not supply, and where it broke."""
    ctx = method_context(tmp_path / "runtime")

    async def body() -> None:
        async with _served(ctx) as (factory, _calls):
            await _commit(canary, tmp_path / "runtime", KEYS[0])
            seam = _seam(canary, factory)
            await seam.load()

            await _commit(canary, tmp_path / "runtime", KEYS[1])
            await _commit(canary, tmp_path / "runtime", KEYS[2])
            _drop_from_retention(canary, sequence=2)
            outcome = await seam.reconnect()

            negotiation = outcome.negotiation
            assert negotiation.disposition is ReconnectDisposition.SNAPSHOT_REQUIRED
            assert negotiation.gap is not None
            assert (negotiation.gap.first_sequence, negotiation.gap.last_sequence) == (2, 3)
            assert negotiation.first_missing == 2
            assert negotiation.retention.first_sequence == 1
            assert negotiation.retention.last_sequence == 3
            assert outcome.applied == 0
            assert seam.connection is ConnectionValue.SNAPSHOT_REQUIRED
            # The refusal is a refusal: nothing was applied over the hole.
            assert seam.cursor == 1

    asyncio.run(body())


def test_replay_and_snapshot_end_at_one_digest_at_one_cursor(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """The two repairs are different journeys to the same rows at the same ordinal."""
    ctx = method_context(tmp_path / "runtime")

    async def body() -> None:
        async with _served(ctx) as (factory, _calls):
            await _commit(canary, tmp_path / "runtime", KEYS[0])
            replayer, snapshotter = _seam(canary, factory), _seam(canary, factory)
            await replayer.load()
            await snapshotter.load()

            await _commit(canary, tmp_path / "runtime", KEYS[1])
            await _commit(canary, tmp_path / "runtime", KEYS[2])
            replayed = await replayer.reconnect()
            # Retention moves only after the replayable client has been served.
            _drop_from_retention(canary, sequence=2)
            refused = await snapshotter.reconnect()
            repaired = await snapshotter.load_snapshot()

            assert replayed.negotiation.disposition is ReconnectDisposition.REPLAY
            assert refused.negotiation.disposition is ReconnectDisposition.SNAPSHOT_REQUIRED
            assert replayer.projection is not None
            assert replayer.projection.header.source_cursor == repaired.header.source_cursor == "3"
            assert replayer.projection.digest == repaired.digest
            assert snapshotter.connection is ConnectionValue.LIVE_COMPLETE

    asyncio.run(body())


def test_a_failed_snapshot_transfer_restates_the_refusal(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """A transfer that dies leaves the console exactly as unrepaired as before."""
    ctx = method_context(tmp_path / "runtime")

    async def body() -> None:
        async with _served(ctx) as (factory, _calls):
            await _commit(canary, tmp_path / "runtime", KEYS[0])
            seam = _seam(canary, factory)
            await seam.load()
            await _commit(canary, tmp_path / "runtime", KEYS[1])
            _drop_from_retention(canary, sequence=2)
            await seam.reconnect()
            assert seam.connection is ConnectionValue.SNAPSHOT_REQUIRED

            seam.binding._client_factory = _FailingClient  # type: ignore[assignment]
            with pytest.raises(ConnectionError):
                await seam.load_snapshot()

            assert seam.connection is ConnectionValue.SNAPSHOT_REQUIRED

    asyncio.run(body())


def test_a_cursor_already_at_the_daemons_is_current_with_no_gap(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """The boundary between nothing missed and one row missed."""
    ctx = method_context(tmp_path / "runtime")

    async def body() -> None:
        async with _served(ctx) as (factory, _calls):
            await _commit(canary, tmp_path / "runtime", KEYS[0])
            seam = _seam(canary, factory)
            await seam.load()

            outcome = await seam.reconnect()

            assert outcome.negotiation.disposition is ReconnectDisposition.CURRENT
            assert outcome.negotiation.gap is None
            assert outcome.applied == 0
            assert seam.connection is ConnectionValue.LIVE_COMPLETE

    asyncio.run(body())


def test_a_selection_the_projection_lost_is_reported_and_not_moved(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """Step five: a missing selected row opens a resolution card, never a neighbour."""
    ctx = method_context(tmp_path / "runtime")

    async def body() -> None:
        async with _served(ctx) as (factory, _calls):
            await _commit(canary, tmp_path / "runtime", KEYS[0])
            seam = _seam(canary, factory)
            await seam.load()
            seam.select("MLS-9999", status="ACTIVE")
            await _commit(canary, tmp_path / "runtime", KEYS[1])

            outcome = await seam.reconnect()

            assert outcome.selection_missing is True
            assert seam.persisted().selected_id == "MLS-9999"
            assert seam.persisted().filters == {"status": "ACTIVE"}
            assert seam.persisted().cursor == 2

    asyncio.run(body())


def test_reconnect_before_any_load_refuses(canary: CanaryProvision, tmp_path: Path) -> None:
    """A console holding no revision cold-loads; it has nothing to reconnect to."""
    ctx = method_context(tmp_path / "runtime")

    async def body() -> None:
        async with _served(ctx) as (factory, _calls):
            seam = _seam(canary, factory)

            with pytest.raises(ValueError, match="no held projection"):
                await seam.reconnect()

    asyncio.run(body())


def test_a_cursor_ahead_of_the_daemon_is_refused(canary: CanaryProvision, tmp_path: Path) -> None:
    """A client cannot hold an ordinal this tree never allocated."""
    ctx = method_context(tmp_path / "runtime")

    async def body() -> None:
        async with _served(ctx) as (factory, _calls):
            with pytest.raises(RuntimeError, match="stands ahead of the daemon"):
                await _ask(factory, {"repo_root": str(canary.root), "cursor": 9})

    asyncio.run(body())


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"cursor": -1}, "bad parameter"),
        ({"cursor": "2"}, "bad parameter"),
        ({"cursor": 1, "unknown": True}, "bad parameter"),
    ],
)
def test_reconnect_refuses_parameters_that_are_not_a_request(
    canary: CanaryProvision, tmp_path: Path, params: dict[str, Any], message: str
) -> None:
    """The boundary is validated at the daemon, not normalised downstream."""
    ctx = method_context(tmp_path / "runtime")

    async def body() -> None:
        async with _served(ctx) as (factory, _calls):
            with pytest.raises(RuntimeError, match=message):
                await _ask(factory, {"repo_root": str(canary.root), **params})

    asyncio.run(body())


def test_a_replay_carrying_a_patch_outside_the_gap_is_refused(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """A console cannot claim a range was closed by rows from outside it."""
    ctx = method_context(tmp_path / "runtime")

    async def body() -> None:
        async with _served(ctx) as (factory, _calls):
            await _commit(canary, tmp_path / "runtime", KEYS[0])
            seam = _seam(canary, factory)
            await seam.load()
            await _commit(canary, tmp_path / "runtime", KEYS[1])

            answer = await seam.binding.call(
                f"projection.{ROUTE}.reconnect",
                {"repo_root": str(canary.root), "cursor": 1},
            )
            answer["patches"][0]["canonical_sequence"] = 9

            async def _forged(_method: str, _params: dict[str, Any]) -> dict[str, Any]:
                return answer

            seam.binding.call = _forged  # type: ignore[method-assign]
            with pytest.raises(ValueError, match="outside the gap"):
                await seam.reconnect()

    asyncio.run(body())


@pytest.mark.parametrize(
    ("client_cursor", "server_cursor", "retained", "disposition"),
    [
        (0, 1, (1,), ReconnectDisposition.REPLAY),
        (0, 1, (), ReconnectDisposition.SNAPSHOT_REQUIRED),
        (1, 2, (1, 2), ReconnectDisposition.REPLAY),
        (1, 2, (2,), ReconnectDisposition.REPLAY),
        (1, 2, (1,), ReconnectDisposition.SNAPSHOT_REQUIRED),
    ],
)
def test_negotiation_turns_on_the_retention_floor(
    client_cursor: int,
    server_cursor: int,
    retained: tuple[int, ...],
    disposition: ReconnectDisposition,
) -> None:
    """Off by one at the floor decides between a replay and a refusal."""
    negotiation = negotiate_reconnect(
        route=ROUTE,
        client_cursor=client_cursor,
        server_cursor=server_cursor,
        retained=retained,
    )

    assert negotiation.disposition is disposition


def test_a_hole_inside_retention_refuses_even_with_both_ends_held() -> None:
    """Contiguity, not the ends: a replay around a hole is the silent corruption."""
    negotiation = negotiate_reconnect(
        route=ROUTE, client_cursor=0, server_cursor=3, retained=(1, 3)
    )

    assert negotiation.disposition is ReconnectDisposition.SNAPSHOT_REQUIRED
    assert negotiation.first_missing == 2
    assert negotiation.retention.first_sequence == 1
    assert negotiation.retention.last_sequence == 3


def test_an_empty_retention_window_states_neither_end() -> None:
    """The empty boundary: nothing retained is a window with no ends, not a zero."""
    negotiation = negotiate_reconnect(route=ROUTE, client_cursor=0, server_cursor=0, retained=())

    assert negotiation.disposition is ReconnectDisposition.CURRENT
    assert negotiation.retention.first_sequence is None
    assert negotiation.retention.last_sequence is None


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"route": "settings"}, "renders no epoch-2 collection"),
        ({"client_cursor": -1}, "client_cursor is a committed"),
        ({"server_cursor": -1}, "server_cursor is a committed"),
        ({"client_cursor": 5, "server_cursor": 2}, "stands ahead of the daemon"),
        ({"retained": (0,)}, "not a committed ordinal"),
    ],
)
def test_negotiate_reconnect_refuses_what_it_cannot_negotiate(
    kwargs: dict[str, Any], message: str
) -> None:
    """Every refusal is raised at the boundary, never normalised into an answer."""
    call: dict[str, Any] = {
        "route": ROUTE,
        "client_cursor": 1,
        "server_cursor": 3,
        "retained": (2, 3),
    }
    call.update(kwargs)

    with pytest.raises(ValueError, match=message):
        negotiate_reconnect(**call)


def test_a_negotiation_that_contradicts_itself_is_refused() -> None:
    """The model is the last gate: a refusal with no missing row states nothing."""
    with pytest.raises(ValueError, match="names its first missing row"):
        ReconnectNegotiation(
            schema_version="1.0",
            route=ROUTE,
            disposition=ReconnectDisposition.SNAPSHOT_REQUIRED,
            client_cursor=1,
            server_cursor=3,
            gap={"first_sequence": 2, "last_sequence": 3},  # type: ignore[arg-type]
            retention={},  # type: ignore[arg-type]
        )


def test_the_replaying_state_is_the_one_the_header_vocabulary_names() -> None:
    """The seam's nine values and the header's states are one vocabulary, not two."""
    assert ConnectionValue.REPLAYING.value == ConnectionState.REPLAYING.value
    assert ConnectionValue.SNAPSHOT_REQUIRED.value == ConnectionState.SNAPSHOT_REQUIRED.value
