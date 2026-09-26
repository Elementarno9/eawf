"""Console verbs reach the daemon, and a verb no daemon mutator carries says why it cannot.

A confirmed answer is sent to the approval seal and a confirmed Run control to the
control-request verb, each under a freshly minted operation id, and nothing the console
holds changes on the way: the daemon's answer and the patch its commit pushes are the only
things that move a frame. Every other writing verb stays listed and is refused with its
reason, in the menu and on the key path alike.

The daemon here is a recording stand-in behind the binding's client factory, so every
call the console issues is counted and nothing reaches a real socket.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.projection.compute import ROUTE_COLLECTIONS, build_route_projection
from eawf.kernel.projection.connection import READ_METHOD_TEMPLATE, negotiate_reconnect
from eawf.kernel.runtime.provider import ControlKind
from eawf.runtime.daemon.methods.delivery_approval import DELIVERY_SEAL_APPROVAL_METHOD
from eawf.runtime.daemon.methods.run import RUN_CONTROL_REQUEST_METHOD
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.action_menu import menu_rows
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.attention import menu_verbs, verb_available
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import NO_LINK, dispatch
from eawf.surfaces.tui.console.fixture import Detail, Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.operations import (
    CONTROL_METHOD,
    QUESTION_OPTIONS,
    SEAL_METHOD,
    UNBOUND_REASON,
    AnswerRequest,
    ConsoleOperation,
    ControlRequest,
    OperationLedger,
    OperationResult,
    OperationStatus,
    Operator,
    VerbRequest,
    address,
    binding_refusal,
    settled,
)
from eawf.surfaces.tui.console.overlays.question import ANSWERS
from eawf.surfaces.tui.console.reads import mut_reason
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import SIZES, Session, SessionSetup

GOLDEN_ROOT = Path(__file__).resolve().parents[4] / "fixtures" / "console" / "golden"
FIXTURE_DIR = GOLDEN_ROOT / "fixture"

AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
SCOPE = "EAWF"
CONTAINER = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
OPERATOR = Operator(principal="OP-0001", receipt_ref=f"{CONTAINER}/evidence/EVD-0003")

#: The action the Attention route lists first (the severest bucket), and the question.
FIRST_OPEN = "ACT-0034"
QUESTION = "ACT-0032"
RUN_KEY = "RUN-538453eb"


class _Host:
    """The dispatcher's host: a held clock and a quit that records it was asked."""

    def __init__(self) -> None:
        self.quits = 0
        self._clock = FakeClock()

    @property
    def clock(self) -> Clock:
        """Return the console clock."""
        return self._clock

    def quit(self) -> None:
        """Record that the console was asked to end."""
        self.quits += 1


class _Link:
    """A daemon link that takes every verb handed to it and records it."""

    def __init__(self) -> None:
        self.sent: list[VerbRequest] = []

    def __call__(self, request: VerbRequest) -> bool:
        self.sent.append(request)
        return True


def _fixture() -> Fixture:
    return load_fixture(FIXTURE_DIR)


def _ctx(session: Session, fixture: Fixture, link: _Link | None) -> Ctx:
    return Ctx(session=session, fixture=fixture, host=_Host(), w=120, h=30, send=link)


def _press(session: Session, fixture: Fixture, link: _Link | None, *keys: str) -> None:
    """Render the frame, then dispatch each key the way the app does."""
    for key in keys:
        render_route(View(session=session, fixture=fixture, w=120, h=30))
        dispatch(_ctx(session, fixture, link), key, False)


def _session(route: str, **fields: Any) -> Session:
    session = Session()
    session.route = route
    for name, value in fields.items():
        setattr(session, name, value)
    return session


def _register(fixture: Fixture) -> list[tuple[str, str, list[tuple[str, str]]]]:
    return [(a.id, a.state, list(a.ledger)) for a in fixture.proto.attention]


# ---------- the key path: a confirmed verb is handed to the daemon, never applied ----------


def test_confirm_sends_answer_and_leaves_fixture_untouched() -> None:
    fixture, link = _fixture(), _Link()
    session = _session("attention")
    before = _register(fixture)
    _press(session, fixture, link, "a", "Enter")
    assert link.sent == [AnswerRequest(target=FIRST_OPEN, option_id="approve")]
    assert _register(fixture) == before
    assert session.trace is not None
    assert "sent to the daemon" in session.trace


def test_confirm_deny_sends_the_decline_option() -> None:
    fixture, link = _fixture(), _Link()
    session = _session("attention")
    _press(session, fixture, link, "x", "Enter")
    assert link.sent == [AnswerRequest(target=FIRST_OPEN, option_id="decline")]


@pytest.mark.parametrize(("key", "option"), [("1", "approve"), ("2", "decline"), ("3", "repair")])
def test_question_answer_sends_the_numbered_option(key: str, option: str) -> None:
    fixture, link = _fixture(), _Link()
    session = _session("attention", overlay="question")
    before = _register(fixture)
    _press(session, fixture, link, key)
    assert link.sent == [AnswerRequest(target=QUESTION, option_id=option)]
    assert session.overlay is None
    assert _register(fixture) == before


def test_question_options_match_the_numbered_answers() -> None:
    assert len(QUESTION_OPTIONS) == len(ANSWERS)


def test_run_menu_interrupt_sends_a_control_request() -> None:
    fixture, link = _fixture(), _Link()
    session = _session("run.detail", subj_id=RUN_KEY)
    _press(session, fixture, link, ".", "n", "Enter")
    assert link.sent == [ControlRequest(target=RUN_KEY, control=ControlKind.INTERRUPT)]


def test_run_menu_cancel_sends_a_control_request() -> None:
    fixture, link = _fixture(), _Link()
    session = _session("run.detail", subj_id=RUN_KEY)
    _press(session, fixture, link, ".", "c", "Enter")
    assert link.sent == [ControlRequest(target=RUN_KEY, control=ControlKind.CANCEL)]


@pytest.mark.parametrize(
    ("key", "control"), [("c", ControlKind.CANCEL), ("n", ControlKind.RECONCILE)]
)
def test_pause_card_sends_its_control(key: str, control: ControlKind) -> None:
    fixture, link = _fixture(), _Link()
    session = _session("attention", overlay="pause")
    _press(session, fixture, link, key, "Enter")
    assert link.sent == [ControlRequest(target=pt.PAUSED_RUN, control=control)]


def test_confirm_without_a_link_sends_nothing_and_says_so() -> None:
    fixture = _fixture()
    session = _session("attention")
    before = _register(fixture)
    _press(session, fixture, None, "a", "Enter")
    assert _register(fixture) == before
    assert session.trace is not None
    assert NO_LINK in session.trace
    assert [toast.title for toast in session.toasts] == ["not sent"]


def test_confirm_on_an_empty_register_sends_nothing() -> None:
    fixture, link = _fixture(), _Link()
    empty = Fixture(
        fixture.proto.model_copy(update={"attention": ()}),
        Detail.model_validate(dict(fixture.detail)),
        fixture.registers,
        fixture.settings,
    )
    session = _session("run.detail", overlay="consequence")
    dispatch(_ctx(session, empty, link), "Enter", False)
    assert link.sent == []
    assert session.trace is not None
    assert "nothing was sent" in session.trace


# ---------- verbs no daemon mutator carries are listed, and refused with their reason ----------


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        ("z", "no daemon verb snoozes a pending action"),
        ("v", "no daemon verb resolves a notice"),
    ],
)
def test_attention_verb_without_mutator_is_refused_with_reason(key: str, reason: str) -> None:
    fixture, link = _fixture(), _Link()
    session = _session("attention")
    _press(session, fixture, link, key)
    assert session.overlay is None
    assert session.trace is not None
    assert reason in session.trace
    verb = fixture.menus.verb("attention", key)
    check = verb_available(session, fixture, verb)
    assert (check.ok, check.why) == (False, reason)
    assert link.sent == []


def test_writing_verb_without_mutator_is_disabled_in_the_menu() -> None:
    fixture = _fixture()
    session = _session("batch.detail")
    rows = "\n".join(
        menu_rows(
            menu_verbs(session, fixture),
            guard=lambda v: verb_available(session, fixture, v),
            w=160,
        )
    )
    assert "cancel batch" in rows
    assert UNBOUND_REASON in rows
    link = _Link()
    _press(session, fixture, link, ".", "c")
    assert session.overlay == "actions"
    assert session.trace is not None
    assert UNBOUND_REASON in session.trace


def test_bound_run_verbs_stay_available_in_the_menu() -> None:
    fixture = _fixture()
    session = _session("run.detail", subj_id=RUN_KEY)
    for key in ("n", "c"):
        check = verb_available(session, fixture, fixture.menus.verb("run.detail", key))
        assert check.ok, check.why


def test_verb_refused_offline_names_state_reason() -> None:
    fixture, link = _fixture(), _Link()
    session = _session("attention", conn="OFFLINE SNAPSHOT")
    reason = mut_reason(session, fixture)
    for key in ("a", "z"):
        check = verb_available(session, fixture, fixture.menus.verb("attention", key))
        assert (check.ok, check.why) == (False, reason)
    _press(session, fixture, link, "a")
    assert session.trace is not None
    assert reason in session.trace
    # a card opened while live and confirmed after the link dropped is refused, not sent
    session = _session("attention")
    _press(session, fixture, link, "a")
    session.conn = "DISCONNECTED"
    _press(session, fixture, link, "Enter")
    assert link.sent == []
    assert session.trace is not None
    assert mut_reason(session, fixture) in session.trace


@pytest.mark.parametrize(
    ("kind", "verb", "bound"),
    [
        ("attention", "answer", True),
        ("attention", "deny", True),
        ("attention", "snooze", False),
        ("run.detail", "interrupt", True),
        ("run", "reconcile", True),
        ("run.detail", "retry", False),
        ("batch.detail", "cancel", False),
        ("", "", False),
    ],
)
def test_binding_refusal_names_only_unbound_verbs(kind: str, verb: str, bound: bool) -> None:
    assert (binding_refusal(kind, verb) == "") is bound


# ---------- the operation contract ----------


def test_method_spellings_match_the_daemon() -> None:
    assert SEAL_METHOD == DELIVERY_SEAL_APPROVAL_METHOD
    assert CONTROL_METHOD == RUN_CONTROL_REQUEST_METHOD


def test_answer_request_refuses_an_option_not_offered() -> None:
    with pytest.raises(ValueError, match="is not one of"):
        AnswerRequest(target=FIRST_OPEN, option_id="snooze")
    with pytest.raises(ValueError, match="is not one of"):
        AnswerRequest(target=FIRST_OPEN, option_id="")


def test_address_names_answer_by_its_operation_id() -> None:
    op = address(
        AnswerRequest(target=FIRST_OPEN, option_id="approve"),
        urn=f"{CONTAINER}/pending-action/{FIRST_OPEN}",
        revision=2,
        operator=OPERATOR,
    )
    assert isinstance(op, ConsoleOperation)
    assert op.method == SEAL_METHOD
    assert op.params["idempotency_key"] == op.operation_id
    assert op.params["expected_revision"] == 2
    assert op.params["resolver"] == {"principal_kind": "human", "principal_id": "OP-0001"}
    again = address(
        AnswerRequest(target=FIRST_OPEN, option_id="approve"),
        urn=f"{CONTAINER}/pending-action/{FIRST_OPEN}",
        revision=2,
        operator=OPERATOR,
    )
    assert isinstance(again, ConsoleOperation)
    assert again.operation_id != op.operation_id


def test_address_names_control_by_its_request_ref() -> None:
    op = address(
        ControlRequest(target=RUN_KEY, control=ControlKind.CANCEL),
        urn=f"{CONTAINER}/run/{RUN_KEY}",
        revision=1,
        operator=Operator(principal="OP-0001"),
    )
    assert isinstance(op, ConsoleOperation)
    assert op.method == CONTROL_METHOD
    assert op.params["control_request_ref"] == op.operation_id
    assert op.operation_id.startswith("CTL-")
    assert op.params["control"] == "cancel"


def test_address_refuses_an_answer_with_no_receipt() -> None:
    result = address(
        AnswerRequest(target=FIRST_OPEN, option_id="approve"),
        urn=f"{CONTAINER}/pending-action/{FIRST_OPEN}",
        revision=1,
        operator=Operator(principal="OP-0001"),
    )
    assert isinstance(result, OperationResult)
    assert result.status is OperationStatus.REFUSED
    assert result.operation_id is None


def _op(operation_id: str = "console-0001") -> ConsoleOperation:
    return ConsoleOperation(
        operation_id=operation_id, method=SEAL_METHOD, params={}, target=FIRST_OPEN
    )


@pytest.mark.parametrize(
    ("answer", "status"),
    [
        ({"outcome": "sealed", "reason": "answered"}, OperationStatus.APPLIED),
        ({"outcome": "superseded", "reason": "lost"}, OperationStatus.SUPERSEDED),
        ({"disposition": "requesting"}, OperationStatus.APPLIED),
        ({}, OperationStatus.APPLIED),
    ],
)
def test_settled_reads_the_daemon_outcome(answer: dict[str, Any], status: OperationStatus) -> None:
    assert settled(_op(), answer).status is status


def test_ledger_holds_an_operation_until_it_is_answered() -> None:
    ledger = OperationLedger()
    assert ledger.outstanding() == ()
    op = _op()
    ledger.open(op)
    ledger.settle(
        OperationResult(
            operation_id=op.operation_id,
            target=op.target,
            status=OperationStatus.OUTSTANDING,
            detail="",
        )
    )
    assert ledger.outstanding() == (op,)
    ledger.settle(settled(op, {"outcome": "sealed"}))
    assert ledger.outstanding() == ()


def test_ledger_refuses_a_duplicate_and_an_unknown_id() -> None:
    ledger = OperationLedger()
    ledger.open(_op())
    with pytest.raises(ValueError, match="already outstanding"):
        ledger.open(_op())
    with pytest.raises(KeyError):
        ledger.settle(settled(_op("console-0002"), {}))
    with pytest.raises(KeyError):
        ledger.settle(
            OperationResult(operation_id=None, target="", status=OperationStatus.REFUSED, detail="")
        )


# ---------- the seam sends through the one binding, addressed from held rows ----------


def _projection(route: str) -> dict[str, Any]:
    document: dict[str, Any] = {}
    if route == "attention":
        document = {
            "pending_action": {
                FIRST_OPEN: {
                    "urn": f"{CONTAINER}/pending-action/{FIRST_OPEN}",
                    "revision": 3,
                    "status": "WAITING",
                }
            }
        }
    elif route == "run.detail":
        document = {
            "run": {
                RUN_KEY: {"urn": f"{CONTAINER}/run/{RUN_KEY}", "revision": 1, "status": "RUNNING"}
            }
        }
    return build_route_projection(
        route=route, document=document, cursor=5, scope_id=SCOPE, generated_at=AT
    ).model_dump(mode="json")


class _Daemon:
    """A daemon that serves route reads and records every write it is sent.

    Attributes:
        writes: ``(method, params)`` of every non-read call, in order.
        fail_with: Raised by the next write instead of answering it.
    """

    def __init__(self) -> None:
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.fail_with: Exception | None = None
        self._reads = {READ_METHOD_TEMPLATE.format(route=r): r for r in ROUTE_COLLECTIONS}

    def client(self) -> _Client:
        return _Client(self)

    def answer(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method in self._reads:
            return _projection(self._reads[method])
        if method.endswith(".reconnect"):
            route = method.split(".")[1]
            negotiation = negotiate_reconnect(
                route=route, client_cursor=5, server_cursor=5, retained=()
            )
            return {"negotiation": negotiation.model_dump(mode="json"), "patches": []}
        self.writes.append((method, params))
        if self.fail_with is not None:
            error, self.fail_with = self.fail_with, None
            raise error
        if method == SEAL_METHOD:
            return {"outcome": "sealed", "reason": f"{FIRST_OPEN} was answered"}
        return {"disposition": "requesting"}


class _Client:
    def __init__(self, daemon: _Daemon) -> None:
        self._daemon = daemon

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._daemon.answer(method, dict(params or {}))


def _seam(daemon: _Daemon, *, route: str = "attention", **options: Any) -> ProjectionSeam:
    return ProjectionSeam(
        route=route,
        scope_id=SCOPE,
        state_path=None,
        clock=lambda: AT,
        daemon_client_factory=daemon.client,
        **options,
    )


def test_confirm_resolves_pending_action_via_daemon() -> None:
    """Gate-fire proof: one seal under an operation id, and the fixture never moves."""
    daemon = _Daemon()
    seam = _seam(daemon, operator=OPERATOR)
    fixture = _fixture()
    before = _register(fixture)

    async def drive() -> ConsoleApp:
        app = ConsoleApp(fixture, FakeClock(), seam=seam)
        app.reset(SessionSetup(route="attention"))
        async with app.run_test(size=SIZES[1]) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.press_key("a")
            app.press_key("Enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
        return app

    app = asyncio.run(drive())
    assert len(daemon.writes) == 1
    method, params = daemon.writes[0]
    assert method == SEAL_METHOD
    assert params["idempotency_key"].startswith("console-")
    assert params["urn"] == f"{CONTAINER}/pending-action/{FIRST_OPEN}"
    assert params["expected_revision"] == 3
    assert params["option_id"] == "approve"
    assert params["receipt_ref"] == OPERATOR.receipt_ref
    assert seam.outstanding == ()
    assert _register(fixture) == before
    assert app.session.log[0].key == "daemon"
    assert app.session.log[0].note.startswith("applied")


def test_run_control_reaches_daemon_through_the_seam() -> None:
    daemon = _Daemon()
    seam = _seam(daemon, route="run.detail", operator=Operator(principal="OP-0001"))

    async def drive() -> OperationResult:
        await seam.load()
        return await seam.request(ControlRequest(target=RUN_KEY, control=ControlKind.CANCEL))

    result = asyncio.run(drive())
    assert result.status is OperationStatus.APPLIED
    ((method, params),) = daemon.writes
    assert method == CONTROL_METHOD
    assert params["control_request_ref"] == result.operation_id
    assert params["urn"] == f"{CONTAINER}/run/{RUN_KEY}"


def test_seam_with_no_operator_refuses_and_sends_nothing() -> None:
    daemon = _Daemon()
    seam = _seam(daemon)

    async def drive() -> OperationResult:
        await seam.load()
        return await seam.request(AnswerRequest(target=FIRST_OPEN, option_id="approve"))

    result = asyncio.run(drive())
    assert result.status is OperationStatus.REFUSED
    assert "no operator principal" in result.detail
    assert daemon.writes == []


def test_seam_refuses_a_target_it_holds_no_row_for() -> None:
    daemon = _Daemon()
    seam = _seam(daemon, operator=OPERATOR)

    async def drive() -> OperationResult:
        await seam.load()
        return await seam.request(AnswerRequest(target="ACT-9999", option_id="approve"))

    result = asyncio.run(drive())
    assert result.status is OperationStatus.REFUSED
    assert "ACT-9999 is in no projection" in result.detail
    assert daemon.writes == []


def test_seam_closes_an_operation_the_daemon_refused() -> None:
    daemon = _Daemon()
    daemon.fail_with = DaemonRpcError(-32602, "validation_failed: identity_not_found: EVD-0003")
    seam = _seam(daemon, operator=OPERATOR)

    async def drive() -> OperationResult:
        await seam.load()
        return await seam.request(AnswerRequest(target=FIRST_OPEN, option_id="approve"))

    result = asyncio.run(drive())
    assert result.status is OperationStatus.REFUSED
    assert "identity_not_found" in result.detail
    assert seam.outstanding == ()


def test_seam_keeps_an_unanswered_operation_outstanding() -> None:
    daemon = _Daemon()
    daemon.fail_with = ConnectionError("the reply was lost")
    seam = _seam(daemon, operator=OPERATOR)

    async def drive() -> OperationResult:
        await seam.load()
        return await seam.request(AnswerRequest(target=FIRST_OPEN, option_id="approve"))

    result = asyncio.run(drive())
    assert result.status is OperationStatus.OUTSTANDING
    assert [op.operation_id for op in seam.outstanding] == [result.operation_id]


def test_reconnect_keeps_an_operation_whose_answer_is_lost_again() -> None:
    daemon = _Daemon()
    seam = _seam(daemon, operator=OPERATOR)

    async def drive() -> tuple[OperationResult, ...]:
        await seam.load()
        daemon.fail_with = ConnectionError("the reply was lost")
        await seam.request(AnswerRequest(target=FIRST_OPEN, option_id="approve"))
        daemon.fail_with = ConnectionError("lost again")
        return (await seam.reconnect()).reconciled

    reconciled = asyncio.run(drive())
    assert [r.status for r in reconciled] == [OperationStatus.OUTSTANDING]
    assert len(seam.outstanding) == 1
    ids = {params["idempotency_key"] for _, params in daemon.writes}
    assert len(daemon.writes) == 2
    assert ids == {seam.outstanding[0].operation_id}


def test_reconnect_with_nothing_outstanding_reconciles_nothing() -> None:
    daemon = _Daemon()
    seam = _seam(daemon, operator=OPERATOR)

    async def drive() -> tuple[OperationResult, ...]:
        await seam.load()
        return (await seam.reconnect()).reconciled

    assert asyncio.run(drive()) == ()
    assert daemon.writes == []
