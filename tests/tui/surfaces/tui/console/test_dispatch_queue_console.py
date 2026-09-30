"""The unattended route draws the daemon's dispatch-queue read, and asks it to pause or drain.

UI-025: an executing Run renders its elapsed time against the budget its capsule sealed and
says it is opaque; an enumerating verification leg renders its count against its total, the
last unit it finished and its tallies. UI-027 / CON-078: progress is a named count against
its total, never a bare percentage and never called done. UI-067: the ``QUEUE`` line counts
the forced sequential Runs, ``PLAN`` states the concurrency and every edge, and ``CONTROL``
names the last request with its outcome. UI-023: ``a`` previews a pause the daemon owns --
a resume once dispatch is held -- the drain menu verb is bound, and a confirmed card leaves
as the daemon's dispatch control verb.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest

from eawf.kernel.runtime.dispatch_queue import (
    DispatchControl,
    DispatchControlFact,
    DispatchOutcome,
    DispatchPlan,
    DispatchQueueView,
    DispatchVerb,
    ProgressMode,
    QueuedRun,
    QueueEdge,
    VerificationLeg,
)
from eawf.surfaces.tui.console.cards import Card
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.live_reads import DISPATCH_QUEUE_READ
from eawf.surfaces.tui.console.mutation import adopt, confirm
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.operations import (
    DISPATCH_CONTROL_METHOD,
    DISPATCH_QUEUE_TARGET,
    DispatchRequest,
    Operator,
    address_dispatch,
    binding_refusal,
)
from eawf.surfaces.tui.console.session import Session
from tests.tui.surfaces.tui.console import journey_support as js
from tests.tui.surfaces.tui.console import test_native_route_bodies as bodies
from tests.tui.surfaces.tui.console import test_native_route_frames as frames

READ_AT: Final = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
NOW: Final = READ_AT + timedelta(seconds=5)
STARTED: Final = NOW - timedelta(minutes=2, seconds=8)


def queue_view(
    *,
    held: DispatchVerb | None = None,
    last: DispatchControlFact | None = None,
    legs: tuple[VerificationLeg, ...] = (),
    edges: tuple[QueueEdge, ...] = (QueueEdge(waits="TSK-0002", on="TSK-0001"),),
) -> DispatchQueueView:
    """Return the queue the daemon states over the probe tree's two Runs."""
    return DispatchQueueView(
        runs=(
            QueuedRun(
                run_key="RUN-00000001",
                task_key="TSK-0001",
                state="RUNNING",
                admitted_at=STARTED,
                started_at=STARTED,
                budget_seconds=2_700,
            ),
            QueuedRun(run_key="RUN-00000002", task_key="TSK-0002", state="QUEUED"),
        ),
        legs=legs,
        plan=DispatchPlan(slots=6, in_use=1, edges=edges),
        control=DispatchControl(
            dispatch_paused=held is DispatchVerb.PAUSE,
            drain_requested=held is DispatchVerb.DRAIN,
            last_request=last,
        ),
        read_at=READ_AT,
    )


def enumerating_leg(*, total: int | None = 50) -> VerificationLeg:
    """Return a running leg that published 31 of its obligations."""
    return VerificationLeg(
        gate_id="pytest",
        criterion_id="CR-01",
        started_at=STARTED,
        heartbeat_at=NOW,
        budget_seconds=3_600,
        progress_mode=ProgressMode.ENUMERATED,
        completed=31,
        total=total,
        last_completed="tests/unit/test_a.py::test_b",
        passed=30,
        failed=1,
        unknown=0,
    )


def frame(view: DispatchQueueView | None, *, w: int = 120) -> list[str]:
    """Return the native unattended frame drawn with the queue read held."""
    live = {DISPATCH_QUEUE_READ: view} if view is not None else {}
    return frames._frame("unattended", w=w, live=live, now=NOW)


def _row(drawn: list[str], key: str) -> str:
    return next(row for row in drawn if key in row)


# ---------- UI-025: an executing Run and leg carry elapsed, budget and progress mode ----------


def test_ui_025_an_executing_run_renders_opaque_with_its_elapsed_budget() -> None:
    running = _row(frame(queue_view()), "RUN-00000001")
    assert running.rstrip().endswith("opaque · 2m 08s of 45m")


def test_ui_025_an_enumerating_leg_renders_counts_last_unit_and_tallies() -> None:
    drawn = frame(queue_view(legs=(enumerating_leg(),)))
    verify = frames._starts(drawn, " VERIFY")
    tallies = drawn[drawn.index(verify) + 1]
    assert verify.rstrip().endswith("gate pytest · 31 of 50 obligations · 2m 08s of 1h")
    assert "30 pass · 1 fail · 0 unknown · last tests/unit/test_a.py::test_b" in tallies


def test_ui_025_an_opaque_leg_says_so_with_its_budget_and_elapsed() -> None:
    leg = enumerating_leg().model_copy(
        update={"progress_mode": ProgressMode.OPAQUE, "completed": None, "total": None}
    )
    verify = frames._starts(frame(queue_view(legs=(leg,))), " VERIFY")
    assert "gate pytest · opaque · 2m 08s of 1h" in verify


def test_ui_026_a_queue_read_that_stopped_arriving_renders_stale() -> None:
    later = frames._frame(
        "unattended", live={DISPATCH_QUEUE_READ: queue_view()}, now=READ_AT + timedelta(hours=1)
    )
    assert "stale · queue last read" in _row(later, "RUN-00000001")


# ---------- UI-027 / CON-078: progress is a named count, never a percentage or done ----------


def test_ui_027_a_count_without_a_total_is_unbounded_and_never_called_done() -> None:
    drawn = frame(queue_view(legs=(enumerating_leg(total=None),)))
    verify = frames._starts(drawn, " VERIFY")
    assert "31 obligations so far · 2m 08s of 1h" in verify and " of 50" not in verify
    text = "\n".join(drawn).lower()
    assert "done" not in text and "complete" not in text


@pytest.mark.parametrize("w", [80, 120, 160])
def test_con_078_progress_is_a_named_numerator_and_never_a_bare_percentage(w: int) -> None:
    drawn = frame(queue_view(legs=(enumerating_leg(),)), w=w)
    assert "%" not in "\n".join(drawn)
    assert re.search(r"\d+ of \d+ obligations", frames._starts(drawn, " VERIFY"))


# ---------- UI-067: QUEUE, PLAN and CONTROL come from the daemon's read ----------


def test_ui_067_queue_plan_and_control_state_the_daemon_read() -> None:
    last = DispatchControlFact(
        request_ref="DSP-0001",
        verb=DispatchVerb.PAUSE,
        actor="EDW",
        requested_at=READ_AT,
        outcome=DispatchOutcome.CONFIRMED,
    )
    drawn = frame(queue_view(held=DispatchVerb.PAUSE, last=last))
    assert "1 queued · 1 running · 1 forced sequential" in frames._starts(drawn, " QUEUE")
    plan = frames._starts(drawn, " PLAN")
    assert "Concurrency 1 of 6 — derived from the dependency graph" in plan
    text = "\n".join(drawn)
    assert "TSK-0002 waits on TSK-0001 · forced sequential" in text
    assert "the daemon confirmed request pause by EDW at" in text
    assert "dispatch is held by pause · claimed runs go on" in text


def test_ui_067_a_rejected_drain_names_its_reason() -> None:
    last = DispatchControlFact(
        request_ref="DSP-0002",
        verb=DispatchVerb.DRAIN,
        actor="EDW",
        requested_at=READ_AT,
        outcome=DispatchOutcome.REJECTED,
        reason="a release is publishing",
    )
    text = "\n".join(frame(queue_view(last=last, edges=())))
    assert "the daemon rejected request drain by EDW" in text
    assert "a release is publishing" in text
    assert "∅ no forced sequential edge" in text


# ---------- UI-023: every control is a daemon request behind the consequence card ----------


class _Host:
    """The dispatcher's host: a held clock and a quit nothing asks for."""

    def __init__(self) -> None:
        self._clock = FakeClock()

    @property
    def clock(self) -> Clock:
        """Return the console clock."""
        return self._clock

    def quit(self) -> None:
        """Record nothing; no key here quits."""


def _ctx(session: Session, view: DispatchQueueView, sent: list[Any]) -> Ctx:
    def send(request: Any) -> bool:
        sent.append(request)
        return True

    return Ctx(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        host=_Host(),
        w=120,
        h=36,
        projection=frames._model("unattended"),
        send=send,
        principal="EDW",
        live={DISPATCH_QUEUE_READ: view},
    )


def _linked(conn: str = "LIVE") -> Session:
    session = Session()
    session.route = "unattended"
    session.conn = conn
    return session


def test_ui_023_a_opens_a_pause_card_the_daemon_is_asked_to_carry_out() -> None:
    session, sent = _linked(), []
    ctx = _ctx(session, queue_view(), sent)

    dispatch(ctx, "a", False)
    adopt(ctx)
    card = session.mutation
    assert isinstance(card, Card) and card.kind == "dispatch"
    assert card.items[0].key == DISPATCH_QUEUE_TARGET
    assert "no claimed run is stopped or cancelled" in card.items[0].not_effects
    confirm(ctx)

    assert sent == [DispatchRequest(verb="pause")]


def test_ui_023_a_held_queue_previews_a_resume() -> None:
    session, sent = _linked(), []
    ctx = _ctx(session, queue_view(held=DispatchVerb.PAUSE), sent)

    dispatch(ctx, "a", False)
    adopt(ctx)

    assert session.c_target is not None and session.c_target["verb"] == "request resume"
    confirm(ctx)
    assert sent == [DispatchRequest(verb="resume")]


def test_ui_023_a_control_moved_under_the_open_card_reloads_it_and_sends_nothing() -> None:
    """The card is bound to the control it opened on; a move underneath withdraws it."""
    session, sent = _linked(), []
    ctx = _ctx(session, queue_view(), sent)
    dispatch(ctx, "a", False)
    adopt(ctx)
    opened = session.mutation
    assert (
        isinstance(opened, Card) and opened.items[0].stale_token == "dispatching before any request"
    )
    assert "reloads against it" in opened.if_stale
    paused = DispatchControlFact(
        request_ref="DSP-0003",
        verb=DispatchVerb.PAUSE,
        actor="ABC",
        requested_at=READ_AT,
        outcome=DispatchOutcome.CONFIRMED,
    )
    moved = _ctx(session, queue_view(held=DispatchVerb.PAUSE, last=paused), sent)

    confirm(moved)

    reloaded = session.mutation
    assert sent == []
    assert isinstance(reloaded, Card) and not reloaded.results
    assert reloaded.items[0].stale_token == "pause after DSP-0003"
    assert reloaded.items[0].status == "held by pause"
    assert reloaded.note == (
        f"reloaded · {DISPATCH_QUEUE_TARGET} dispatching before any request → pause after "
        "DSP-0003 · the authorization was withdrawn"
    )
    confirm(moved)
    assert sent == [DispatchRequest(verb="pause")]


def test_ui_023_offline_the_request_verbs_are_refused_before_any_card() -> None:
    session, sent = _linked("OFFLINE SNAPSHOT"), []
    ctx = _ctx(session, queue_view(), sent)

    dispatch(ctx, "a", False)

    assert session.overlay is None and sent == []


def test_ui_023_the_drain_menu_verb_is_bound_to_the_daemon() -> None:
    assert binding_refusal("unattended", "request drain") == ""
    assert binding_refusal("dispatch queue", "request pause") == ""


def test_ui_023_a_request_is_sent_as_the_daemon_dispatch_control_verb() -> None:
    operation = address_dispatch(DispatchRequest(verb="drain"), operator=Operator(principal="EDW"))

    assert operation.method == DISPATCH_CONTROL_METHOD
    assert dict(operation.params) == {
        "verb": "drain",
        "actor": "EDW",
        "request_ref": operation.operation_id,
    }


def test_ui_023_the_console_spells_the_daemon_verbs_the_daemon_registers() -> None:
    from eawf.runtime.daemon.methods import dispatch_queue as daemon

    assert DISPATCH_CONTROL_METHOD == daemon.DISPATCH_CONTROL_REQUEST_METHOD
    assert DISPATCH_QUEUE_READ == daemon.DISPATCH_QUEUE_READ_METHOD


def test_ui_023_a_confirmed_pause_leaves_through_the_seam_as_the_daemon_verb() -> None:
    """Live: the seam reads the queue, ``a`` previews, and only Enter sends the request."""
    daemon = js.DocumentDaemon(bodies.DOCUMENT)

    async def body() -> tuple[int, list[tuple[str, dict[str, Any]]]]:
        async with js.driven(js.held_app(daemon)) as harness:
            await js.walk(harness, {"route": "unattended"}, ["a"])
            previewed = len(daemon.writes)
            await harness.press("Enter", "ui-023")
            await harness.app.workers.wait_for_complete()
            return previewed, list(daemon.writes)

    previewed, writes = asyncio.run(body())
    assert previewed == 0
    assert [(method, params["verb"]) for method, params in writes] == [
        (DISPATCH_CONTROL_METHOD, "pause")
    ]
