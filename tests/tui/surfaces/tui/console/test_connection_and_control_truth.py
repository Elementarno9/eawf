"""Connection and control truth, driven through the seam over a real daemon.

The chrome says two things an operator acts on: what the link to the daemon is, and what
became of a control the operator sent. Both are easy to get plausibly wrong -- a header
that keeps drawing ``LIVE`` after the daemon died, a cancel announced as done when the
daemon only recorded that somebody asked, a lost answer reported as a failure. This suite
therefore drives the transitions for real wherever one exists: a daemon served on a
private socket, killed and restarted under the seam, a retention window moved while the
console was away, a control whose answer is lost on the way back.

Requirement rows proved here, by test-name prefix:

- UI-010: a mutating control renders in exactly one of nine outcomes (idle, requesting,
  accepted, confirmed, rejected, invalidated, unknown, recovery, superseded), each with
  its own word, and they stay distinct across a disconnect, a restart and a reconcile.
- UI-014: an uncertified or unbound capability states its reason and cannot dispatch.
- UI-032: superseded is its own terminal outcome, never a rejection, failure or error.
- UI-036: DEGRADED names only the connection; neither vocabulary admits the other's word.
- UI-037: SNAPSHOT LOADING and SNAPSHOT REQUIRED stay distinct; a transfer preserves the
  selection and filters, and a failed one returns to the refusal.
- UI-038: TERMINAL is a property of the subject, stated in its summary, never a connection
  value; the header keeps the link's own value on a terminal frame.
- UI-060: DISCONNECTED is entered when the daemon dies, names its cause, dates its last
  revision, removes writes, and leaves by replay or by a snapshot refusal.
- UI-061: every value but LIVE labels its counts known, draws the ATTACHED line, withdraws
  the verb keys and refuses writes from the one gate with that state's own reason.
- UI-027: progress never implies completeness.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.projection.compute import build_route_projection
from eawf.kernel.projection.connection import ConnectionValue, ReconnectDisposition
from eawf.kernel.projection.spine import build_spine_view
from eawf.kernel.projection.truth import (
    ConnectionState,
    Freshness,
    Precision,
    ProjectionHeader,
    TruthField,
    TruthKind,
    TruthState,
)
from eawf.kernel.runtime.control import ControlDisposition
from eawf.kernel.runtime.provider import ControlKind
from eawf.kernel.state.enums import MeasurementQuality
from eawf.kernel.state.epoch2.run import ActivityBucket
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.runtime_dir import ensure_runtime_dir
from eawf.runtime.daemon.server import handle_connection
from eawf.surfaces.tui.console.app import WRITE_TOAST_SEVERITY, ConsoleApp
from eawf.surfaces.tui.console.attention import verb_available
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.header import header_row, state_slot
from eawf.surfaces.tui.console.keybar import KEY
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.operations import (
    CONTROL_METHOD,
    OUTCOME_SENTENCES,
    SEAL_METHOD,
    ConsoleOperation,
    ControlRequest,
    OperationStatus,
    Operator,
    VerbRequest,
    binding_refusal,
    outcome_detail,
    refused,
    settled,
)
from eawf.surfaces.tui.console.reads import (
    DISCONNECTED_CAUSE,
    can_mutate,
    mut_reason,
    reads,
    write_refusal,
)
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.seam import KNOWN_COUNT_LABEL, ProjectionSeam
from eawf.surfaces.tui.console.session import Session, conn_label
from eawf.surfaces.tui.console.tokens import Severity
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    method_context,
    seed,
    seed_row,
)
from tests.tui.surfaces.tui.console.test_console_live_smoke import (
    _LoopbackClient as _StreamingClient,
)
from tests.tui.surfaces.tui.console.test_console_write_path import _Host
from tests.tui.surfaces.tui.console.test_replay_gap_recovery import (
    ACTOR,
    KEYS,
    ROUTE,
    RUN_KEY,
    _commit,
    _drop_from_retention,
    _FailingClient,
)
from tests.tui.surfaces.tui.console.test_replay_gap_recovery import (
    canary as canary,
)
from tests.tui.surfaces.tui.console.test_replay_gap_recovery import (
    canary_runtime_under_tmp as canary_runtime_under_tmp,
)

#: How long a transition the probe loop drives is waited for; a failure guard only.
TRANSITION_TIMEOUT_SECONDS = 10.0

#: The probe cadence the seam's binding runs at, so a dead daemon is noticed quickly.
PROBE_SECONDS = 0.02

#: The system temp dir, captured before the canary fixture redirects ``tempfile.tempdir``.
SYSTEM_TEMP_DIR = tempfile.gettempdir()

#: Words a superseded answer must never be reported under.
FAILURE_WORDS = ("reject", "refus", "denied", "fail", "error")

AT = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)

GOLDEN_FIXTURE = Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    """Return the tracked prototype registers."""
    return load_fixture(GOLDEN_FIXTURE)


def _proto_frame(fixture: Fixture, route: str, value: ConnectionValue) -> list[str]:
    """Return the frame the prototype registers draw for ``route`` under ``value``."""
    session = Session()
    session.route = route
    session.conn = conn_label(value)
    return render_route(View(session=session, fixture=fixture, w=80, h=24))


class _Daemon:
    """A real daemon served on the well-known socket, which a test kills and restarts.

    The seam's binding probes ``runtime_dir() / "eawfd.sock"`` to decide whether the
    daemon is up, so the daemon is served exactly there: killing it removes the socket
    and the probe loop notices, the way it would notice a real daemon exiting.
    """

    def __init__(self, ctx: MethodContext, sock_path: str) -> None:
        self._ctx = ctx
        self.sock_path = sock_path
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        """Serve the daemon on the socket."""
        self._server = await asyncio.start_unix_server(
            lambda r, w: handle_connection(r, w, self._ctx), path=self.sock_path
        )

    async def kill(self) -> None:
        """Stop serving and remove the socket, as a daemon that exited leaves it."""
        assert self._server is not None, "only a started daemon is killed"
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        with contextlib.suppress(OSError):
            os.unlink(self.sock_path)

    def client(self) -> _StreamingClient:
        """Return a client bound to the socket, whether or not anything serves it."""
        return _StreamingClient(self.sock_path)


@contextlib.contextmanager
def _runtime_dir() -> Iterator[Path]:
    """Point ``EAWF_RUNTIME_DIR`` at a short private directory for the test's life."""
    runtime = Path(SYSTEM_TEMP_DIR) / f"eawf-ct-{uuid.uuid4().hex[:8]}"
    previous = os.environ.get("EAWF_RUNTIME_DIR")
    os.environ["EAWF_RUNTIME_DIR"] = str(runtime)
    try:
        ensure_runtime_dir()
        yield runtime
    finally:
        if previous is None:
            os.environ.pop("EAWF_RUNTIME_DIR", None)
        else:
            os.environ["EAWF_RUNTIME_DIR"] = previous
        for leftover in runtime.glob("*"):
            with contextlib.suppress(OSError):
                leftover.unlink()
        with contextlib.suppress(OSError):
            runtime.rmdir()


@contextlib.asynccontextmanager
async def _live(
    canary: CanaryProvision,
    tmp_path: Path,
    *,
    route: str = ROUTE,
    client: Callable[[_Daemon], Any] | None = None,
) -> AsyncIterator[tuple[_Daemon, ProjectionSeam]]:
    """Serve the canary, connect a seam to it for real, and tear both down."""
    with _runtime_dir() as runtime:
        daemon = _Daemon(method_context(tmp_path / "runtime"), str(runtime / "eawfd.sock"))
        await daemon.start()
        seam = ProjectionSeam(
            route=route,
            scope_id="EAWF",
            state_path=None,
            repo_root=canary.root,
            daemon_client_factory=(lambda: client(daemon)) if client else daemon.client,
            daemon_probe_interval_s=PROBE_SECONDS,
            operator=Operator(principal=ACTOR),
        )
        await seam.connect()
        try:
            yield daemon, seam
        finally:
            await seam.disconnect()
            with contextlib.suppress(AssertionError):
                await daemon.kill()


async def _until(condition: Callable[[], bool]) -> None:
    """Wait for the probe loop to make ``condition`` true; fail past the guard."""
    deadline = asyncio.get_running_loop().time() + TRANSITION_TIMEOUT_SECONDS
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("the transition never happened")
        await asyncio.sleep(PROBE_SECONDS)


def _header(conn: ConnectionValue) -> str:
    """Return the header a session drawn at ``conn`` renders."""
    session = Session()
    session.conn = conn_label(conn)
    return header_row(session, crumb=" Eä ▸ eawf-core", scope="eawf-core", needs=0, w=80)


def _announced(result: Any) -> tuple[str, Severity, str]:
    """Return the toast title, severity and key-log note the console gives ``result``."""
    app = ConsoleApp(chrome=load_chrome(), clock=FakeClock())
    app.announce(result)
    toast = app.session.toasts[-1]
    return toast.title, toast.sev, app.session.log[0].note


def _op(method: str = CONTROL_METHOD, **params: Any) -> ConsoleOperation:
    return ConsoleOperation(
        operation_id="CTL-0000000000000001",
        method=method,
        params={"control": "cancel", **params} if method == CONTROL_METHOD else params,
        target=RUN_KEY,
    )


# ---------- UI-010: nine control outcomes, each its own ----------


def test_ui_010_every_outcome_has_its_own_word_sentence_and_loudness() -> None:
    assert set(OUTCOME_SENTENCES) == set(ControlDisposition)
    assert set(WRITE_TOAST_SEVERITY) == set(ControlDisposition)
    assert len(set(OUTCOME_SENTENCES.values())) == len(ControlDisposition) == 9


@pytest.mark.parametrize("disposition", list(ControlDisposition))
def test_ui_010_each_outcome_is_announced_under_its_own_title(
    disposition: ControlDisposition,
) -> None:
    result = settled(_op(), {"disposition": disposition.value})
    title, sev, note = _announced(result)

    assert result.disposition is disposition
    assert title == disposition.value
    assert note.startswith(f"{disposition.value} · ")
    # only a confirmed effect speaks as success; the request channel never paints one
    assert (sev is Severity.OK) == (disposition is ControlDisposition.CONFIRMED)


def test_ui_010_idle_is_stated_rather_than_left_blank() -> None:
    seam = ProjectionSeam(route="run.detail", scope_id="EAWF", state_path=None)
    result = asyncio.run(seam.request(ControlRequest(target=RUN_KEY, control=ControlKind.CANCEL)))

    assert result.disposition is ControlDisposition.IDLE
    assert result.status is OperationStatus.REFUSED
    assert "nothing was sent" in result.detail
    assert _announced(result)[0] == "idle"


def test_ui_010_a_rejected_cancel_ends_the_request_and_not_the_run() -> None:
    result = refused(_op(), "validation_failed: lease held")

    assert result.disposition is ControlDisposition.REJECTED
    assert "the target is unchanged" in result.detail
    assert "cancelled" not in result.detail


def test_ui_010_an_answer_naming_no_disposition_is_unknown_not_confirmed() -> None:
    assert settled(_op(), {}).disposition is ControlDisposition.UNKNOWN


def test_ui_010_an_unrecognised_disposition_is_refused_at_the_boundary() -> None:
    with pytest.raises(ValueError, match="done"):
        settled(_op(), {"disposition": "done"})


def test_ui_010_a_control_sent_to_the_daemon_is_requesting_not_done(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    seed(canary, {"run": {RUN_KEY: seed_row("run", "RUNNING")}})

    async def body() -> Any:
        async with _live(canary, tmp_path, route="activity") as (_daemon, seam):
            await seam.load()
            return await seam.request(ControlRequest(target=RUN_KEY, control=ControlKind.CANCEL))

    result = asyncio.run(body())

    assert result.disposition is ControlDisposition.REQUESTING
    assert result.status is OperationStatus.APPLIED
    assert "nothing has moved until the daemon acts" in result.detail
    title, sev, _note = _announced(result)
    assert (title, sev) == ("requesting", Severity.INFO)


def test_ui_010_outcomes_stay_distinct_across_kill_restart_and_reconcile(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """Killed daemon: unknown. Restarted: the reconnect asks again and reads requesting."""
    seed(canary, {"run": {RUN_KEY: seed_row("run", "RUNNING")}})

    async def body() -> tuple[Any, ...]:
        async with _live(canary, tmp_path, route="activity") as (daemon, seam):
            await seam.load()
            await daemon.kill()
            await _until(lambda: seam.connection is ConnectionValue.DISCONNECTED)
            lost = await seam.request(ControlRequest(target=RUN_KEY, control=ControlKind.CANCEL))
            held = seam.outstanding
            await daemon.start()
            outcome = await seam.reconnect()
            return lost, held, outcome, seam.outstanding

    lost, held, outcome, after = asyncio.run(body())

    assert lost.disposition is ControlDisposition.UNKNOWN
    assert [op.operation_id for op in held] == [lost.operation_id]
    assert [(r.operation_id, r.disposition) for r in outcome.reconciled] == [
        (lost.operation_id, ControlDisposition.REQUESTING)
    ]
    assert after == ()


class _AnswerLosingClient:
    """A client over the real socket whose control answers never make it back."""

    def __init__(self, inner: _StreamingClient) -> None:
        self._inner = inner

    def __enter__(self) -> _AnswerLosingClient:
        self._inner.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self._inner.__exit__(*exc)

    @property
    def _reader(self) -> Any:
        return self._inner._reader

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Forward the call, and lose the answer to any control request.

        Raises:
            ConnectionError: The call was a control request.
        """
        answer = self._inner.call(method, params)
        if method == CONTROL_METHOD:
            raise ConnectionError("the answer to the control request was lost")
        return answer


def test_ui_010_an_answer_lost_again_on_reconcile_is_recovery(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    seed(canary, {"run": {RUN_KEY: seed_row("run", "RUNNING")}})

    async def body() -> tuple[Any, ...]:
        async with _live(
            canary, tmp_path, client=lambda daemon: _AnswerLosingClient(daemon.client())
        ) as (_daemon, seam):
            # the Run's row addresses the control; the visible route carries no Run patch,
            # so the reconnect cannot settle it from the replay and must ask again
            await seam.load("activity")
            await seam.load()
            lost = await seam.request(ControlRequest(target=RUN_KEY, control=ControlKind.CANCEL))
            outcome = await seam.reconnect()
            return lost, outcome, seam.outstanding

    lost, outcome, after = asyncio.run(body())

    assert lost.disposition is ControlDisposition.UNKNOWN
    (again,) = outcome.reconciled
    assert again.disposition is ControlDisposition.RECOVERY
    assert again.status is OperationStatus.OUTSTANDING
    assert "you decide" in again.detail
    assert [op.operation_id for op in after] == [lost.operation_id]
    assert _announced(again)[:2] == ("recovery", Severity.WARN)


# ---------- UI-032: superseded is neither applied nor refused ----------


def test_ui_032_a_superseded_answer_is_its_own_outcome_and_never_a_failure() -> None:
    seal = _op(SEAL_METHOD, option_id="approve")
    result = settled(seal, {"outcome": "superseded", "reason": "ACT-0001 is answered by OP-1"})
    title, sev, note = _announced(result)

    assert result.status is OperationStatus.SUPERSEDED
    assert result.disposition is ControlDisposition.SUPERSEDED
    assert title == "superseded"
    assert sev is Severity.INFO
    assert "nothing was written for this one" in result.detail
    for word in FAILURE_WORDS:
        assert word not in f"{title} {note}".lower()


def test_ui_032_superseded_rejected_and_invalidated_never_share_wording() -> None:
    worded = {
        d: outcome_detail(_op(), d)
        for d in (
            ControlDisposition.SUPERSEDED,
            ControlDisposition.REJECTED,
            ControlDisposition.INVALIDATED,
        )
    }
    assert len(set(worded.values())) == 3
    assert "reject" not in worded[ControlDisposition.SUPERSEDED]


def test_ui_032_a_sealed_answer_is_confirmed() -> None:
    result = settled(_op(SEAL_METHOD, option_id="approve"), {"outcome": "sealed"})
    assert result.disposition is ControlDisposition.CONFIRMED


# ---------- UI-014: an unbound capability says why and cannot dispatch ----------


def test_ui_014_every_refused_verb_states_its_reason_and_cannot_dispatch() -> None:
    fixture = Fixture.from_chrome(load_chrome())
    refused_verbs = 0
    for route in ("attention", "run.detail", "batch.detail", "activity"):
        session = Session()
        session.route = route
        for verb in fixture.menus.verbs(route):
            check = verb_available(session, fixture, verb)
            if check.ok:
                continue
            refused_verbs += 1
            assert check.why.strip(), (route, verb.verb)
    assert refused_verbs


class _Link:
    """A daemon link that records every verb handed to it."""

    def __init__(self) -> None:
        self.sent: list[VerbRequest] = []

    def __call__(self, request: VerbRequest) -> bool:
        self.sent.append(request)
        return True


@pytest.mark.parametrize(("key", "verb"), [("z", "snooze"), ("v", "resolve")])
def test_ui_014_an_unbound_attention_verb_sends_nothing_and_says_why(
    fixture: Fixture, key: str, verb: str
) -> None:
    session = Session()
    session.route = "attention"
    link = _Link()
    ctx = Ctx(session=session, fixture=fixture, host=_Host(), w=120, h=30, send=link)

    dispatch(ctx, key, False)

    assert link.sent == []
    assert session.overlay != "consequence"
    assert session.trace is not None
    assert binding_refusal("attention", verb) in session.trace


# ---------- UI-036: DEGRADED names the connection alone ----------


def test_ui_036_a_header_emitting_provider_degraded_fails_at_the_boundary() -> None:
    with pytest.raises(ValidationError, match="connection_state"):
        ProjectionHeader.model_validate(
            {
                "schema_version": "1.0",
                "projection_kind": "SpineView",
                "scope_id": "EAWF",
                "projection_revision": 1,
                "source_cursor": "1",
                "generated_at": AT,
                "observed_at": AT,
                "connection_state": "provider_degraded",
                "completeness": "partial",
                "freshness": "live",
                "producer_refs": ["daemon"],
                "policy_revision": 1,
            }
        )


def test_ui_036_no_run_population_bucket_is_named_degraded() -> None:
    assert not any("degraded" in bucket.value for bucket in ActivityBucket)
    with pytest.raises(ValueError, match="degraded"):
        ActivityBucket("degraded")


def test_ui_036_degraded_is_one_connection_value_with_one_label() -> None:
    labels = [conn_label(value) for value in ConnectionValue]
    assert labels.count("DEGRADED") == 1
    assert ConnectionState.DEGRADED.value == ConnectionValue.DEGRADED.value
    assert "◐ DEGRADED" in _header(ConnectionValue.DEGRADED)


@pytest.mark.parametrize("value", list(ConnectionValue), ids=[v.value for v in ConnectionValue])
def test_ui_036_no_activity_frame_names_a_bucket_degraded(
    fixture: Fixture, value: ConnectionValue
) -> None:
    rows = _proto_frame(fixture, "activity", value)
    buckets = next(row for row in rows if row.startswith(" BUCKETS"))
    assert "degraded" not in buckets.lower()
    assert "degraded" not in "".join(rows[2:]).lower()


# ---------- UI-037: loading and required never collapse ----------


def test_ui_037_the_two_snapshot_values_read_apart_everywhere() -> None:
    loading, required = ConnectionValue.SNAPSHOT_LOADING, ConnectionValue.SNAPSHOT_REQUIRED
    assert conn_label(loading) != conn_label(required)
    assert _header(loading) != _header(required)
    session = Session()
    session.conn = conn_label(required)
    assert "repair" in reads(session).label
    session.conn = conn_label(loading)
    assert "loading" in reads(session).label


def test_ui_037_a_transfer_keeps_the_selection_and_a_failed_one_restates_the_refusal(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    async def body() -> tuple[Any, ...]:
        async with _live(canary, tmp_path) as (daemon, seam):
            await _commit(canary, tmp_path / "runtime", KEYS[0])
            await seam.load()
            seam.select(KEYS[0], status="ACTIVE")
            await daemon.kill()
            await _until(lambda: seam.connection is ConnectionValue.DISCONNECTED)
            envelope = await _commit(canary, tmp_path / "runtime", KEYS[1])
            _drop_from_retention(canary, sequence=int(envelope.payload["canonical_sequence"]))
            await daemon.start()
            refused_at = (await seam.reconnect()).connection

            during: list[ConnectionValue] = []
            real = seam.binding.call

            async def watched(method: str, params: dict[str, Any]) -> Any:
                during.append(seam.connection)
                return await real(method, params)

            seam.binding.call = watched  # type: ignore[method-assign]
            await seam.load_snapshot()
            kept = seam.persisted()
            repaired = seam.connection

            seam.binding._client_factory = _FailingClient  # type: ignore[assignment]
            seam._connection = ConnectionValue.SNAPSHOT_REQUIRED
            with pytest.raises(ConnectionError):
                await seam.load_snapshot()
            return refused_at, during, kept, repaired, seam.connection

    refused_at, during, kept, repaired, failed = asyncio.run(body())

    assert refused_at is ConnectionValue.SNAPSHOT_REQUIRED
    assert during
    assert set(during) == {ConnectionValue.SNAPSHOT_LOADING}
    assert (kept.selected_id, dict(kept.filters)) == (KEYS[0], {"status": "ACTIVE"})
    assert repaired is ConnectionValue.LIVE_COMPLETE
    assert failed is ConnectionValue.SNAPSHOT_REQUIRED


# ---------- UI-038: terminal is the subject's property, never a connection value ----------


def _status(value: str) -> TruthField[str]:
    return TruthField[str](
        value=value,
        state=TruthState.KNOWN,
        truth_kind=TruthKind.STORED,
        producer="document",
        producer_revision=1,
        precision=Precision.EXACT,
        measurement_quality=MeasurementQuality.EXACT,
        freshness=Freshness.LIVE,
        provenance_refs=("urn:probe:1",),
    )


def _run_frame(fixture: Fixture, status: str, conn: ConnectionValue) -> list[str]:
    document = {
        "run": {RUN_KEY: {"urn": f"urn:eawf:EAWF:run:{RUN_KEY}", "revision": 7, "status": status}}
    }
    spine = build_spine_view(
        build_route_projection(
            route="run.detail", document=document, cursor=41208, scope_id="EAWF", generated_at=AT
        )
    )
    rows = tuple(
        dataclasses.replace(row, fields={**row.fields, "status": _status(status)})
        for row in spine.rows
    )
    session = Session()
    session.route = "run.detail"
    session.subj_id = RUN_KEY
    session.conn = conn_label(conn)
    view = View(
        session=session,
        fixture=fixture,
        w=120,
        h=30,
        projection=dataclasses.replace(spine, rows=rows),
    )
    return render_route(view)


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "CANCELLED"])
@pytest.mark.parametrize("conn", [ConnectionValue.LIVE_COMPLETE, ConnectionValue.DISCONNECTED])
def test_ui_038_j4_13_a_terminal_run_keeps_the_link_and_states_its_end_below(
    fixture: Fixture, status: str, conn: ConnectionValue
) -> None:
    frame = _run_frame(fixture, status, conn)
    header = frame[0]

    assert header.endswith(state_slot(conn_label(conn)))
    assert "TERMINAL" not in header
    assert f"{status} · final" in frame[1]
    assert len(header) == 120


def test_ui_038_a_running_run_keeps_its_chip(fixture: Fixture) -> None:
    header = _run_frame(fixture, "RUNNING", ConnectionValue.DISCONNECTED)[0]
    assert header.endswith(state_slot("DISCONNECTED"))


def test_ui_038_the_attention_count_sits_before_the_link() -> None:
    session = Session()
    row = header_row(session, crumb=" Eä ▸ x", scope="x", needs=3, w=60)
    assert row.endswith("!3 NEEDS YOU  ● LIVE")


# ---------- UI-060: DISCONNECTED over a daemon that died ----------


def test_ui_060_a_killed_daemon_disconnects_and_a_restart_replays(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    async def body() -> tuple[Any, ...]:
        async with _live(canary, tmp_path) as (daemon, seam):
            await _commit(canary, tmp_path / "runtime", KEYS[0])
            await seam.load()
            live = seam.connection
            await daemon.kill()
            await _until(lambda: seam.connection is ConnectionValue.DISCONNECTED)
            counted = seam.count(4)
            await _commit(canary, tmp_path / "runtime", KEYS[1])
            await daemon.start()
            outcome = await seam.reconnect()
            return live, counted, outcome, seam.connection

    live, counted, outcome, after = asyncio.run(body())

    assert live is ConnectionValue.LIVE_COMPLETE
    assert counted == f"4 {KNOWN_COUNT_LABEL}"
    assert outcome.negotiation.disposition is ReconnectDisposition.REPLAY
    assert outcome.applied == 1
    assert after is ConnectionValue.LIVE_COMPLETE


def test_ui_060_retention_that_moved_while_disconnected_exits_to_snapshot_required(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    async def body() -> ConnectionValue:
        async with _live(canary, tmp_path) as (daemon, seam):
            await _commit(canary, tmp_path / "runtime", KEYS[0])
            await seam.load()
            await daemon.kill()
            await _until(lambda: seam.connection is ConnectionValue.DISCONNECTED)
            envelope = await _commit(canary, tmp_path / "runtime", KEYS[1])
            _drop_from_retention(canary, sequence=int(envelope.payload["canonical_sequence"]))
            await daemon.start()
            return (await seam.reconnect()).connection

    assert asyncio.run(body()) is ConnectionValue.SNAPSHOT_REQUIRED


@pytest.mark.parametrize("route", ["activity", "scope.home"])
def test_ui_060_the_disconnected_frame_names_its_cause_and_dates_its_revision(
    fixture: Fixture, route: str
) -> None:
    rows = _proto_frame(fixture, route, ConnectionValue.DISCONNECTED)

    assert rows[0].endswith(state_slot("DISCONNECTED"))
    assert rows[1].rstrip().endswith(f"disconnected · {DISCONNECTED_CAUSE}")
    assert rows[3].rstrip() == " ATTACHED  revision 41,208 · 6m 12s old"


def test_ui_060_the_disconnected_attention_frame_removes_writes_and_labels_known(
    fixture: Fixture,
) -> None:
    rows = _proto_frame(fixture, "attention", ConnectionValue.DISCONNECTED)
    session = Session()
    session.conn = conn_label(ConnectionValue.DISCONNECTED)

    assert "4 known" in rows[1]
    assert "fleet-wide" not in rows[1]
    assert not can_mutate(session)
    assert mut_reason(session, fixture) == (
        f"DISCONNECTED · {DISCONNECTED_CAUSE} · no request can be issued"
    )
    assert "a answer" not in rows[-1]
    assert "x deny" not in rows[-1]


# ---------- UI-061: every value but LIVE labels, dates and gates ----------

_NOT_LIVE = [value for value in ConnectionValue if value is not ConnectionValue.LIVE_COMPLETE]


@pytest.mark.parametrize("value", _NOT_LIVE, ids=[v.value for v in _NOT_LIVE])
def test_ui_061_a_count_outside_live_is_known_and_dated(
    fixture: Fixture, value: ConnectionValue
) -> None:
    rows = _proto_frame(fixture, "attention", value)
    attached = next(row for row in rows if row.startswith(" ATTACHED"))

    assert "known" in rows[1]
    assert "fleet-wide" not in rows[1]
    assert "all principals" not in rows[1]
    if value is ConnectionValue.SNAPSHOT_REQUIRED:
        assert attached.rstrip() == " ATTACHED  ∅ unavailable"
    elif value in (ConnectionValue.OFFLINE_SNAPSHOT, ConnectionValue.DISCONNECTED):
        assert attached.rstrip().endswith("· 6m 12s old")
    else:
        assert attached.rstrip() == " ATTACHED  revision 41,208"


@pytest.mark.parametrize("value", _NOT_LIVE, ids=[v.value for v in _NOT_LIVE])
def test_ui_061_a_state_that_refuses_writes_withdraws_the_verbs_and_says_why(
    fixture: Fixture, value: ConnectionValue
) -> None:
    rows = _proto_frame(fixture, "attention", value)
    session = Session()
    session.conn = conn_label(value)
    reason = mut_reason(session, fixture)

    assert not can_mutate(session)
    assert reason.startswith(f"{session.conn} · ")
    assert not reason.endswith("not permitted in this connection state")
    assert " ".join(KEY["actions"].pair()) in rows[-1]
    assert "a answer" not in rows[-1]


@pytest.mark.parametrize("value", list(ConnectionValue), ids=[v.value for v in ConnectionValue])
def test_c_03_every_write_verb_refuses_outside_live_naming_the_state(
    fixture: Fixture, value: ConnectionValue
) -> None:
    """C-03: of the nine connection values only LIVE admits a write; each other refuses.

    The refusal comes from the one write gate, so the same words answer a keybar verb, a
    menu verb and an overlay verb, and they name the connection state that refused it.
    """
    session = Session()
    session.conn = conn_label(value)
    refusals = {
        write_refusal(session, fixture, verb=verb)
        for verb in ("answer", "approve", "cancel", "promote", "defer")
    }

    if value is ConnectionValue.LIVE_COMPLETE:
        assert can_mutate(session)
        assert refusals == {""}
    else:
        assert not can_mutate(session)
        assert refusals == {mut_reason(session, fixture)}
        assert refusals.pop().startswith(f"{session.conn} · ")


@pytest.mark.parametrize("value", _NOT_LIVE, ids=[v.value for v in _NOT_LIVE])
def test_ui_061_an_unvouched_count_is_never_zero_dash_or_unknown(value: ConnectionValue) -> None:
    seam = ProjectionSeam(route="attention", scope_id="EAWF", state_path=None)
    seam._connection = value
    assert seam.count(0) == f"0 {KNOWN_COUNT_LABEL}"
    assert seam.count(None) == "∅ unavailable"
    assert seam.count(3) not in ("0", "?", "–")  # noqa: RUF001


# ---------- UI-027: progress never implies completeness ----------


def test_ui_027_progress_is_counted_against_its_total_and_never_called_done(
    fixture: Fixture,
) -> None:
    session = Session()
    session.route = "unattended"
    rows = render_route(View(session=session, fixture=fixture, w=120, h=30))
    head = next(i for i, row in enumerate(rows) if "PROGRESS" in row)
    queue = [row for row in rows[head + 1 :] if row.strip().startswith(("RUN-", "▸ RUN-"))]
    assert queue
    for row in queue:
        progress = row.rstrip().rsplit("  ", 1)[-1]
        assert " of " in progress or "not started" in progress, row
        assert "%" not in row
        assert "complete" not in row.lower()
        assert "done" not in row.lower()
