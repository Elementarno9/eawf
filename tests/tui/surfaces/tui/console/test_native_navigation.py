"""A native frame climbs, steps sideways and cycles along what the read model states.

``u`` and an Escape with no history climb the containment chain the rows name -- a Run to
its Task, a Milestone to its Track -- and ``[ ]`` walk the records filed under the same
parent. A light verb opens its surface on the record it was invoked from, and that surface
climbs back to it. Shift-Tab walks back through whatever Tab walks, a key the frame draws
no cursor for is not bound, and a key some handler claims but that changed nothing on
screen says why in a toast. Each test names the confirmation-jury finding it closes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eawf.kernel.projection.spine import build_spine_view
from eawf.kernel.projection.verification import build_verification_view
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.drawers import action_rows
from eawf.surfaces.tui.console.drill import NOTHING_CYCLES, say_why
from eawf.surfaces.tui.console.fixture import load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.overlays.palette import palette_hits
from eawf.surfaces.tui.console.palette import HitKind
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import Session, Toast
from eawf.surfaces.tui.console.tokens import Severity
from eawf.workflow.projection.acceptance import build_acceptance_view
from tests.tui.surfaces.tui.console import test_native_route_bodies as bodies
from tests.tui.surfaces.tui.console.test_native_drills import (
    PROTOTYPE_IDS,
    W,
    _bar,
    _Host,
    _live,
    _open,
    _press,
)
from tests.tui.surfaces.tui.console.test_native_windowing import SCOPE, _projection, _tracks


def _shift_tab(view: View) -> Session:
    """Dispatch Shift-Tab against ``view`` and redraw it, as the running console does."""
    ctx = Ctx(
        session=view.session,
        fixture=view.fixture,
        host=_Host(),
        w=view.w,
        h=view.h,
        projection=view.projection,
    )
    dispatch(ctx, "Tab", True)
    render_route(view)
    return view.session


def _milestone(subject: str) -> View:
    """Return a live view of one Milestone's frame over the probe tree."""
    model = build_acceptance_view(bodies._projection("milestone"))
    return _open("milestone", subject=subject, model=model)


def _verification(route: str, subject: str | None = None) -> View:
    """Return a live view of a verification route over the probe tree."""
    model = build_verification_view(bodies._projection(route), verdicts=())
    return _open(route, subject=subject, model=model)


# ---------- C2-02: the containment chain is the one the rows state ----------


def test_c2_02_u_climbs_a_run_to_the_task_it_is_an_attempt_at() -> None:
    session = _press(_open("run.detail", subject="RUN-00000001"), "u")
    assert (session.route, session.subj_id) == ("task.detail", "TSK-0001")


def test_c2_02_escape_with_no_history_climbs_a_milestone_to_its_track() -> None:
    session = _press(_milestone("MLS-0100"), "Escape")
    assert (session.route, session.subj_id) == ("track", "TRK-CORE")


def test_c2_02_escape_climbs_a_record_that_names_no_parent_to_scope_home() -> None:
    """TSK-0002 is filed in no Batch, so the climb has nowhere real to go but home."""
    session = _press(_open("task.detail", subject="TSK-0002"), "Escape")
    assert (session.route, session.subj_id) == ("scope.home", None)


def test_c2_02_brackets_walk_the_runs_of_one_task_and_wrap() -> None:
    """RUN-00000003 runs another Task, so it is never a sibling of the first two."""
    view = _open("run.detail", subject="RUN-00000001")
    assert _press(view, "]").subj_id == "RUN-00000002"
    assert _press(view, "]").subj_id == "RUN-00000001"
    assert _press(view, "[").subj_id == "RUN-00000002"


def test_c2_02_a_record_with_no_sibling_stays_put() -> None:
    session = _press(_milestone("MLS-0100"), "]")
    assert session.subj_id == "MLS-0100"
    assert session.log[0].note == "no sibling at this depth"


def test_c2_02_a_light_verb_opens_on_its_run_and_climbs_back_to_it() -> None:
    view = _open("run.detail", subject="RUN-00000002")
    session = _press(view, ".", "b")
    assert (session.route, session.subj_id) == ("git.pr", "RUN-00000002")
    session.back.clear()
    session = _press(view, "u")
    assert (session.route, session.subj_id) == ("run.detail", "RUN-00000002")


# ---------- C1-04: no prototype record on a live tree ----------


def test_c1_04_escape_from_trust_climbs_to_the_milestone_it_is_about() -> None:
    session = _press(_verification("trust", "MLS-0100"), "Escape")
    assert (session.route, session.subj_id) == ("milestone", "MLS-0100")


def test_c1_04_escape_from_a_subjectless_trust_or_evidence_lands_on_scope_home() -> None:
    for route in ("trust", "evidence"):
        session = _press(_verification(route), "Escape")
        assert (session.route, session.subj_id) == ("scope.home", None), route
        assert session.subj_id not in PROTOTYPE_IDS


# ---------- C2-03: the palette names the records the link holds ----------


def test_c2_03_the_palette_finds_a_held_milestone_and_opens_it_with_its_id() -> None:
    rows = bodies._projection("scope.home").rows
    hits = [h for h in palette_hits("mls", _live(), rows) if h.kind is HitKind.ENTITY]
    assert [(h.name, h.route, h.subject) for h in hits] == [("MLS-0100", "milestone", "MLS-0100")]


def test_c2_03_the_palette_routes_a_track_by_its_collection_not_its_prefix() -> None:
    rows = bodies._projection("scope.home").rows
    hits = palette_hits("trk-core", _live(), rows)
    assert [(h.route, h.subject) for h in hits] == [("track", "TRK-CORE")]


def test_c2_03_a_palette_over_no_held_row_names_no_entity() -> None:
    assert all(h.kind is HitKind.ROUTE for h in palette_hits("", _live(), ()))


# ---------- C2-06: Shift-Tab walks back through what Tab walks ----------


def test_c2_06_shift_tab_steps_back_through_the_backlog_groups() -> None:
    view = _open("backlog")
    forward = _press(view, "Tab").bl_group
    assert _shift_tab(view).bl_group != forward
    assert _shift_tab(view).bl_group == forward


def test_c2_06_shift_tab_on_home_answers_as_tab_does() -> None:
    """With nothing open on the attention list, both keys say so instead of moving."""
    document: dict[str, Any] = {**bodies.DOCUMENT, "pending_action": {}}
    view = _open("scope.home", document=document)
    _press(view, "Tab", document=document)
    tab = view.session.log[0].note
    _shift_tab(view)
    assert view.session.log[0].note == tab
    assert view.session.home_region != "attention"


def test_c2_06_a_task_frame_binds_no_tab_while_it_draws_no_region() -> None:
    view = _open("task.detail", subject="TSK-0001")
    for session in (_press(view, "Tab"), _shift_tab(view)):
        assert session.region is None
        assert session.trace is not None and session.trace.endswith("unclaimed")


def test_c2_06_shift_tab_where_nothing_cycles_says_the_same_as_tab() -> None:
    view = _verification("trust", "MLS-0100")
    assert _press(view, "Tab").log[0].note == NOTHING_CYCLES
    assert _shift_tab(view).log[0].note == NOTHING_CYCLES


# ---------- C2-07: home's paging keys land on leaves, and only where they act ----------


def _two_group_home() -> View:
    """Return home over one Track with 60 Milestones and two Milestones filed under none."""
    session = Session()
    session.route = "scope.home"
    track = next(iter(_tracks(1)))
    milestones = {
        f"MLS-{i:04d}": {
            "urn": f"urn:eawf:{SCOPE}:milestone:MLS-{i:04d}",
            "revision": 1,
            "status": "PLANNED",
            "primary_track_ref": f"urn:eawf:{SCOPE}:track:{track}",
        }
        for i in range(60)
    }
    for key in ("P37", "P38"):
        milestones[key] = {
            "urn": f"urn:eawf:{SCOPE}:milestone:{key}",
            "revision": 1,
            "status": "PLANNED",
        }
    spine = build_spine_view(
        _projection("scope.home", {"track": _tracks(1), "milestone": milestones})
    )
    view = View(session=session, fixture=_live(), w=W, h=30, projection=spine)
    render_route(view)
    return view


def test_c2_07_end_lands_on_the_last_leaf_of_the_last_group() -> None:
    view = _two_group_home()
    assert view.session.windowed
    assert _press(view, "End").sel_id == "P38"
    assert _press(view, "Home").sel_id == "MLS-0000"


def test_c2_07_page_down_steps_a_screen_of_leaves() -> None:
    view = _two_group_home()
    session = _press(view, "PageDown")
    assert session.sel_id == f"MLS-{max(1, session.visible):04d}"


def test_c2_07_an_unwindowed_tree_binds_no_paging_key() -> None:
    view = _open("scope.home")
    assert not view.session.windowed
    before = view.session.sel_id
    for key in ("End", "PageDown"):
        session = _press(view, key)
        assert session.sel_id == before
        assert session.trace is not None and session.trace.endswith("unclaimed")
    assert "Home End" not in _bar(view)


# ---------- C2-08: the timeline offers the region rows once Tab focuses them ----------


def test_c2_08_the_timeline_offers_rows_and_drill_once_a_region_holds_them() -> None:
    second = bodies._row("milestone", "MLS-0101", "PLANNED")
    document = {
        **bodies.DOCUMENT,
        "milestone": {**bodies.DOCUMENT["milestone"], "MLS-0101": second},
    }
    view = _open("timeline", document=document)
    assert "↑↓" not in _bar(view)
    session = _press(view, "Tab")
    bar = _bar(view)
    assert session.tl_reg == "UNDATED"
    assert "↑↓ row" in bar
    assert "Enter drill" in bar


# ---------- C2-11: evidence with no Claim held offers only the way back ----------


def test_c2_11_evidence_with_no_claim_draws_no_caret_and_offers_only_back() -> None:
    view = _verification("evidence")
    frame = render_route(view)
    bar = frame[-1]
    assert "↑↓" not in bar
    assert "Enter" not in bar
    assert "y copy" not in bar
    assert "Esc back" in bar
    assert not any(" ▸ 1 resolve" in row for row in frame)


def test_c2_11_enter_on_evidence_with_no_claim_opens_no_rung() -> None:
    session = _press(_verification("evidence"), "Enter")
    assert session.route == "evidence"
    assert "no Claim is held" in session.log[0].note


# ---------- C2-09: a claimed key that changed nothing says why ----------


def _ctx(view: View) -> Ctx:
    return Ctx(
        session=view.session,
        fixture=view.fixture,
        host=_Host(),
        w=view.w,
        h=view.h,
        projection=view.projection,
    )


def test_c2_09_a_claimed_key_that_left_the_frame_as_it_was_raises_a_toast() -> None:
    view = _milestone("MLS-0100")
    view.session.log_key("]", "no sibling at this depth")
    assert say_why(_ctx(view), "]", head=[], toasts=0, still=True)
    toast = view.session.toasts[-1]
    assert (toast.title, toast.text) == ("]", "no sibling at this depth")


def test_c2_09_a_key_that_moved_the_frame_or_toasted_already_raises_none() -> None:
    view = _milestone("MLS-0100")
    view.session.log_key("]", "sibling → MLS-0101")
    assert not say_why(_ctx(view), "]", head=[], toasts=0, still=False)
    view.session.log_key(".", "no verb for this route — nothing to open")
    view.session.toasts.append(Toast(title="NO ACTIONS", text="none", sev=Severity.WARN, at=0.0))
    assert not say_why(_ctx(view), ".", head=[], toasts=0, still=True)
    assert len(view.session.toasts) == 1


def test_c2_09_an_unclaimed_key_and_a_motion_key_at_its_edge_raise_none() -> None:
    view = _milestone("MLS-0100")
    head = view.session.log[:1]
    view.session.noop("z", verbose=False)
    assert not say_why(_ctx(view), "z", head=head, toasts=0, still=True)
    view.session.log_key("ArrowUp", "up")
    assert not say_why(_ctx(view), "k", head=head, toasts=0, still=True)
    assert view.session.toasts == []


def test_c2_09_the_prototype_replay_raises_no_toast() -> None:
    view = _milestone("MLS-0100")
    golden = load_fixture(Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture")
    view = View(session=view.session, fixture=golden, w=W, h=30)
    view.session.log_key("]", "no sibling at this depth")
    assert not say_why(_ctx(view), "]", head=[], toasts=0, still=True)


# ---------- C2-08: a finished subject's menu keeps its light verbs only ----------


def test_c2_08_a_finished_run_offers_its_menu_of_light_verbs_only() -> None:
    """RUN-00000001 has COMPLETED: Git and the transcript stay; no lifecycle verb is listed."""
    view = _open("run.detail", subject="RUN-00000001")
    assert ". actions" in _bar(view)
    session = _press(view, ".")
    assert session.overlay == "actions"
    listed = "\n".join(action_rows(view))
    assert "open branch and PR" in listed
    assert "cancel" not in listed and "retry" not in listed


def test_c2_08_a_lifecycle_letter_on_a_finished_run_previews_nothing() -> None:
    session = _press(_open("run.detail", subject="RUN-00000001"), ".", "c")
    assert session.overlay != "consequence"
    assert session.route == "run.detail"


def test_c2_08_a_finished_batch_with_no_light_verb_neither_offers_nor_binds_the_menu() -> None:
    batch = bodies._row("batch", "BAT-0100", "COMPLETED")
    batch["milestone_ref"] = bodies.DOCUMENT["batch"]["BAT-0100"]["milestone_ref"]
    document = {**bodies.DOCUMENT, "batch": {"BAT-0100": batch}}
    view = _open("batch.detail", subject="BAT-0100", document=document)
    assert ". actions" not in _bar(view)
    session = _press(view, ".", document=document)
    assert session.overlay is None
    assert session.trace is not None and session.trace.endswith("unclaimed")
