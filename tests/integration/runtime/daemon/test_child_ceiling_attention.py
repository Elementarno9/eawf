"""A child Run admitted past a ``child_runs`` ceiling is a read-only Attention notice.

RUN-022: child count never exceeds the resolved ceiling across the delegation subtree;
the one admission past it -- a host subagent that was already running -- is filed as a
:class:`~eawf.kernel.runtime.delegation.ChildCeilingBreach` on the run ledger. The
Attention register the daemon serves lists that breach beside the calls waiting on a
principal, filed against the Run whose ceiling was passed, while that Run is live. It is
a notice: nothing answers it, so it counts toward no one, is never the item ``!`` jumps
to, never toasts, and the console offers it no verb.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.projection.attention import (
    AttentionBucket,
    attention_all,
    attention_mine,
    build_attention_view,
    delivered_revisions,
    deliveries,
    top_item,
)
from eawf.kernel.projection.compute import CEILING_BREACH_KIND, ProjectionRow, RouteProjection
from eawf.kernel.projection.connection import apply_patches
from eawf.kernel.projection.registers import RegisterView, build_register_view
from eawf.kernel.runtime.delegation import ChildCeilingBreach
from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.budget.notices import BudgetThresholdNotice, notice_key_for
from eawf.runtime.daemon.methods.host_subagent import HOST_SUBAGENT_START_METHOD
from eawf.surfaces.tui.console.attention import selected_open_row
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.enter_keys import confirm
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.notices import short_key
from eawf.surfaces.tui.console.operations import VerbRequest
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.attention import NOTICE_LINE
from eawf.surfaces.tui.console.session import Session
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import AT, seed_row
from tests.integration.runtime.daemon.test_child_runs import (
    ACTOR,
    HOST_SESSION,
    ROOT_KEY,
    append_run_line,
    call,
    canary_with,
    host_parent,
    run_row,
    urn,
)

pytestmark = pytest.mark.integration

READ: Final = "projection.attention.read"
OTHER_KEY: Final = "RUN-00000020"


def adopt_past_the_ceiling(tmp_path: Path, **runs: dict[str, Any]) -> tuple[Any, str]:
    """Adopt a host subagent under a task-scoped root that resolves ``child_runs=0``."""
    task_scope = seed_row("run", "RUNNING")["scope"]
    canary = canary_with(tmp_path, {ROOT_KEY: host_parent(scope=task_scope), **runs})
    answer = call(
        canary,
        tmp_path,
        HOST_SUBAGENT_START_METHOD,
        harness="claude-code",
        agent_id="subagent02",
        host_session_id=HOST_SESSION,
    )
    return canary, answer["run_ref"]


def attention(canary: Any, tmp_path: Path) -> tuple[RouteProjection, RegisterView]:
    projection = RouteProjection.model_validate(call(canary, tmp_path, READ))
    return projection, build_register_view(projection)


def _breaches(register: RegisterView) -> tuple[ProjectionRow, ...]:
    """Return the breach notices the register lists; its running Runs are listed beside them."""
    return tuple(row for row in register.rows if row.facts.get("kind") == CEILING_BREACH_KIND)


def file_breach(canary: Any, *, ancestor: str, child: str) -> None:
    breach = ChildCeilingBreach(
        child_run_ref=parse_qualified_urn(urn(child)),
        ancestor_run_ref=parse_qualified_urn(urn(ancestor)),
        ceiling=0,
        descendants=1,
        recorded_at=AT,
    )
    append_run_line(
        canary,
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key=f"CHILD-CEILING-{child}",
            status="breached",
            recorded_at=AT + timedelta(seconds=1),
            payload=breach.model_dump(mode="json"),
        ),
    )


def test_run_022_a_breach_is_listed_in_attention_against_the_overrun_run(
    tmp_path: Path,
) -> None:
    canary, child_ref = adopt_past_the_ceiling(tmp_path)
    child = child_ref.rsplit("/", 1)[-1]

    _projection, register = attention(canary, tmp_path)

    (row,) = _breaches(register)
    assert (row.collection, row.key, row.urn) == (
        Epoch2Collection.RUN,
        f"CHILD-CEILING-{child}",
        urn(ROOT_KEY),
    )
    assert row.status.value == "breached"
    assert row.facts["kind"] == CEILING_BREACH_KIND
    assert (row.facts["subject"], row.facts["child"]) == (ROOT_KEY, child)
    assert row.facts["question"] == f"{child} made the subtree 1 Runs past child_runs=0"
    (item,) = [i for i in build_attention_view(register).items if i.key == row.key]
    assert (item.bucket, item.need, item.read_only) == (AttentionBucket.OVER_BUDGET, None, True)
    assert item.assignee_ref is None
    assert item.addressed_to(ACTOR)


def test_run_022_a_breach_is_a_notice_that_counts_toward_no_one(tmp_path: Path) -> None:
    canary, _child = adopt_past_the_ceiling(tmp_path)

    _projection, register = attention(canary, tmp_path)

    assert attention_mine(register, principal=ACTOR).value == "0"
    assert attention_all(register).value == "0"
    assert top_item(register, principal=ACTOR) is None
    assert deliveries(register, principal=ACTOR, delivered=()) == ()
    (breach,) = _breaches(register)
    assert (breach.urn, breach.revision) in delivered_revisions(register)
    budget = next(
        count
        for count in build_attention_view(register).bucket_counts()
        if count.bucket is AttentionBucket.OVER_BUDGET and count.need is None
    )
    assert budget.count == 1


def test_run_022_the_console_offers_a_breach_no_verb(tmp_path: Path) -> None:
    canary, _child = adopt_past_the_ceiling(tmp_path)
    projection, register = attention(canary, tmp_path)
    session = Session(route="attention", sel_id=register.rows[0].key)
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=160,
        h=30,
        register=register,
        attention=register,
        linked=True,
        principal=ACTOR,
    )

    text = "\n".join(render_route(view))

    assert selected_open_row(session, projection) is None
    assert f"ceiling breach · {NOTICE_LINE}" in text
    assert "you may answer" not in text
    assert not any(f"{key} {name}" in text for key, name in (("a", "answer"), ("x", "deny")))


class _Host:
    """The dispatcher's host: a held clock, and a quit nothing here asks for."""

    def __init__(self) -> None:
        self._clock = FakeClock()

    @property
    def clock(self) -> Clock:
        return self._clock

    def quit(self) -> None:
        raise AssertionError("confirming a notice never ends the console")


def test_run_022_enter_on_a_breach_sends_nothing(tmp_path: Path) -> None:
    canary, _child = adopt_past_the_ceiling(tmp_path)
    projection, register = attention(canary, tmp_path)
    session = Session(route="attention", sel_id=register.rows[0].key)
    sent: list[VerbRequest] = []

    def link(request: VerbRequest) -> bool:
        sent.append(request)
        return True

    confirm(
        Ctx(
            session=session,
            fixture=Fixture.from_chrome(load_chrome()),
            host=_Host(),
            w=120,
            h=30,
            send=link,
            attention=projection,
        )
    )

    assert sent == []
    assert "is a notice" in (session.trace or "")


@pytest.mark.parametrize("key", ["a", "x", "z", "v"])
def test_run_022_an_attention_verb_on_a_breach_answers_read_only(tmp_path: Path, key: str) -> None:
    canary, _child = adopt_past_the_ceiling(tmp_path)
    projection, register = attention(canary, tmp_path)
    session = Session(route="attention", sel_id=register.rows[0].key)
    sent: list[VerbRequest] = []

    def link(request: VerbRequest) -> bool:
        sent.append(request)
        return True

    ctx = Ctx(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        host=_Host(),
        w=120,
        h=30,
        send=link,
        attention=projection,
        principal=ACTOR,
    )

    dispatch(ctx, key, False)

    assert session.overlay is None
    assert sent == []
    assert "read-only" in (session.trace or "")


def test_run_022_a_breach_leaves_attention_once_its_run_is_no_longer_live(
    tmp_path: Path,
) -> None:
    ended = run_row(OTHER_KEY, status="COMPLETED")
    canary, _child = adopt_past_the_ceiling(tmp_path, **{OTHER_KEY: ended})
    file_breach(canary, ancestor=OTHER_KEY, child="RUN-00000021")
    file_breach(canary, ancestor="RUN-00000099", child="RUN-00000022")

    _projection, register = attention(canary, tmp_path)

    assert [row.urn for row in _breaches(register)] == [urn(ROOT_KEY)]


def test_run_022_a_replayed_register_keeps_the_notice(tmp_path: Path) -> None:
    canary, _child = adopt_past_the_ceiling(tmp_path)
    projection, _register = attention(canary, tmp_path)

    replayed = apply_patches(
        projection,
        (),
        cursor=int(projection.header.source_cursor),
        scope_id=projection.header.scope_id,
        generated_at=projection.header.generated_at,
    )

    assert replayed.rows == projection.rows
    assert replayed.digest == projection.digest


def test_run_022_a_tree_with_no_breach_lists_no_run(tmp_path: Path) -> None:
    canary = canary_with(tmp_path, {ROOT_KEY: host_parent()})

    _projection, register = attention(canary, tmp_path)

    assert _breaches(register) == ()
    assert [i.bucket for i in build_attention_view(register).items] == [AttentionBucket.ACTIVE]


def budget_notice() -> BudgetThresholdNotice:
    return BudgetThresholdNotice(
        notice_key=notice_key_for(scope_id=ROOT_KEY, axis="tokens", basis="hard_limit"),
        scope_id=ROOT_KEY,
        axis="tokens",
        basis="hard_limit",
        highest_band="limit_reached",
        severity="critical",
        observed_value=1200,
        budget_value=1000,
        revision=2,
        opened_at=AT,
        last_observed_at=AT,
        audience=(ACTOR,),
    )


def test_run_022_a_breach_and_a_budget_notice_share_the_window_and_keep_their_verbs(
    tmp_path: Path,
) -> None:
    canary, _child = adopt_past_the_ceiling(tmp_path)
    projection, register = attention(canary, tmp_path)
    notice = budget_notice()

    def press(sel_id: str, key: str) -> Session:
        session = Session(route="attention", sel_id=sel_id)
        view = View(
            session=session,
            fixture=Fixture.from_chrome(load_chrome()),
            w=160,
            h=30,
            register=register,
            attention=register,
            linked=True,
            principal=ACTOR,
            notices=(notice,),
        )
        frames.append("\n".join(render_route(view)))
        ctx = Ctx(
            session=session,
            fixture=view.fixture,
            host=_Host(),
            w=160,
            h=30,
            attention=projection,
            notices=(notice,),
            principal=ACTOR,
        )
        dispatch(ctx, key, False)
        return session

    frames: list[str] = []
    on_breach = press(register.rows[0].key, "z")
    on_notice = press(notice.notice_key, "z")

    assert on_breach.overlay is None
    assert "read-only" in (on_breach.trace or "")
    assert on_notice.overlay is not None
    assert "read-only" not in (on_notice.trace or "")
    breach_frame, notice_frame = frames
    for frame in frames:
        assert register.rows[0].key in frame
        assert short_key(notice) in frame
        headings = [line for line in frame.splitlines() if line.startswith(" OVER BUDGET")]
        assert [line.split()[:3] for line in headings] == [["OVER", "BUDGET", "2"]]
    assert "resolve" not in breach_frame.splitlines()[-1]
    assert "snooze" in notice_frame.splitlines()[-1]
    assert "1200 of 1000 tokens" in notice_frame
    assert NOTICE_LINE in breach_frame
