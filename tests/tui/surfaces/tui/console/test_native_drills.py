"""A native frame's keys act on the row its caret names, and on nothing else.

Every drill from a frame drawn from a read model carries the id of the row under the
caret as the destination's subject, and a caret that names no row drills nowhere. Scope
home's cursor lands on Milestone leaves only, its Tab focuses the attention list only when
the list holds something, and a Run frame stays about one Run whatever the arrows do. No
prototype record is reachable from a live tree, and a keybar offers only keys that act on
what the frame holds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eawf.kernel.projection.operations import build_operations_view
from eawf.kernel.projection.registers import build_register_view
from eawf.kernel.projection.spine import build_spine_view
from eawf.surfaces.tui.console.attention import selected_open_row
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch, parent_of
from eawf.surfaces.tui.console.drill import NO_EVENTS, NOTHING_SELECTED, NOTHING_WAITING
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import Receded, View
from eawf.surfaces.tui.console.header import header_row
from eawf.surfaces.tui.console.navigation import Ctx, remember
from eawf.surfaces.tui.console.operations import DISPATCH_QUEUE_TARGET
from eawf.surfaces.tui.console.overlays.drawn import draw_overlay
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.read_model import crumb
from eawf.surfaces.tui.console.session import Session
from tests.tui.surfaces.tui.console import decision_support as ds
from tests.tui.surfaces.tui.console import test_native_route_bodies as bodies

W, H = 120, 30
#: The prototype records a live tree must never reach.
PROTOTYPE_IDS = ("MLS-0001", "MLS-0004", "MLS-0007", "CAM-0001", "CLM-0004", "REL-0001")
#: The probe tree with nothing open on the Attention register.
QUIET: dict[str, Any] = {
    **bodies.DOCUMENT,
    "pending_action": {"ACT-0003": bodies.DOCUMENT["pending_action"]["ACT-0003"]},
}


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


def _live() -> Fixture:
    """Return the fixture a console built from the packaged chrome holds: no prototype row."""
    return Fixture.from_chrome(load_chrome())


def _open(
    route: str,
    *,
    subject: str | None = None,
    document: dict[str, Any] | None = None,
    model: Any = None,
) -> View:
    """Return a live view of ``route`` over the probe tree, drawn once as the app does."""
    session = Session()
    session.route, session.subj_id = route, subject
    held: dict[str, Any] = {}
    # the port keys a route by its own name where the normalisation map renames it
    key = REGISTRY.by_id[route].key
    if model is not None:
        held["projection"] = model
    elif route in ("activity", "attention", "cost.ceiling"):
        held["register"] = build_register_view(bodies._projection(key, document))
    else:
        held["projection"] = build_spine_view(bodies._projection(key, document))
    view = View(
        session=session,
        fixture=_live(),
        w=W,
        h=H,
        linked=True,
        principal=bodies.ME,
        attention=bodies._attention(document),
        **held,
    )
    render_route(view)
    return view


def _press(view: View, *keys: str, document: dict[str, Any] | None = None) -> Session:
    """Dispatch ``keys`` against ``view``, redrawing while the route is the one drawn."""
    session, route = view.session, view.session.route
    for key in keys:
        ctx = Ctx(
            session=session,
            fixture=view.fixture,
            host=_Host(),
            w=W,
            h=H,
            projection=view.projection,
            attention=bodies._projection("attention", document),
            principal=bodies.ME,
        )
        dispatch(ctx, key, False)
        if session.route == route and session.overlay is None:
            render_route(view)
    return session


def _bar(view: View) -> str:
    return render_route(view)[-1]


# ---------- scope home: leaves only, Enter and Tab ----------


def test_home_cursor_starts_on_a_milestone_leaf_never_the_track() -> None:
    view = _open("scope.home")
    assert view.session.sel_id == "MLS-0100"
    frame = render_route(view)
    assert any(row.startswith("   ▸ MLS-0100") for row in frame)
    assert not any(row.startswith(" ▸ TRK-CORE") for row in frame)


def test_home_arrows_step_between_leaves_and_never_onto_the_track() -> None:
    view = _open("scope.home")
    for key in ("ArrowUp", "ArrowDown", "ArrowDown"):
        _press(view, key)
        assert view.session.sel_id == "MLS-0100"


def test_home_enter_drills_the_leaf_with_it_as_subject() -> None:
    session = _press(_open("scope.home"), "Enter")
    assert (session.route, session.subj_id) == ("milestone", "MLS-0100")
    assert session.back.items()[-1].route == "scope.home"


def test_home_tab_with_nothing_waiting_says_so_and_keeps_the_focus() -> None:
    view = _open("scope.home", document=QUIET)
    assert "Tab list" not in _bar(view)
    session = _press(view, "Tab", document=QUIET)
    assert session.region is None
    assert session.log[0].note == NOTHING_WAITING
    assert [toast.text for toast in session.toasts] == [NOTHING_WAITING]


def test_home_tab_focuses_the_attention_list_and_enter_opens_its_item() -> None:
    view = _open("scope.home")
    assert "Tab list" in _bar(view)
    session = _press(view, "Tab")
    assert session.home_region == "attention"
    frame = render_route(view)
    assert any(row.startswith(" ▸ RUN-00000002") for row in frame)
    assert not any(" ▸ MLS-0100" in row for row in frame)
    # the tree no longer owns the arrows, so it recedes
    tree = next(row for row in frame if "MLS-0100" in row)
    assert isinstance(tree, Receded)
    _press(view, "Enter")
    assert (session.route, session.sel_id) == ("attention", "ACT-0001")


# ---------- a Run frame stays about one Run ----------


def test_run_detail_arrows_never_move_to_another_run() -> None:
    view = _open("run.detail")
    pinned = view.session.subj_id
    assert pinned is not None
    for key in ("ArrowDown", "ArrowDown", "End", "PageDown"):
        _press(view, key)
        assert view.session.subj_id == pinned
    assert view.session.log[0].note == NO_EVENTS
    assert "↑↓" not in _bar(view)


def test_a_run_subject_the_register_does_not_hold_is_not_swapped_for_another() -> None:
    frame = render_route(_open("run.detail", subject="RUN-99999999"))
    text = "\n".join(frame)
    assert "RUN-99999999 is not held" in text
    assert "RUN-00000001" not in frame[1]


# ---------- every drill carries its subject ----------


def test_activity_enter_drills_the_run_under_the_caret() -> None:
    view = _open("activity")
    _press(view, "ArrowDown")
    selected = view.session.sel_id
    session = _press(view, "Enter")
    assert (session.route, session.subj_id) == ("run.detail", selected)


def test_activity_enter_on_a_bucket_holding_nothing_is_a_no_op() -> None:
    view = _open("activity")
    view.session.bucket = "perm"
    view.session.filters["activity"] = "no such run"
    render_route(view)
    assert view.session.sel_id is None
    session = _press(view, "Enter")
    assert (session.route, session.subj_id) == ("activity", None)
    assert session.log[0].note == NOTHING_SELECTED


def test_task_enter_drills_the_row_under_the_caret_by_its_collection() -> None:
    view = _open("task.detail")
    _press(view, "ArrowDown")
    selected = view.session.sel_id
    assert selected == "TSK-0002"
    session = _press(view, "Enter")
    assert (session.route, session.subj_id) == ("task.detail", selected)


def test_batch_enter_drills_the_child_task_under_the_caret() -> None:
    view = _open("batch.detail", subject="BAT-0100")
    assert view.session.sel_id == "TSK-0001"
    session = _press(view, "Enter")
    assert (session.route, session.subj_id) == ("task.detail", "TSK-0001")


def test_a_batch_with_no_child_offers_no_enter_and_says_so_when_pressed() -> None:
    document = {**bodies.DOCUMENT, "task": {}}
    view = _open("batch.detail", subject="BAT-0100", document=document)
    assert "Enter" not in _bar(view)
    session = _press(view, "Enter")
    assert (session.route, session.subj_id) == ("batch.detail", "BAT-0100")
    assert session.toasts and "BAT-0100" in session.toasts[-1].text


def test_track_enter_drills_its_milestone_never_a_prototype_campaign() -> None:
    view = _open("track", subject="TRK-CORE")
    assert "Tab" in _bar(view)
    session = _press(view, "Enter")
    assert (session.route, session.subj_id) == ("milestone", "MLS-0100")
    view = _open("track", subject="TRK-CORE")
    session = _press(view, "Tab", "Enter")
    assert session.route == "track" and session.subj_id not in PROTOTYPE_IDS
    # the plain Track register draws no group for Tab to walk
    assert "Tab" not in _bar(_open("track"))


def test_timeline_enter_drills_the_row_never_a_prototype_marker() -> None:
    view = _open("timeline")
    assert "Enter" not in _bar(view), "a lane states no dated marker to open"
    session = _press(view, "Enter")
    assert (session.route, session.overlay) == ("timeline", None)
    session = _press(view, "Tab", "Enter")
    assert session.overlay is None
    assert (session.route, session.subj_id) == ("milestone", "MLS-0100")


def test_search_enter_drills_the_hit_under_the_caret() -> None:
    view = _open("search")
    _press(view, "ArrowDown")
    selected = view.session.sel_id
    assert selected is not None
    session = _press(view, "Enter")
    assert session.subj_id == selected
    assert session.route != "search"


def test_history_enter_never_opens_a_prototype_fact() -> None:
    view = _open("history")
    assert view.session.sel_id is None, "the ledger lists no fact until a feed is served"
    session = _press(view, "Enter")
    assert (session.route, session.overlay, session.ov_subject) == ("history", None, None)
    assert session.log[0].note == NOTHING_SELECTED


def test_milestone_enter_on_its_own_row_opens_its_acceptance_evidence() -> None:
    view = _open("milestone", subject="MLS-0030", model=ds.milestone_view(None))
    session = _press(view, "Enter")
    assert (session.overlay, session.ov_subject) == ("acceptance", "MLS-0030")
    assert "Tab" not in _bar(_open("milestone", model=ds.milestone_view(None)))


def test_an_empty_backlog_offers_no_row_key() -> None:
    tasks = {key: {**row, "status": "RUNNING"} for key, row in bodies.DOCUMENT["task"].items()}
    bar = _bar(_open("backlog", document={**bodies.DOCUMENT, "task": tasks}))
    assert "↑↓" not in bar and "Enter" not in bar and "Tab" not in bar


def test_release_enter_drills_the_membership_milestone() -> None:
    view = _open("release", model=ds.release_view("CANDIDATE"))
    assert view.session.sel_id == "MLS-0030"
    session = _press(view, "Enter")
    assert (session.route, session.subj_id) == ("milestone", "MLS-0030")


def test_backlog_enter_opens_the_draft_card_on_the_row_under_the_caret() -> None:
    tasks = {key: {**row, "status": "DRAFT"} for key, row in bodies.DOCUMENT["task"].items()}
    view = _open("backlog", document={**bodies.DOCUMENT, "task": tasks})
    _press(view, "ArrowDown")
    selected = view.session.sel_id
    assert selected is not None
    session = _press(view, "Enter")
    assert (session.overlay, session.ov_subject) == ("draft", selected)
    # the card is bound to the backlog row itself, not to a record the link never read
    card = draw_overlay("draft", view)
    assert card is not None
    text = "\n".join(card)
    assert selected in card[0] and "draft" in card[1] and "is not held" not in text
    assert "Seal the ledger · draft" in card[1]


# ---------- attention: nothing open, nothing sealed ----------


def test_enter_on_an_attention_frame_with_nothing_open_opens_no_card() -> None:
    view = _open("attention", document=QUIET)
    assert view.session.sel_id is None
    session = _press(view, "Enter", document=QUIET)
    assert session.overlay is None
    assert "Enter" not in _bar(view)


def test_a_sealed_row_is_never_selected_for_an_answer() -> None:
    session = Session()
    session.route, session.sel_id = "attention", "ACT-0003"
    assert selected_open_row(session, bodies._projection("attention")) is None
    session.sel_id = "ACT-0001"
    row = selected_open_row(session, bodies._projection("attention"))
    assert row is not None and row.key == "ACT-0001"
    session.sel_id = None
    assert selected_open_row(session, bodies._projection("attention")) is None


# ---------- no prototype record on a live tree ----------


def test_escape_from_a_fixed_parent_route_climbs_without_a_prototype_subject() -> None:
    for route in ("trust", "evidence"):
        session = Session()
        session.route = route
        up = parent_of(session, _live())
        assert up is not None and up[1] is None, route


def test_a_subjectless_native_crumb_names_no_prototype_record() -> None:
    model = build_spine_view(bodies._projection("campaign"))
    for route in ("evidence", "campaign", "release"):
        view = _open("campaign")
        view.session.route = route
        text = crumb(view, model)
        assert not any(found in text for found in PROTOTYPE_IDS), text
        assert text.endswith(REGISTRY.route_word(route)), text


def test_no_drill_on_the_probe_tree_reaches_a_prototype_record() -> None:
    reached: list[str] = []
    for route in ("track", "timeline", "cost.ceiling", "history", "search"):
        session = _press(_open(route), "Tab", "Enter")
        reached += [value for value in (session.subj_id, session.ov_subject) if value]
    assert not [found for found in reached if found in PROTOTYPE_IDS]


# ---------- keys shown are the keys that work ----------


def test_a_one_row_frame_offers_no_arrows_and_an_empty_one_no_enter() -> None:
    track = _bar(_open("track", subject="TRK-CORE"))
    assert "↑↓" not in track and "Home End" not in track and "Enter drill" in track
    ceiling = _bar(_open("cost.ceiling"))
    assert "↑↓" not in ceiling and "Enter" not in ceiling


def test_paging_is_offered_only_when_the_table_is_windowed() -> None:
    assert "PageUp PageDown" not in _bar(_open("task.detail"))
    assert "↑↓ row" in _bar(_open("task.detail"))


def test_the_prototype_replay_keeps_its_golden_keybar() -> None:
    session = Session()
    session.route = "track"
    golden = load_fixture(Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture")
    frame = render_route(View(session=session, fixture=golden, w=W, h=H))
    assert "Tab group" in frame[-1] and "↑↓ row" in frame[-1]


def test_unattended_requests_address_no_prototype_queue_on_a_live_tree() -> None:
    view = _open("unattended", model=build_operations_view(bodies._projection("unattended")))
    assert "request pause" in _bar(view)
    session = _press(view, "a")
    assert session.overlay == "consequence" and session.c_target is not None
    assert session.c_target["id"] == DISPATCH_QUEUE_TARGET
    assert session.c_target["id"] not in PROTOTYPE_IDS


def test_the_back_stack_crumb_names_no_prototype_record_on_a_live_tree() -> None:
    session = Session()
    session.route = "evidence"
    session.back.record(remember(session))
    session.route = "evidence.digest"
    live = header_row(session, crumb=" Eä ▸ EAWF ▸ Rung 1", scope="EAWF", needs=0, w=W)
    assert not any(found in live for found in PROTOTYPE_IDS), live
    replay = header_row(
        session, crumb=" Eä ▸ EAWF ▸ Rung 1", scope="EAWF", needs=0, w=W, prototype=True
    )
    assert "CLM-0004" in replay
