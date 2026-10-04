"""A marked selection released from the console is one daemon bulk operation.

Marking several claimed Tasks and releasing them opens one card naming every target;
confirming it sends one ``runtime.bulk.preview`` then one ``runtime.bulk.control`` rather
than a request per Task, and every target keeps its own row, filled from its own item
result. An unknown row is reconciled through ``runtime.bulk.reconcile`` under the same
id. The daemon is a recording stand-in behind the seam's client factory.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.delivery.bulk import BulkItemState, BulkVerb
from eawf.kernel.projection.compute import (
    ROUTE_COLLECTIONS,
    ProjectionRow,
    build_route_projection,
)
from eawf.kernel.projection.connection import read_method
from eawf.kernel.runtime.control import ControlDisposition
from eawf.kernel.state.epoch2.consequence import MUTATIONS_BY_METHOD
from eawf.runtime.daemon.methods import bulk as daemon_bulk
from eawf.runtime.daemon.task_release import TASK_RELEASE_METHOD
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.tui.console import bulk
from eawf.surfaces.tui.console.bulk import (
    BULK_METHODS,
    ITEM_DISPOSITIONS,
    BulkRequest,
    bulk_params,
    bulk_results,
    refused_bulk,
    unanswered_bulk,
)
from eawf.surfaces.tui.console.cards import Card, lifecycle_card, settle
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.mutation import NATIVE_KEYS
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.operations import (
    LifecycleRequest,
    OperationResult,
    OperationStatus,
    Operator,
    VerbRequest,
)
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import Session

from .overlay_support import Host, chrome

AT = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
ROOT = "eawf://EAWF/EAWF/EAWF"
ONE, TWO, THREE = "EAWF-0001", "EAWF-0002", "EAWF-0003"
RELEASE = MUTATIONS_BY_METHOD[TASK_RELEASE_METHOD]


def _urn(key: str) -> str:
    return f"{ROOT}/task/{key}"


def _document() -> dict[str, Any]:
    return {
        "task": {
            ONE: {"urn": _urn(ONE), "revision": 3, "status": "CLAIMED"},
            TWO: {"urn": _urn(TWO), "revision": 5, "status": "CLAIMED"},
            THREE: {"urn": _urn(THREE), "revision": 2, "status": "PLANNED"},
        }
    }


def _rows() -> tuple[ProjectionRow, ...]:
    return build_route_projection(
        route="backlog", document=_document(), cursor=9, scope_id="EAWF", generated_at=AT
    ).rows


class Link:
    """A daemon link that records every request it is handed."""

    def __init__(self) -> None:
        self.sent: list[VerbRequest | BulkRequest] = []

    def __call__(self, request: VerbRequest | BulkRequest) -> bool:
        self.sent.append(request)
        return True


def _press(session: Session, *keys: str, link: Link) -> None:
    for key in keys:
        ctx = Ctx(
            session=session, fixture=chrome(), host=Host(), w=120, h=30, send=link, rows=_rows()
        )
        dispatch(ctx, key, False)


def _released(link: Link, *marked: str) -> Session:
    session = Session()
    session.route = "backlog"
    session.marked = list(marked)
    _press(session, ".", NATIVE_KEYS[TASK_RELEASE_METHOD], "Enter", link=link)
    return session


def _card(session: Session) -> Card:
    card = session.mutation
    assert isinstance(card, Card)
    return card


def _answer(states: dict[str, str], *, code: str | None = None) -> dict[str, Any]:
    return {
        "item_results": {
            _urn(key): {"state": state, "code": code if state == "rejected" else None, "detail": ""}
            for key, state in states.items()
        }
    }


# ---- DEL-021: one operation, each target judged on its own -------------------


def test_del_021_the_console_bulk_methods_name_the_daemon_verbs() -> None:
    assert bulk.BULK_PREVIEW_METHOD == daemon_bulk.BULK_PREVIEW_METHOD
    assert bulk.BULK_CONTROL_METHOD == daemon_bulk.BULK_CONTROL_METHOD
    assert bulk.BULK_RECONCILE_METHOD == daemon_bulk.BULK_RECONCILE_METHOD
    assert BULK_METHODS == {TASK_RELEASE_METHOD: BulkVerb.RELEASE}


def test_del_021_a_marked_release_is_sent_as_one_bulk_operation() -> None:
    link = Link()

    session = _released(link, ONE, TWO, THREE)

    [sent] = link.sent
    assert isinstance(sent, BulkRequest)
    assert sent.verb is BulkVerb.RELEASE
    assert sent.targets == (ONE, TWO)
    assert dict(sent.revisions) == {ONE: 3, TWO: 5}
    assert sent.reconcile is False
    card = _card(session)
    assert [row.disposition.value for row in card.results] == ["requesting", "requesting", "idle"]
    assert "illegal_transition" in card.results[2].detail


def test_del_021_a_single_target_release_is_refused_on_the_card_for_its_missing_cause() -> None:
    card = lifecycle_card(RELEASE, _rows()[:1], principal="you", now=0.0)

    assert card.bulk_verb is None
    [item] = card.items
    assert item.refusal is not None
    assert item.refusal.code == "transition_reason_missing"


def test_del_021_a_bulk_release_card_does_not_ask_for_a_cause_the_verb_records() -> None:
    card = lifecycle_card(RELEASE, _rows()[:2], principal="you", now=0.0)

    assert card.bulk_verb is BulkVerb.RELEASE
    assert all(item.refusal is None for item in card.items)


def test_del_021_a_verb_with_no_bulk_twin_is_still_sent_target_by_target() -> None:
    link = Link()
    session = Session()
    session.route = "backlog"
    session.marked = [ONE, TWO]
    _press(session, ".", NATIVE_KEYS["domain.task.start"], link=link)

    assert _card(session).bulk_verb is None


def test_del_021_a_bulk_request_needs_targets_and_their_revisions() -> None:
    with pytest.raises(ValueError, match="at least one target"):
        BulkRequest(verb=BulkVerb.RELEASE, targets=(), revisions={}, operation_id="MUT-1")
    with pytest.raises(ValueError, match="carries the revision"):
        BulkRequest(verb=BulkVerb.RELEASE, targets=(ONE,), revisions={}, operation_id="MUT-1")


def test_del_021_the_operation_is_anchored_at_the_revisions_the_card_showed() -> None:
    request = BulkRequest(
        verb=BulkVerb.RELEASE, targets=(ONE, TWO), revisions={ONE: 3, TWO: 5}, operation_id="M-1"
    )

    params = bulk_params(
        request, {ONE: _urn(ONE), TWO: _urn(TWO)}, actor="OP-0001", digest="sha256:x"
    )

    assert params == {
        "verb": "release",
        "item_refs": [_urn(ONE), _urn(TWO)],
        "expected_revisions": {_urn(ONE): 3, _urn(TWO): 5},
        "actor": "OP-0001",
        "idempotency_key": "M-1",
        "confirmation_digest": "sha256:x",
    }


# ---- DEL-022 and DEL-023: one row per target, unknown until reconciled ---------


def test_del_022_every_item_state_reads_as_its_own_console_outcome() -> None:
    assert set(ITEM_DISPOSITIONS) == set(BulkItemState)
    assert ITEM_DISPOSITIONS[BulkItemState.UNKNOWN] is ControlDisposition.UNKNOWN


def test_del_022_each_row_takes_its_own_item_result() -> None:
    link = Link()
    session = _released(link, ONE, TWO)
    [sent] = link.sent
    assert isinstance(sent, BulkRequest)
    urns = {ONE: _urn(ONE), TWO: _urn(TWO)}

    for result in bulk_results(sent, urns, _answer({ONE: "confirmed", TWO: "rejected"}, code="x")):
        settle(session, result, now=1.0)

    rows = _card(session).results
    assert [row.disposition for row in rows] == [
        ControlDisposition.CONFIRMED,
        ControlDisposition.REJECTED,
    ]
    assert "x" in rows[1].detail


def test_del_023_an_unanswered_operation_leaves_every_target_unknown() -> None:
    request = BulkRequest(
        verb=BulkVerb.RELEASE, targets=(ONE, TWO), revisions={ONE: 3, TWO: 5}, operation_id="M-1"
    )

    results = unanswered_bulk(request, "no answer")

    assert {r.disposition for r in results} == {ControlDisposition.UNKNOWN}
    assert {r.status for r in results} == {OperationStatus.OUTSTANDING}


def test_del_023_reconcile_asks_the_daemon_again_for_the_whole_operation() -> None:
    link = Link()
    session = _released(link, ONE, TWO)
    [sent] = link.sent
    assert isinstance(sent, BulkRequest)
    settle(
        session,
        OperationResult(
            operation_id=sent.operation_id,
            target=TWO,
            status=OperationStatus.OUTSTANDING,
            detail="unknown",
            disposition=ControlDisposition.UNKNOWN,
        ),
        now=1.0,
    )

    _press(session, "ArrowDown", "n", link=link)

    again = link.sent[-1]
    assert isinstance(again, BulkRequest)
    assert again.reconcile is True
    assert (again.operation_id, again.targets) == (sent.operation_id, sent.targets)


def test_del_023_a_refused_operation_rejects_every_target() -> None:
    request = BulkRequest(
        verb=BulkVerb.RELEASE, targets=(ONE,), revisions={ONE: 3}, operation_id="M-1"
    )

    [result] = refused_bulk(request, "idempotency_conflict")

    assert result.disposition is ControlDisposition.REJECTED
    assert "idempotency_conflict" in result.detail


# ---- the seam sends preview then control -------------------------------------


class _Daemon:
    """A daemon serving the backlog read and answering the bulk verbs."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail
        self._reads = {read_method(r): r for r in ROUTE_COLLECTIONS}

    def client(self) -> _Client:
        return _Client(self)

    def answer(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        route = self._reads.get(method)
        if route is not None:
            return build_route_projection(
                route=route, document=_document(), cursor=5, scope_id="EAWF", generated_at=AT
            ).model_dump(mode="json")
        self.calls.append((method, params))
        if method == bulk.BULK_PREVIEW_METHOD:
            return {"confirmation_digest": f"sha256:{'a' * 64}"}
        if self.fail is not None:
            raise self.fail
        return _answer({ONE: "confirmed", TWO: "unknown"})


class _Client:
    def __init__(self, daemon: _Daemon) -> None:
        self._daemon = daemon

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._daemon.answer(method, dict(params or {}))


def _sent(daemon: _Daemon, *, operator: Operator | None, reconcile: bool = False) -> Any:
    seam = ProjectionSeam(
        route="backlog",
        scope_id="EAWF",
        state_path=None,
        clock=lambda: AT,
        daemon_client_factory=daemon.client,
        operator=operator,
    )
    request = BulkRequest(
        verb=BulkVerb.RELEASE,
        targets=(ONE, TWO),
        revisions={ONE: 3, TWO: 5},
        operation_id="MUT-1",
        reconcile=reconcile,
    )

    async def go() -> Any:
        await seam.load()
        return await seam.bulk(request)

    return asyncio.run(go())


def test_del_024_the_seam_opens_under_the_digest_the_preview_answered() -> None:
    daemon = _Daemon()

    results = _sent(daemon, operator=Operator(principal="OP-0001"))

    [(preview, shown), (control, opened)] = daemon.calls
    assert preview == bulk.BULK_PREVIEW_METHOD
    assert shown["item_refs"] == [_urn(ONE), _urn(TWO)]
    assert control == bulk.BULK_CONTROL_METHOD
    assert opened["confirmation_digest"] == f"sha256:{'a' * 64}"
    assert opened["actor"] == "OP-0001"
    assert [r.disposition for r in results] == [
        ControlDisposition.CONFIRMED,
        ControlDisposition.UNKNOWN,
    ]


def test_del_023_the_seam_reconciles_under_the_same_key() -> None:
    daemon = _Daemon()

    _sent(daemon, operator=Operator(principal="OP-0001"), reconcile=True)

    method, params = daemon.calls[-1]
    assert method == bulk.BULK_RECONCILE_METHOD
    assert params["idempotency_key"] == "MUT-1"


def test_del_023_a_lost_answer_leaves_every_target_unknown() -> None:
    results = _sent(_Daemon(fail=OSError("gone")), operator=Operator(principal="OP-0001"))

    assert {r.disposition for r in results} == {ControlDisposition.UNKNOWN}


def test_del_025_a_refused_operation_rejects_every_target_with_the_daemons_reason() -> None:
    refusal = DaemonRpcError(-32602, "validation_failed: idempotency_conflict: another set")

    results = _sent(_Daemon(fail=refusal), operator=Operator(principal="OP-0001"))

    assert {r.disposition for r in results} == {ControlDisposition.REJECTED}
    assert all("idempotency_conflict" in r.detail for r in results)


def test_del_021_a_console_acting_as_nobody_sends_nothing() -> None:
    daemon = _Daemon()

    results = _sent(daemon, operator=None)

    assert daemon.calls == []
    assert {r.disposition for r in results} == {ControlDisposition.IDLE}


def test_del_021_a_lifecycle_request_is_still_what_a_single_target_sends() -> None:
    link = Link()
    session = Session()
    session.route = "backlog"
    session.sel_id = THREE
    _press(session, ".", NATIVE_KEYS["domain.task.claim"], "Enter", link=link)

    assert [type(sent) for sent in link.sent] == [LifecycleRequest]
