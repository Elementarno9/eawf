"""The console's write path, as a linked console acting as somebody or nobody sees it.

Three promises are held here. A console linked to a daemon but acting as nobody shows
every bound verb refused before one is chosen, naming why, rather than letting the
operator confirm a write that could never be attributed. An answer is recorded under an
evidence receipt of its own: a receipt that already records the answer to one pending
action is never cited for another. And the attention key path and the header count read
only what the link holds: a linked console holding the prototype registers and no read
model sends nothing and shows no badge.

The last promise is proved twice, green and red. The red half plants the prototype
fallback the port used to carry and shows the same check fails under it, so the check
is known to be able to fire.

The daemon is a recording stand-in behind the binding's client factory, so every call
the console issues is counted and nothing reaches a real socket.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.projection.compute import (
    ROUTE_COLLECTIONS,
    RouteProjection,
    build_route_projection,
)
from eawf.kernel.projection.connection import READ_METHOD_TEMPLATE
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.tui.console import dispatch as dispatch_mod
from eawf.surfaces.tui.console import frame as frame_mod
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.attention import (
    is_mutation,
    menu_verbs,
    open_count,
    verb_available,
)
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.drawers import action_rows
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View, header, needs_count
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.operations import (
    NO_PRINCIPAL_REASON,
    AnswerRequest,
    OperationStatus,
    Operator,
    VerbRequest,
    binding_refusal,
)
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import Session

GOLDEN_ROOT = Path(__file__).resolve().parents[4] / "fixtures" / "console" / "golden"
FIXTURE_DIR = GOLDEN_ROOT / "fixture"

AT = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
SCOPE = "EAWF"
CONTAINER = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
RECEIPT = f"{CONTAINER}/evidence/EVD-0003"
OTHER_RECEIPT = f"{CONTAINER}/evidence/EVD-0004"

#: The two pending actions the stand-in daemon's Attention projection holds.
FIRST = "ACT-0034"
SECOND = "ACT-0032"


class _Host:
    """The dispatcher's host: a held clock and a quit nobody asks for."""

    def __init__(self) -> None:
        self._clock = FakeClock()

    @property
    def clock(self) -> Clock:
        """Return the console clock."""
        return self._clock

    def quit(self) -> None:
        """End nothing; no test here quits."""


class _Link:
    """A daemon link that takes every verb handed to it and records it."""

    def __init__(self) -> None:
        self.sent: list[VerbRequest] = []

    def __call__(self, request: VerbRequest) -> bool:
        self.sent.append(request)
        return True


def _attention_projection() -> dict[str, Any]:
    rows = {
        key: {"urn": f"{CONTAINER}/pending-action/{key}", "revision": 3, "status": "WAITING"}
        for key in (FIRST, SECOND)
    }
    return build_route_projection(
        route="attention",
        document={"pending_action": rows},
        cursor=5,
        scope_id=SCOPE,
        generated_at=AT,
    ).model_dump(mode="json")


class _Daemon:
    """A daemon that serves route reads and records every write it is sent.

    Attributes:
        writes: ``(method, params)`` of every non-read call, in order.
        refuse_next: Refuse the next write with this error instead of answering it.
    """

    def __init__(self) -> None:
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.refuse_next: DaemonRpcError | None = None
        self._reads = {READ_METHOD_TEMPLATE.format(route=r): r for r in ROUTE_COLLECTIONS}

    def client(self) -> _Client:
        return _Client(self)

    def answer(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        route = self._reads.get(method)
        if route == "attention":
            return _attention_projection()
        if route is not None:
            return build_route_projection(
                route=route, document={}, cursor=5, scope_id=SCOPE, generated_at=AT
            ).model_dump(mode="json")
        self.writes.append((method, params))
        if self.refuse_next is not None:
            error, self.refuse_next = self.refuse_next, None
            raise error
        return {"outcome": "sealed", "reason": f"{params['urn']} was answered"}


class _Client:
    def __init__(self, daemon: _Daemon) -> None:
        self._daemon = daemon

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._daemon.answer(method, dict(params or {}))


def _seam(daemon: _Daemon, *, operator: Operator | None) -> ProjectionSeam:
    return ProjectionSeam(
        route="attention",
        scope_id=SCOPE,
        state_path=None,
        clock=lambda: AT,
        daemon_client_factory=daemon.client,
        operator=operator,
    )


def _live_fixture() -> Fixture:
    return Fixture.from_chrome(load_chrome())


def _prototype() -> Fixture:
    return load_fixture(FIXTURE_DIR)


def _session(route: str, **fields: Any) -> Session:
    session = Session()
    session.route = route
    for name, value in fields.items():
        setattr(session, name, value)
    return session


def _bound_verbs(fixture: Fixture) -> list[tuple[str, str]]:
    """Return every ``(route, verb)`` that writes through a daemon mutator."""
    bound = []
    for route in REGISTRY.ids:
        for verb in fixture.menus.verbs(route):
            writes = is_mutation(_session(route), verb)
            if writes and verb.available and binding_refusal(route, verb.verb) == "":
                bound.append((route, verb.verb))
    return bound


# ---------- a console acting as nobody greys every bound verb before one is chosen ----------


def test_the_catalog_binds_verbs_to_grey() -> None:
    """The next check is vacuous unless some verb is bound to a daemon mutator."""
    bound = _bound_verbs(_live_fixture())
    assert {"attention", "run.detail"} <= {route for route, _verb in bound}


def test_no_principal_greys_every_bound_verb_before_selection() -> None:
    fixture = _live_fixture()
    for route, name in _bound_verbs(fixture):
        session = _session(route)
        verb = next(v for v in fixture.menus.verbs(route) if v.verb == name)
        check = verb_available(session, fixture, verb, principal_refusal=NO_PRINCIPAL_REASON)
        assert (check.ok, check.why) == (False, NO_PRINCIPAL_REASON), (route, name)
        assert verb_available(session, fixture, verb).ok, (route, name)


def test_the_action_menu_draws_the_reason_beside_each_bound_verb() -> None:
    fixture = _live_fixture()
    session = _session("run.detail")
    view = View(
        session=session, fixture=fixture, w=160, h=40, principal_refusal=NO_PRINCIPAL_REASON
    )
    rows = action_rows(view)
    for verb in menu_verbs(session, fixture):
        if binding_refusal("run.detail", verb.verb) == "" and verb.mutates:
            (row,) = [r for r in rows if f" {verb.verb} " in r]
            assert "no operator principal" in row, row


def test_a_greyed_verb_opens_no_card_and_sends_nothing() -> None:
    fixture, link = _prototype(), _Link()
    session = _session("run.detail", subj_id="RUN-538453eb")
    ctx = Ctx(
        session=session,
        fixture=fixture,
        host=_Host(),
        w=120,
        h=30,
        send=link,
        principal_refusal=NO_PRINCIPAL_REASON,
    )
    for key in (".", "c", "Enter"):
        dispatch(ctx, key, False)
    assert link.sent == []
    assert session.overlay != "consequence"


def test_an_attention_letter_is_refused_with_the_principal_reason() -> None:
    fixture, link = _prototype(), _Link()
    session = _session("attention")
    ctx = Ctx(
        session=session,
        fixture=fixture,
        host=_Host(),
        w=120,
        h=30,
        send=link,
        principal_refusal=NO_PRINCIPAL_REASON,
    )
    dispatch(ctx, "a", False)
    assert session.overlay is None
    assert session.trace is not None
    assert NO_PRINCIPAL_REASON in session.trace


@pytest.mark.parametrize(
    ("operator", "expected"),
    [(None, NO_PRINCIPAL_REASON), (Operator(principal="OP-0001"), "")],
)
def test_the_app_states_the_principal_refusal_from_its_seam(
    operator: Operator | None, expected: str
) -> None:
    app = ConsoleApp(clock=FakeClock(), seam=_seam(_Daemon(), operator=operator))
    assert app.view().principal_refusal == expected


def test_a_console_with_no_link_greys_nothing_ahead() -> None:
    """With no link nothing is attributed at all; the confirm path says so instead."""
    assert ConsoleApp(_prototype(), FakeClock()).view().principal_refusal == ""


# ---------- one evidence receipt records one answer ----------


def _answer(seam: ProjectionSeam, *targets: str) -> list[Any]:
    async def drive() -> list[Any]:
        await seam.load()
        return [
            await seam.request(AnswerRequest(target=target, option_id="approve"))
            for target in targets
        ]

    return asyncio.run(drive())


def test_each_answered_item_is_sealed_under_its_own_receipt() -> None:
    daemon = _Daemon()
    seam = _seam(daemon, operator=Operator(principal="OP-0001", receipt_ref=RECEIPT))
    first, second = _answer(seam, FIRST, SECOND)
    assert first.status is OperationStatus.APPLIED
    assert second.status is OperationStatus.REFUSED
    assert second.operation_id is None
    assert f"already records the answer to {FIRST}" in second.detail
    cited = [
        (params["urn"].rsplit("/", 1)[1], params["receipt_ref"]) for _m, params in daemon.writes
    ]
    assert cited == [(FIRST, RECEIPT)]


def test_answering_the_same_item_again_cites_its_receipt_again() -> None:
    """A retry is the same answer, so it may cite the receipt that answer is filed under."""
    daemon = _Daemon()
    seam = _seam(daemon, operator=Operator(principal="OP-0001", receipt_ref=RECEIPT))
    results = _answer(seam, FIRST, FIRST)
    assert [r.status for r in results] == [OperationStatus.APPLIED, OperationStatus.APPLIED]
    assert len(daemon.writes) == 2


def test_a_refused_answer_leaves_its_receipt_unspent() -> None:
    daemon = _Daemon()
    daemon.refuse_next = DaemonRpcError(-32602, "validation_failed: stale revision")
    seam = _seam(daemon, operator=Operator(principal="OP-0001", receipt_ref=RECEIPT))
    first, second = _answer(seam, FIRST, SECOND)
    assert first.status is OperationStatus.REFUSED
    assert second.status is OperationStatus.APPLIED
    assert [params["receipt_ref"] for _m, params in daemon.writes] == [RECEIPT, RECEIPT]


def test_two_consoles_with_two_receipts_answer_two_items() -> None:
    """The boundary the rule allows: distinct receipts, distinct answers, both sent."""
    daemon = _Daemon()
    one = _seam(daemon, operator=Operator(principal="OP-0001", receipt_ref=RECEIPT))
    two = _seam(daemon, operator=Operator(principal="OP-0001", receipt_ref=OTHER_RECEIPT))
    (a,) = _answer(one, FIRST)
    (b,) = _answer(two, SECOND)
    assert (a.status, b.status) == (OperationStatus.APPLIED, OperationStatus.APPLIED)
    assert [params["receipt_ref"] for _m, params in daemon.writes] == [RECEIPT, OTHER_RECEIPT]


# ---------- the key path and the badge read only what the link holds ----------


def _confirm_sends(fixture: Fixture, attention: RouteProjection | None) -> list[VerbRequest]:
    """Return what confirming an answer on the Attention route hands the link."""
    link = _Link()
    session = _session("attention", overlay="consequence", verb="a")
    ctx = Ctx(
        session=session, fixture=fixture, host=_Host(), w=120, h=30, send=link, attention=attention
    )
    dispatch(ctx, "Enter", False)
    return link.sent


def _linked_badge(fixture: Fixture) -> tuple[int, str]:
    """Return the count and header a linked console with no read model draws."""
    view = View(session=_session("activity"), fixture=fixture, w=120, h=30, linked=True)
    return needs_count(view), header(view, " Eä ▸ Activity")


def _reads_only_what_is_held(fixture: Fixture) -> bool:
    """Return whether a linked console with no read model sends nothing and shows no badge."""
    count, row = _linked_badge(fixture)
    return _confirm_sends(fixture, None) == [] and count == 0 and "NEEDS YOU" not in row


def test_prototype_registers_and_no_read_model_send_nothing_and_show_no_badge() -> None:
    """Gate-fire proof, green half."""
    fixture = _prototype()
    assert fixture.proto.attention, "the check is vacuous over an empty prototype register"
    assert _reads_only_what_is_held(fixture)


def test_a_planted_prototype_fallback_reds_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate-fire proof, red half: the removed fallbacks, planted back, fail the same check."""
    fixture = _prototype()

    def planted_count(view: View) -> int:
        return open_count(view.fixture) if view.attention is None else 0

    def planted_confirm(ctx: Ctx) -> None:
        ctx.s.overlay = None
        action = ctx.fixture.proto.attention[0]
        ctx.dispatch_write(AnswerRequest(target=action.id, option_id="approve"))

    monkeypatch.setattr(frame_mod, "needs_count", planted_count)
    assert not _reads_only_what_is_held(fixture)
    monkeypatch.undo()
    monkeypatch.setattr(dispatch_mod, "_confirm", planted_confirm)
    assert not _reads_only_what_is_held(fixture)


def test_confirm_answers_the_selected_row_of_the_held_projection() -> None:
    held = RouteProjection.model_validate(_attention_projection())
    sent = _confirm_sends(_live_fixture(), held)
    assert sent == [AnswerRequest(target=held.rows[0].key, option_id="approve")]


def test_confirm_sends_nothing_when_the_selected_id_is_gone() -> None:
    """A selection the projection no longer holds does not slide onto a neighbour."""
    held = RouteProjection.model_validate(_attention_projection())
    link = _Link()
    session = _session("attention", overlay="consequence", verb="a", sel_id="ACT-9999")
    ctx = Ctx(
        session=session,
        fixture=_live_fixture(),
        host=_Host(),
        w=120,
        h=30,
        send=link,
        attention=held,
    )
    dispatch(ctx, "Enter", False)
    assert link.sent == []


def test_an_unlinked_prototype_console_still_counts_its_own_register() -> None:
    """The tracked golden contract replays this mode, so its badge is unchanged."""
    fixture = _prototype()
    view = View(session=_session("activity"), fixture=fixture, w=120, h=30)
    assert needs_count(view) == open_count(fixture) > 0
