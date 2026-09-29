"""Budget notices on the Attention route: listed after the actions, snoozed or resolved.

A notice blocks nothing and asks nothing, so the only verbs it takes are a snooze and a
resolve. Each is previewed on the consequence card and sent to the notice ledger's
disposition verb at the revision the operator was shown; the inbox is re-read afterwards
because no patch carries the ledger.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.projection.compute import ROUTE_COLLECTIONS, build_route_projection
from eawf.kernel.projection.connection import READ_METHOD_TEMPLATE
from eawf.runtime.budget.notices import BudgetThresholdNotice, notice_key_for
from eawf.surfaces.tui.console.app import compose_frame
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.mutation import Card, notice_card
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.notices import notice_cells, notice_of, short_key
from eawf.surfaces.tui.console.operations import (
    NOTICE_DISPOSE_METHOD,
    NOTICE_LIST_METHOD,
    SNOOZE_FOR,
    ConsoleOperation,
    NoticeRequest,
    OperationStatus,
    Operator,
    address_notice,
    settled,
)
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.workflow.decision_question import QUESTION_DECISIONS_METHOD
from tests.tui.surfaces.tui.console import test_native_route_bodies as bodies

from .overlay_support import Host, Link

AT = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
ME = bodies.ME
RUN = "RUN-538453eb"


def _notice(*, revision: int = 2, scope: str = RUN) -> BudgetThresholdNotice:
    return BudgetThresholdNotice(
        notice_key=notice_key_for(scope_id=scope, axis="tokens", basis="hard_limit"),
        scope_id=scope,
        axis="tokens",
        basis="hard_limit",
        highest_band="limit_reached",
        severity="critical",
        observed_value=1200,
        budget_value=1000,
        revision=revision,
        opened_at=AT,
        last_observed_at=AT,
        audience=(ME,),
    )


NOTICE = _notice()


def _view(
    *, notices: tuple[BudgetThresholdNotice, ...] = (NOTICE,), sel_id: str | None = None
) -> View:
    view = bodies._view("attention")
    view.session.sel_id = sel_id
    return _with(view, notices)


def _with(view: View, notices: tuple[BudgetThresholdNotice, ...]) -> View:
    return dataclasses.replace(view, notices=notices)


def _ctx(view: View, link: Link | None = None) -> Ctx:
    return Ctx(
        session=view.session,
        fixture=view.fixture,
        host=Host(),
        w=view.w,
        h=view.h,
        notices=view.notices,
        principal=view.principal,
        send=link,
    )


def _press(view: View, key: str, link: Link | None = None) -> None:
    compose_frame(view)
    dispatch(_ctx(view, link), key, False)


# ---------- the frame lists a notice after the actions, under its own bucket ----------


def test_attention_lists_a_held_notice_under_over_budget() -> None:
    rows = compose_frame(_view())
    head = next(i for i, row in enumerate(rows) if row.startswith(" OVER BUDGET  1"))
    assert any(short_key(NOTICE) in row for row in rows[head + 1 : head + 2])


def test_attention_with_no_notice_draws_no_notice_group() -> None:
    rows = compose_frame(_view(notices=()))
    assert not any(row.startswith(" OVER BUDGET") for row in rows)


def test_attention_selected_notice_says_what_it_measured_and_blocks_nothing() -> None:
    rows = compose_frame(_view(sel_id=NOTICE.notice_key))
    assert any("1200 of 1000 tokens · revision 2 · blocks nothing" in row for row in rows)


def test_attention_keybar_on_a_notice_offers_snooze_and_resolve_only() -> None:
    bar = compose_frame(_view(sel_id=NOTICE.notice_key))[-1]
    assert "snooze" in bar and "resolve" in bar
    assert "answer" not in bar and "deny" not in bar


def test_notice_cells_show_the_short_key_and_what_crossed() -> None:
    assert notice_cells(NOTICE)[:3] == [short_key(NOTICE), f"{RUN} tokens limit reached", "OPEN"]
    assert len(short_key(NOTICE)) == 8


def test_notice_of_finds_by_full_key_and_nothing_else() -> None:
    assert notice_of((NOTICE,), NOTICE.notice_key) is NOTICE
    assert notice_of((NOTICE,), short_key(NOTICE)) is None
    assert notice_of((), NOTICE.notice_key) is None
    assert notice_of((NOTICE,), None) is None


# ---------- a snooze or resolve previews, then sends the disposition ----------


@pytest.mark.parametrize(("key", "disposition"), [("z", "snooze"), ("v", "resolve")])
def test_notice_verb_previews_then_sends_the_disposition(key: str, disposition: str) -> None:
    view, link = _view(sel_id=NOTICE.notice_key), Link()
    _press(view, key, link)
    card = view.session.mutation
    assert view.session.overlay == "consequence" and isinstance(card, Card)
    assert (card.kind, card.action) == ("notice", disposition)
    _press(view, "Enter", link)
    [sent] = link.sent
    assert isinstance(sent, NoticeRequest)
    assert (sent.target, sent.disposition, sent.revision) == (NOTICE.notice_key, disposition, 2)
    assert (sent.snooze_until is not None) == (disposition == "snooze")


@pytest.mark.parametrize(("key", "verb"), [("a", "answer"), ("x", "deny")])
def test_notice_refuses_an_answer_or_a_denial(key: str, verb: str) -> None:
    view, link = _view(sel_id=NOTICE.notice_key), Link()
    _press(view, key, link)
    assert view.session.overlay is None
    assert view.session.trace is not None
    assert f"a notice has nothing to {verb}" in view.session.trace
    assert link.sent == []


def test_notice_card_reloads_when_the_notice_escalated_before_confirming() -> None:
    view, link = _view(sel_id=NOTICE.notice_key), Link()
    _press(view, "v", link)
    moved = _with(view, (_notice(revision=3),))
    _press(moved, "Enter", link)
    assert link.sent == []
    card = moved.session.mutation
    assert isinstance(card, Card) and "reloaded" in card.note
    assert card.items[0].revision == 3


def test_notice_card_snooze_lapses_one_snooze_length_after_the_wall_clock() -> None:
    card = notice_card(NOTICE, "z", principal=ME, now=0.0, wall=AT)
    request = card.items[0].request
    assert isinstance(request, NoticeRequest)
    assert request.snooze_until == AT + SNOOZE_FOR
    assert notice_card(NOTICE, "v", principal=ME, now=0.0, wall=AT).items[0].request == (
        NoticeRequest(target=NOTICE.notice_key, disposition="resolve", revision=2)
    )


# ---------- the request, its addressing and its settlement ----------


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        ({"disposition": "resolve", "revision": 0}, "revision must be positive"),
        ({"disposition": "snooze", "revision": 1}, "a snooze names when it lapses"),
        ({"disposition": "resolve", "revision": 1, "snooze_until": AT}, "only a snooze"),
    ],
)
def test_notice_request_refuses_what_the_ledger_would_refuse(
    fields: dict[str, Any], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        NoticeRequest(target=NOTICE.notice_key, **fields)


def test_address_notice_names_the_principal_revision_and_deadline() -> None:
    request = NoticeRequest(
        target=NOTICE.notice_key, disposition="snooze", revision=2, snooze_until=AT
    )
    operation = address_notice(request, operator=Operator(principal=ME))
    assert operation.method == NOTICE_DISPOSE_METHOD
    assert dict(operation.params) == {
        "notice_key": NOTICE.notice_key,
        "principal": ME,
        "disposition": "snooze",
        "expected_revision": 2,
        "snooze_until": AT.isoformat(),
    }
    assert operation.operation_id.startswith("NTC-")


def test_settled_notice_disposition_is_applied_and_stops_no_work() -> None:
    operation = ConsoleOperation(
        operation_id="NTC-1",
        method=NOTICE_DISPOSE_METHOD,
        params={"disposition": "resolve"},
        target=NOTICE.notice_key,
    )
    result = settled(operation, {"notice": {"status": "RESOLVED"}})
    assert result.status is OperationStatus.APPLIED
    assert result.detail.endswith("resolved · the notice is resolved · no work was stopped")


# ---------- the seam reads the inbox and sends the disposition ----------


class _Daemon:
    """A daemon serving route reads, one principal's inbox and the disposition verb."""

    def __init__(self, notices: list[BudgetThresholdNotice]) -> None:
        self.notices = notices
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._reads = {READ_METHOD_TEMPLATE.format(route=r): r for r in ROUTE_COLLECTIONS}

    def client(self) -> _Client:
        return _Client(self)

    def answer(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        route = self._reads.get(method)
        if route is not None:
            return build_route_projection(
                route=route, document={}, cursor=5, scope_id="EAWF", generated_at=AT
            ).model_dump(mode="json")
        if method == QUESTION_DECISIONS_METHOD:
            return {"decisions": []}
        self.calls.append((method, params))
        if method == NOTICE_LIST_METHOD:
            return {"active": [n.model_dump(mode="json") for n in self.notices]}
        resolved = self.notices.pop()
        return {"notice": {**resolved.model_dump(mode="json"), "status": "RESOLVED"}}


class _Client:
    def __init__(self, daemon: _Daemon) -> None:
        self._daemon = daemon

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._daemon.answer(method, dict(params or {}))


def _seam(daemon: _Daemon, operator: Operator | None) -> ProjectionSeam:
    return ProjectionSeam(
        route="attention",
        scope_id="EAWF",
        state_path=None,
        clock=lambda: AT,
        daemon_client_factory=daemon.client,
        operator=operator,
    )


def test_seam_owes_the_inbox_on_attention_only_when_it_acts_as_someone() -> None:
    assert NOTICE_LIST_METHOD in _seam(_Daemon([]), Operator(principal=ME)).owed()
    assert NOTICE_LIST_METHOD not in _seam(_Daemon([]), None).owed()


def test_seam_holds_the_inbox_it_read() -> None:
    seam = _seam(_Daemon([NOTICE]), Operator(principal=ME))
    asyncio.run(seam.sync())
    assert seam.notices == (NOTICE,)
    assert NOTICE_LIST_METHOD not in seam.owed()


def test_seam_disposes_a_held_notice_and_rereads_the_inbox() -> None:
    daemon = _Daemon([NOTICE])
    seam = _seam(daemon, Operator(principal=ME))
    asyncio.run(seam.sync())
    request = NoticeRequest(target=NOTICE.notice_key, disposition="resolve", revision=2)
    result = asyncio.run(seam.request(request))
    assert result.status is OperationStatus.APPLIED
    method, params = daemon.calls[1]
    assert (method, params["notice_key"], params["principal"]) == (
        NOTICE_DISPOSE_METHOD,
        NOTICE.notice_key,
        ME,
    )
    assert daemon.calls[-1][0] == NOTICE_LIST_METHOD
    assert seam.notices == ()


def test_seam_refuses_unsent_a_notice_it_does_not_hold() -> None:
    daemon = _Daemon([])
    seam = _seam(daemon, Operator(principal=ME))
    request = NoticeRequest(target=NOTICE.notice_key, disposition="resolve", revision=2)
    result = asyncio.run(seam.request(request))
    assert result.status is OperationStatus.REFUSED
    assert "nothing was sent" in result.detail
    assert daemon.calls == []


def test_seam_load_notices_acting_as_nobody_raises_value_error() -> None:
    with pytest.raises(ValueError, match="acts as nobody"):
        asyncio.run(_seam(_Daemon([]), None).load_notices())
