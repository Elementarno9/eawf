"""A standing stall is one Attention item under ``stalled``, cleared when the stall clears.

UI-062: the Attention register the daemon serves lists each stall the sweep raised over a
running Run, for as long as it stands, as one item under the ``stalled`` bucket. It is
addressed to every principal, counted, the item ``!`` jumps to and announced once per
episode. Once the Run answers, its stall no longer stands and the item is gone; a later
silence is a new episode and a new item. The console lists it with the pause over its Run
as the one place it is resumed or let go: no attention verb is offered on the row, and
Enter opens that pause.

Everything is driven through the real verbs and the real sweep, on a disposable canary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import pytest

from eawf.kernel.projection.attention import (
    AttentionBucket,
    NotificationClass,
    attention_all,
    attention_mine,
    build_attention_view,
    delivered_revisions,
    deliveries,
    top_item,
)
from eawf.kernel.projection.compute import STALL_KIND, RouteProjection
from eawf.kernel.projection.registers import RegisterView, build_register_view
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.pause import PAUSE_READ_METHOD, PausesAnswer
from eawf.runtime.daemon.stall_sweep import sweep_once
from eawf.surfaces.tui.console.attention import STALL_REFUSAL, selected_open_row
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.decisions import DecisionRecords, PauseRecord
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.enter_keys import confirm
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.operations import VerbRequest
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.attention import STALL_LINE
from eawf.surfaces.tui.console.session import Session
from tests.integration.runtime.daemon.test_run_stall_interval import (
    ACTOR,
    RUN_KEY,
    RUN_URN,
    announce_as,
    configure,
)
from tests.integration.runtime.daemon.test_run_stall_signal import act, call, canary, ctx, quiet

__all__ = ["canary", "ctx"]

pytestmark = pytest.mark.integration

READ: Final = "projection.attention.read"

#: Another principal, whose count a stall addressed to every principal also reaches.
OTHER: Final = "OP-0002"


def attention(ctx: MethodContext, canary: CanaryProvision) -> tuple[RouteProjection, RegisterView]:
    """Read the Attention register through the daemon verb."""
    projection = RouteProjection.model_validate(call(READ, ctx, repo_root=str(canary.root)))
    return projection, build_register_view(projection)


def stalled(ctx: MethodContext, canary: CanaryProvision) -> tuple[RouteProjection, RegisterView]:
    """Let the Run go quiet, sweep once, and read the Attention register."""
    quiet(ctx, canary)
    assert sweep_once(ctx, now=datetime.now(UTC)) == (RUN_KEY,)
    return attention(ctx, canary)


def test_ui_062_a_standing_stall_is_one_stalled_item_against_its_run(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    _projection, register = stalled(ctx, canary)

    (row,) = register.rows
    assert (row.collection, row.key, row.urn) == (
        Epoch2Collection.RUN,
        f"STL-{RUN_KEY}-1",
        RUN_URN,
    )
    # the row is revised by the episode: one past the activity it was measured from
    assert (row.revision, row.status.value) == (2, "stalled")
    assert row.facts["kind"] == STALL_KIND
    assert row.facts["subject"] == RUN_KEY
    assert row.facts["question"].startswith("stopped responding · silent ")
    assert row.facts["last_activity"] == "reasoning started"
    (item,) = build_attention_view(register).items
    assert (item.bucket, item.need, item.read_only) == (AttentionBucket.STALLED, None, False)
    assert item.notification_class is NotificationClass.STOPPED_RESPONDING
    assert item.assignee_ref is None
    counts = {c.bucket: c.count for c in build_attention_view(register).bucket_counts()}
    assert counts[AttentionBucket.STALLED] == 1


def test_ui_062_a_stall_is_every_principal_s_counted_jumped_to_and_announced_once(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    _projection, register = stalled(ctx, canary)

    assert attention_mine(register, principal=ACTOR).value == "1"
    assert attention_mine(register, principal=OTHER).value == "1"
    assert attention_all(register).value == "1"
    top = top_item(register, principal=ACTOR)
    assert top is not None and top.key == f"STL-{RUN_KEY}-1"
    (announced,) = deliveries(register, principal=ACTOR, delivered=())
    assert announced.key == top.key
    assert deliveries(register, principal=ACTOR, delivered=delivered_revisions(register)) == ()


def test_ui_062_the_item_leaves_once_the_run_answers_and_a_new_silence_is_a_new_one(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    _projection, first = stalled(ctx, canary)
    act(ctx, canary, 2)

    _projection, answered = attention(ctx, canary)
    assert answered.rows == ()
    assert build_attention_view(answered).items == ()

    assert sweep_once(ctx, now=datetime.now(UTC)) == (RUN_KEY,)
    _projection, again = attention(ctx, canary)
    (row,) = again.rows
    assert (row.key, row.revision) == (f"STL-{RUN_KEY}-2", 3)
    (fresh,) = deliveries(again, principal=ACTOR, delivered=delivered_revisions(first))
    assert fresh.key == row.key


def test_ui_062_a_run_silent_since_it_started_is_listed_as_episode_zero(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    configure(canary, {"codex": {"stall_interval_s": 0}})
    announce_as(ctx, canary, "codex")
    assert sweep_once(ctx, now=datetime.now(UTC)) == (RUN_KEY,)

    _projection, register = attention(ctx, canary)

    (row,) = register.rows
    assert (row.key, row.revision) == (f"STL-{RUN_KEY}-0", 1)
    assert row.facts["last_activity"] == "nothing since it started"
    (item,) = build_attention_view(register).items
    assert item.bucket is AttentionBucket.STALLED


def test_ui_062_no_stall_raised_lists_nothing(canary: CanaryProvision, ctx: MethodContext) -> None:
    quiet(ctx, canary)

    _projection, register = attention(ctx, canary)

    assert register.rows == ()
    counts = {c.bucket: c.count for c in build_attention_view(register).bucket_counts()}
    assert counts[AttentionBucket.STALLED] == 0


def _view(register: RegisterView, session: Session) -> View:
    return View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=160,
        h=30,
        register=register,
        attention=register,
        linked=True,
        principal=ACTOR,
    )


def test_ui_062_the_console_lists_the_stall_and_offers_it_no_attention_verb(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    projection, register = stalled(ctx, canary)
    session = Session(route="attention", sel_id=register.rows[0].key)

    text = "\n".join(render_route(_view(register, session)))

    assert " STALLED  1" in text
    assert f"stall · {STALL_LINE}" in text
    assert f"{RUN_KEY} stopped responding" in text
    assert selected_open_row(session, projection) is not None
    assert not any(f"{key} {name}" in text for key, name in (("a", "answer"), ("x", "deny")))


class _Host:
    """The dispatcher's host: a held clock, and a quit nothing here asks for."""

    def __init__(self) -> None:
        self._clock = FakeClock()

    @property
    def clock(self) -> Clock:
        return self._clock

    def quit(self) -> None:
        raise AssertionError("acting on a stall never ends the console")


def _ctx(
    projection: RouteProjection,
    session: Session,
    sent: list[VerbRequest],
    decisions: DecisionRecords | None = None,
) -> Ctx:
    def link(request: VerbRequest) -> bool:
        sent.append(request)
        return True

    return Ctx(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        host=_Host(),
        w=120,
        h=30,
        send=link,
        attention=projection,
        decisions=decisions,
        principal=ACTOR,
    )


@pytest.mark.parametrize("key", ["a", "x", "z", "v"])
def test_ui_062_an_attention_verb_on_a_stall_is_refused_naming_the_pause(
    canary: CanaryProvision, ctx: MethodContext, key: str
) -> None:
    projection, register = stalled(ctx, canary)
    session = Session(route="attention", sel_id=register.rows[0].key)
    sent: list[VerbRequest] = []

    dispatch(_ctx(projection, session, sent), key, False)

    assert session.overlay is None
    assert sent == []
    assert STALL_REFUSAL in (session.trace or "")


def _pauses(ctx: MethodContext, canary: CanaryProvision) -> DecisionRecords:
    """Read the pauses the sweep opened, as the Attention route's records bind them."""
    answer: Any = call(PAUSE_READ_METHOD, ctx, repo_root=str(canary.root))
    pauses = PausesAnswer.model_validate(answer).pauses
    return DecisionRecords(
        pauses=tuple(PauseRecord.of_pause(i.pause, situation=i.situation) for i in pauses)
    )


def test_ui_062_enter_on_a_stall_opens_the_pause_over_its_run(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    projection, register = stalled(ctx, canary)
    records = _pauses(ctx, canary)
    (pause,) = records.pauses
    assert pause.scope == RUN_KEY
    session = Session(route="attention", sel_id=register.rows[0].key)
    sent: list[VerbRequest] = []

    dispatch(_ctx(projection, session, sent, records), "Enter", False)

    assert (session.overlay, session.ov_subject) == ("pause", pause.id)
    assert sent == []


def test_ui_062_enter_on_a_stall_whose_pause_is_unread_opens_nothing(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    projection, register = stalled(ctx, canary)
    session = Session(route="attention", sel_id=register.rows[0].key)
    sent: list[VerbRequest] = []

    dispatch(_ctx(projection, session, sent), "Enter", False)

    assert session.overlay is None
    assert sent == []
    assert "not read yet" in (session.trace or "")


def test_ui_062_confirming_a_card_on_a_stall_sends_nothing(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    projection, register = stalled(ctx, canary)
    session = Session(route="attention", sel_id=register.rows[0].key)
    sent: list[VerbRequest] = []

    confirm(_ctx(projection, session, sent))

    assert sent == []
    assert STALL_REFUSAL in (session.trace or "")
