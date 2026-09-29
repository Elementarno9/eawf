"""Bulk control on Activity: one preview, one request and one result row per target.

CON-148 declares bulk control a coverage hole until the console draws it from its own
render path, and names the journeys that close it. Each journey here drives the
production dispatcher over a held Run register the way the app presses keys: Space marks
two Runs, which the frame shows in text and the action menu counts as ``2 selected``; the
bulk verb opens the one consequence card naming the count, both identifiers and the
``UNKNOWN`` pane; Enter sends one request per target. The daemon's answers are then
settled per target, and the card shows one row per target -- never a collapsed verdict --
for the three endings: every target confirms, none does, and exactly one does not, whose
``unknown`` row is answered only by ``reconcile``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from eawf.kernel.runtime.control import ControlDisposition
from eawf.surfaces.tui.console.app import compose_frame
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.drawers import action_rows
from eawf.surfaces.tui.console.frame import MARKED, paint_marks
from eawf.surfaces.tui.console.mutation import CARD, NOTHING_TO_MARK, RECONCILABLE, Card, settle
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.operations import (
    OperationResult,
    OperationStatus,
    VerbRequest,
)
from eawf.surfaces.tui.console.session import Session
from tests.tui.surfaces.tui.console import test_native_route_bodies as bodies
from tests.tui.surfaces.tui.console.test_console_verbs import _Host

FIRST, SECOND = "RUN-00000011", "RUN-00000012"
MS_FIRST, MS_SECOND = "MLS-0201", "MLS-0202"


def _run(key: str) -> dict[str, Any]:
    return bodies._row(
        "run",
        key,
        "RUNNING",
        2,
        updated_at="2026-09-17T11:00:00Z",
        scope={"purpose": "implement", "task_ref": f"{bodies.ROOT}/task/TSK-0001"},
    )


def _milestone(key: str) -> dict[str, Any]:
    return bodies._row(
        "milestone",
        key,
        "PLANNED",
        2,
        title=f"Planned {key}",
        primary_track_ref=f"{bodies.ROOT}/track/TRK-CORE",
    )


#: Two running Runs of one Task, and two planned Milestones of one Track.
DOCUMENT: dict[str, Any] = {
    **bodies.DOCUMENT,
    "run": {key: _run(key) for key in (FIRST, SECOND)},
    "milestone": {key: _milestone(key) for key in (MS_FIRST, MS_SECOND)},
}

#: The Run verb the Activity journey previews; a Run's outcome is only ever observed, so
#: the console's card refuses it for every target, which is the refusal render.
FINISH = "o"
#: The Milestone verb the result journeys send: a planned Milestone may be activated.
ACTIVATE = "t"


class _Link:
    """A daemon link that takes every request and records it."""

    def __init__(self) -> None:
        self.sent: list[VerbRequest] = []

    def __call__(self, request: VerbRequest) -> bool:
        self.sent.append(request)
        return True


class _Journey:
    """A route over the held rows, driven key by key the way the app presses them."""

    def __init__(self, route: str = "activity", first: str = FIRST) -> None:
        self.rows = bodies._projection(route, DOCUMENT).rows
        self.view = replace(bodies._view(route, document=DOCUMENT), rows=self.rows)
        self.link = _Link()
        self.first = first
        self.frame = compose_frame(self.view)

    @property
    def session(self) -> Any:
        return self.view.session

    def press(self, *keys: str) -> list[str]:
        for key in keys:
            ctx = Ctx(
                session=self.session,
                fixture=self.view.fixture,
                host=_Host(),
                w=self.view.w,
                h=self.view.h,
                send=self.link,
                principal=bodies.ME,
                rows=self.rows,
                projection=self.view.projection,
            )
            dispatch(ctx, key, False)
            self.frame = compose_frame(self.view)
        return self.frame

    def card(self) -> Card:
        card = self.session.mutation
        assert isinstance(card, Card)
        return card

    def answer(self, target: str, disposition: ControlDisposition) -> None:
        status = {
            ControlDisposition.CONFIRMED: OperationStatus.APPLIED,
            ControlDisposition.REJECTED: OperationStatus.REFUSED,
        }.get(disposition, OperationStatus.OUTSTANDING)
        request = next(item.request for item in self.card().items if item.key == target)
        settle(
            self.session,
            OperationResult(
                operation_id=getattr(request, "operation_id", None),
                target=target,
                status=status,
                detail="",
                disposition=disposition,
            ),
            1001.0,
        )
        self.frame = compose_frame(self.view)

    def mark_two(self) -> None:
        self.session.sel_id = self.first
        self.frame = compose_frame(self.view)
        self.press(" ", "ArrowDown", " ")

    def outcome(self, target: str) -> ControlDisposition:
        return next(row.disposition for row in self.card().results if row.key == target)


def _sent() -> _Journey:
    """Return the home route with both planned Milestones activated in one card."""
    journey = _Journey("scope.home", MS_FIRST)
    journey.mark_two()
    journey.press(".", ACTIVATE, "Enter")
    return journey


# ---------- the selection, the menu title, the one preview ----------


def test_con_148_space_marks_two_runs_in_text_and_the_menu_counts_them() -> None:
    journey = _Journey()
    journey.mark_two()
    assert journey.session.marked == [FIRST, SECOND]
    assert len([row for row in journey.frame if row[2:3] == MARKED]) == 2
    journey.session.overlay = "actions"
    assert any("2 selected" in row for row in action_rows(journey.view))


def test_con_148_the_bulk_verb_previews_the_count_every_id_and_the_unknown_pane() -> None:
    journey = _Journey()
    journey.mark_two()
    journey.press(".", FINISH)
    assert journey.session.overlay == CARD
    selected = [row for row in journey.frame if FIRST in row or SECOND in row]
    assert len(selected) >= 2
    assert any(row.startswith(" UNKNOWN") for row in journey.frame)
    assert journey.link.sent == []


def test_con_148_a_verb_every_target_refuses_renders_the_refusal_and_sends_nothing() -> None:
    journey = _Journey()
    journey.mark_two()
    journey.press(".", FINISH, "Enter")
    assert journey.link.sent == []
    assert "refused before sending" in journey.frame[1]
    text = "\n".join(journey.frame)
    assert f"{FIRST} RUNNING refused" in text and f"{SECOND} RUNNING refused" in text


def test_con_148_escape_on_the_preview_sends_nothing() -> None:
    journey = _Journey()
    journey.mark_two()
    journey.press(".", FINISH, "Escape")
    assert journey.link.sent == []
    assert journey.session.overlay is None


# ---------- the three endings, one row per target ----------


def test_con_148_enter_sends_one_request_per_target() -> None:
    journey = _sent()
    assert [request.target for request in journey.link.sent] == [MS_FIRST, MS_SECOND]
    assert [row.key for row in journey.card().results] == [MS_FIRST, MS_SECOND]


def test_con_148_journey_every_target_confirms() -> None:
    journey = _sent()
    for target in (MS_FIRST, MS_SECOND):
        journey.answer(target, ControlDisposition.CONFIRMED)
    assert {journey.outcome(t) for t in (MS_FIRST, MS_SECOND)} == {ControlDisposition.CONFIRMED}
    rows = [row for row in journey.frame if row[3:].startswith(("MLS-0201 ", "MLS-0202 "))]
    assert len(rows) == 2 and all(row.rstrip().endswith("confirmed") for row in rows)


def test_con_148_journey_no_target_confirms() -> None:
    journey = _sent()
    for target in (MS_FIRST, MS_SECOND):
        journey.answer(target, ControlDisposition.REJECTED)
    assert {journey.outcome(t) for t in (MS_FIRST, MS_SECOND)} == {ControlDisposition.REJECTED}


def test_con_148_journey_exactly_one_does_not_and_reconcile_answers_it() -> None:
    journey = _sent()
    journey.answer(MS_FIRST, ControlDisposition.CONFIRMED)
    journey.answer(MS_SECOND, ControlDisposition.UNKNOWN)
    assert journey.outcome(MS_SECOND) in RECONCILABLE
    # the verdict is never collapsed: both rows stand, one each
    assert len(journey.card().results) == 2
    assert any(row.startswith(" UNKNOWN") for row in journey.frame)
    # reconcile on that row asks again, and its answer is what the row then reads
    journey.press("ArrowDown", "n")
    assert journey.link.sent[-1].target == MS_SECOND
    journey.answer(MS_SECOND, ControlDisposition.CONFIRMED)
    assert journey.outcome(MS_SECOND) is ControlDisposition.CONFIRMED
    assert journey.outcome(MS_FIRST) is ControlDisposition.CONFIRMED


def test_con_148_leaving_a_card_with_an_unknown_row_says_it_stays_unknown() -> None:
    journey = _sent()
    journey.answer(MS_FIRST, ControlDisposition.CONFIRMED)
    journey.answer(MS_SECOND, ControlDisposition.UNKNOWN)
    journey.press("Escape")
    assert journey.session.overlay is None
    assert "1 unknown rows stay unknown until reconcile answers" in journey.session.log[0].note


# ---------- every route that marks a row draws the mark ----------


def _marked_rows(frame: list[str], *keys: str) -> list[str]:
    return [row for row in frame if any(f"{MARKED}{key} " in row for key in keys)]


def test_space_on_scope_home_draws_the_mark_on_each_marked_leaf() -> None:
    journey = _Journey("scope.home", MS_FIRST)
    journey.mark_two()
    assert journey.session.marked == [MS_FIRST, MS_SECOND]
    assert len(_marked_rows(journey.frame, MS_FIRST, MS_SECOND)) == 2


def test_space_on_a_track_marks_the_milestone_under_the_cursor() -> None:
    journey = _Journey("track")
    journey.session.subj_id = "TRK-CORE"
    journey.frame = compose_frame(journey.view)
    row = journey.session.sel_id
    assert row in (MS_FIRST, MS_SECOND)
    journey.press(" ")
    assert journey.session.marked == [row]
    assert len(_marked_rows(journey.frame, row)) == 1


def test_space_with_no_row_under_the_cursor_says_why_rather_than_nothing() -> None:
    journey = _Journey()
    journey.session.sel_id = None
    journey.press(" ")
    assert journey.session.marked == []
    assert journey.session.log[0].note == NOTHING_TO_MARK


def test_the_mark_goes_only_beside_a_marked_key_that_opens_its_row() -> None:
    session = Session()
    session.marked = [FIRST]
    rows = [
        f" Eä ▸ EAWF ▸ {FIRST}",
        f"   ▸ {FIRST} running",
        f"   {SECOND} running",
        f"   note about {FIRST}",
    ]
    paint_marks(session, rows)
    assert rows == [
        f" Eä ▸ EAWF ▸ {FIRST}",
        f"   ▸{MARKED}{FIRST} running",
        f"   {SECOND} running",
        f"   note about {FIRST}",
    ]


def test_no_mark_leaves_every_row_as_it_was() -> None:
    rows = [" head", f"   {FIRST} running"]
    paint_marks(Session(), rows)
    assert rows == [" head", f"   {FIRST} running"]
