"""Focus regions are bounded at three, and a selection survives a patch that reorders rows.

Two chassis rules meet on the spine. A route declares the places the focus moves between,
and there are at most three of them: a fourth is a frame an operator has to remember
rather than read, so the registry refuses to build over the bound. And a selection is held
by stable id, never by row offset -- a keyed patch may insert a record above the cursor,
and an offset kept across that insert lands on a neighbour, which is a console quietly
selecting something the operator did not.

The patch leg here is the production one: the patch goes through the seam's own sink, the
seam rebuilds the projection through the daemon's builder, and the frame reads the
rebuilt rows. Nothing reorders rows by hand.

The last rule is the one the native mode must not cost: the epoch-1 home mode, which the
tracked golden contract replays, keeps every key its footer advertises resolving. A footer
that advertises a key nothing handles is a frame promising an action it does not have.

CON-149, CON-151 and CON-152 are held at the end: the ``u``, bracket and ``!``
journeys, the docked readout that follows the cursor by id, and the roadmap marker
cursor drawn in text.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.projection.compute import KeyedPatch, RouteProjection, build_route_projection
from eawf.kernel.projection.spine import ENTRY_ROUTE, SPINE_ROUTES, SpineView, build_spine_view
from eawf.surfaces.tui.console.app import (
    Body,
    ConsoleApp,
    KeybarRow,
    ProjectionHeader,
    compose_frame,
)
from eawf.surfaces.tui.console.attention import is_notice, open_actions
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch, siblings_of
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.keybar import KEY_NAMES
from eawf.surfaces.tui.console.keymap import allowlist, route_keys
from eawf.surfaces.tui.console.navigation import (
    Ctx,
    cycle_region,
    focused_region,
    go,
    recall,
    regions_of,
    remember,
)
from eawf.surfaces.tui.console.overlays.resolution import ENDINGS, Ending
from eawf.surfaces.tui.console.registry import (
    FOCUS_REGION_LIMIT,
    REGISTRY,
    ROOT_ROUTE,
    ROUTES,
    RouteRegistry,
)
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.read_model import restore
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import BACK_CAP, BackEntry, Session, SessionSetup
from tests.tui.surfaces.tui.console import test_native_route_bodies as bodies
from tests.tui.surfaces.tui.console import test_native_route_frames as frames

AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
SCOPE = "EAWF"
#: A route whose every row takes the cursor: scope home's Track rows are containers the
#: cursor never lands on, so the selection-by-id suite walks the Track route instead.
ROUTE = "track"

#: The six routes the spine binds: the five a projection carries, and the entry layer.
BOUND_ROUTES: tuple[str, ...] = (ENTRY_ROUTE, *SPINE_ROUTES)

#: The subject each detail route opens onto so its own body renders rather than the
#: absent frame; the two list routes open without one.
OWN_SUBJECT: dict[str, str | None] = {
    ENTRY_ROUTE: None,
    "scope.home": None,
    "track": "Runtime",
    "batch.detail": "BAT-0001",
    "task.detail": "EAWF-0001",
    "run.detail": "RUN-9e3779b1",
}

#: Three tracks whose keys sort in the order they are written, so a fourth key inserted
#: ahead of the selection is what moves it.
TRACKS: dict[str, Any] = {
    "TRK-0002": {"urn": f"urn:eawf:{SCOPE}:track:TRK-0002", "revision": 1, "status": "ACTIVE"},
    "TRK-0003": {"urn": f"urn:eawf:{SCOPE}:track:TRK-0003", "revision": 1, "status": "PAUSED"},
    "TRK-0004": {"urn": f"urn:eawf:{SCOPE}:track:TRK-0004", "revision": 1, "status": "ACTIVE"},
}


def _fixture() -> Fixture:
    """Return the tracked prototype registers the epoch-1 mode renders from."""
    root = Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"
    return load_fixture(root)


def _projection(cursor: int = 41208, *, tracks: Any = None) -> RouteProjection:
    """Return the scope-home projection over ``tracks`` at ``cursor``."""
    return build_route_projection(
        route=ROUTE,
        document={"track": TRACKS if tracks is None else tracks},
        cursor=cursor,
        scope_id=SCOPE,
        generated_at=AT,
    )


def _patch(key: str, *, sequence: int, status: str = "ACTIVE") -> KeyedPatch:
    """Return the keyed patch one committed transition on ``key`` publishes."""
    return KeyedPatch.model_validate(
        {
            "schema_version": "1.0",
            "projection_kind": "scope_home_view",
            "routes": [ROUTE],
            "scope_id": SCOPE,
            "canonical_sequence": sequence,
            "entries": [
                {
                    "key": key,
                    "urn": f"urn:eawf:{SCOPE}:track:{key}",
                    "collection": "track",
                    "revision": 1,
                    "status": status,
                }
            ],
        }
    )


def _seam_holding(projection: RouteProjection) -> ProjectionSeam:
    """Return a seam bound to no tree, already holding ``projection``.

    The held projection is normally adopted from a read; a test that wants one particular
    starting projection states it directly, as the connection-state suite does.
    """
    seam = ProjectionSeam(route=ROUTE, scope_id=SCOPE, state_path=None, clock=lambda: AT)
    seam._projection = projection
    return seam


def _render(spine: SpineView, session: Session) -> list[str]:
    """Return the native frame ``spine`` renders into ``session``."""
    view = View(session=session, fixture=_fixture(), w=120, h=24, projection=spine)
    return render_route(view)


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


# ---------- focus regions, bounded at three ----------


@pytest.mark.parametrize("route", BOUND_ROUTES)
def test_every_bound_route_declares_at_most_three_focus_regions(route: str) -> None:
    """The chassis bound holds on every route the spine binds, and each declares one."""
    regions = REGISTRY.focus_regions[route]
    assert 1 <= len(regions) <= FOCUS_REGION_LIMIT
    assert len(set(regions)) == len(regions)


def test_the_focus_region_limit_is_three() -> None:
    """The bound is stated once; a change to it fails here rather than in a frame."""
    assert FOCUS_REGION_LIMIT == 3


def test_no_route_anywhere_exceeds_the_limit() -> None:
    """The shipped table is inside the bound, not only the routes this wave touched."""
    assert all(len(spec.focus_regions) <= FOCUS_REGION_LIMIT for spec in ROUTES)


def test_a_fourth_focus_region_refuses_to_build() -> None:
    """A route with a fourth place for the arrows is a registry that will not build."""
    rows = tuple(
        dataclasses.replace(spec, focus_regions=("a", "b", "c", "d"))
        if spec.id == "track"
        else spec
        for spec in ROUTES
    )
    with pytest.raises(ValueError, match=re.escape("track declares 4")):
        RouteRegistry(rows)


def test_a_repeated_focus_region_refuses_to_build() -> None:
    """One region named twice is a cycle that visits a place twice; it is refused."""
    rows = tuple(
        dataclasses.replace(spec, focus_regions=("a", "a")) if spec.id == "track" else spec
        for spec in ROUTES
    )
    with pytest.raises(ValueError, match=re.escape("track: a")):
        RouteRegistry(rows)


def test_a_route_may_declare_no_focus_region() -> None:
    """A frame with one place for the arrows declares nothing, and is not a hole."""
    assert set(REGISTRY.focus_regions) < set(REGISTRY.ids)
    row = next(spec for spec in ROUTES if not spec.focus_regions)
    assert row.id not in REGISTRY.focus_regions


def test_exactly_three_regions_is_admitted() -> None:
    """The bound is inclusive: three is the limit, not the first refusal."""
    rows = tuple(
        dataclasses.replace(spec, focus_regions=("a", "b", "c")) if spec.id == "track" else spec
        for spec in ROUTES
    )
    assert RouteRegistry(rows).focus_regions["track"] == ("a", "b", "c")


#: The spine routes whose native frame is still the shared record table, which names its
#: focus regions on a ``REGIONS`` line. Home and the Run frame draw each region as a
#: section of the packet layout instead, which the two tests below hold.
TABLE_ROUTES: tuple[str, ...] = tuple(
    r for r in SPINE_ROUTES if r not in ("scope.home", "run.detail")
)


def test_the_home_frame_draws_both_its_regions_as_sections() -> None:
    """Home's outcome tree and its attention list are each drawn, one under the other."""
    spine = build_spine_view(
        build_route_projection(
            route="scope.home", document={}, cursor=7, scope_id=SCOPE, generated_at=AT
        )
    )
    session = Session()
    session.route = "scope.home"
    rows = _render(spine, session)
    assert REGISTRY.focus_regions["scope.home"] == ("outcomes", "attention")
    assert any(row.startswith("   MILESTONES") for row in rows)
    assert any(row.startswith(" ATTENTION") for row in rows)


def test_the_run_frame_draws_its_timeline_region() -> None:
    """The Run frame's one focus region, its timeline, is drawn as the section it names."""
    spine = build_spine_view(
        build_route_projection(
            route="run.detail", document={}, cursor=7, scope_id=SCOPE, generated_at=AT
        )
    )
    session = Session()
    session.route = "run.detail"
    rows = _render(spine, session)
    assert REGISTRY.focus_regions["run.detail"] == ("timeline",)
    assert rows[3].startswith(" TIMELINE"), "the timeline pane opens the frame"


@pytest.mark.parametrize("route", TABLE_ROUTES)
def test_the_native_frame_names_the_route_regions(route: str) -> None:
    """A native frame says which places the focus moves between."""
    spine = build_spine_view(
        build_route_projection(route=route, document={}, cursor=7, scope_id=SCOPE, generated_at=AT)
    )
    session = Session()
    session.route = route
    rows = _render(spine, session)
    regions = next(row for row in rows if row.startswith(" REGIONS"))
    for name in REGISTRY.focus_regions[route]:
        assert name in regions


# ---------- the selection survives a patch that reorders the rows ----------


def test_selection_restores_by_id_after_a_patch_inserts_a_row_above_it() -> None:
    """The cursor moves with its row, not with the offset the row used to be at."""
    seam = _seam_holding(_projection())
    session = Session()
    session.route = ROUTE
    session.sel, session.sel_id = 1, "TRK-0003"
    before = _render(build_spine_view(seam.projection), session)
    assert session.sel == 1

    asyncio.run(seam.apply_patch(_patch("TRK-0001", sequence=41209)))
    after = _render(build_spine_view(seam.projection), session)

    assert session.sel == 2
    assert session.sel_id == "TRK-0003"
    assert before != after


def test_the_patched_projection_stands_at_the_patch_cursor() -> None:
    """The rows the frame draws are the rows at the ordinal the patch carried."""
    seam = _seam_holding(_projection())
    asyncio.run(seam.apply_patch(_patch("TRK-0001", sequence=41209)))
    spine = build_spine_view(seam.projection)
    assert spine.source_cursor == "41209"
    assert [row.key for row in spine.rows] == ["TRK-0001", "TRK-0002", "TRK-0003", "TRK-0004"]
    assert spine.count("track") == 4


def test_a_patch_on_the_selected_row_keeps_the_selection_where_it_is() -> None:
    """A status move is not a reorder; the cursor does not travel for one."""
    seam = _seam_holding(_projection())
    session = Session()
    session.route = ROUTE
    session.sel, session.sel_id = 1, "TRK-0003"
    _render(build_spine_view(seam.projection), session)

    asyncio.run(seam.apply_patch(_patch("TRK-0003", sequence=41209, status="COMPLETED")))
    spine = build_spine_view(seam.projection)
    _render(spine, session)

    assert session.sel == 1
    assert session.sel_id == "TRK-0003"
    assert spine.rows[1].field("status").value == "COMPLETED"


def test_a_selection_naming_a_row_the_projection_lost_is_reported_not_slid() -> None:
    """A vanished selection is a resolution the operator owns, not a silent neighbour."""
    seam = _seam_holding(_projection())
    seam.select("TRK-0009")
    assert seam._selection_missing() is True
    seam.select("TRK-0003")
    assert seam._selection_missing() is False


def test_index_of_answers_none_for_an_id_no_row_carries() -> None:
    """A stable id that is gone resolves to nothing rather than to a position."""
    spine = build_spine_view(_projection())
    assert spine.index_of("TRK-0003") == 1
    assert spine.index_of("TRK-0009") is None
    assert spine.index_of(None) is None


def test_restore_on_an_empty_read_model_selects_nothing() -> None:
    """With no rows there is no selection to publish, and no index out of range."""
    spine = build_spine_view(_projection(tracks={}))
    session = Session()
    session.route, session.sel, session.sel_id = ROUTE, 4, "TRK-0003"
    assert restore(session, spine) == 0
    assert session.sel_id is None


def test_restore_clamps_an_offset_past_the_last_row() -> None:
    """An offset kept from a longer projection lands on the last row, never past it."""
    spine = build_spine_view(_projection())
    session = Session()
    session.route, session.sel, session.sel_id = ROUTE, 9, None
    assert restore(session, spine) == 2
    assert session.sel_id == "TRK-0004"


def test_restore_publishes_the_selected_id_for_the_next_patch() -> None:
    """The id the next reorder restores by is published by the render that drew it."""
    spine = build_spine_view(_projection())
    session = Session()
    session.route, session.sel, session.sel_id = ROUTE, 0, None
    restore(session, spine)
    assert session.sel_id == "TRK-0002"


def test_a_patch_for_another_route_does_not_reach_this_seam() -> None:
    """One feed carries every route; a patch that is not ours changes nothing."""
    seam = _seam_holding(_projection())
    other = _patch("TRK-0001", sequence=41209).model_copy(update={"routes": ("scope.home",)})
    asyncio.run(seam.apply_patch(other))
    assert [row.key for row in seam.projection.rows] == ["TRK-0002", "TRK-0003", "TRK-0004"]


# ---------- the epoch-1 home mode still resolves every advertised key ----------


#: The dispatcher name of every key the bar prints under another name. The entry layer
#: builds its table from the prototype register, which spells its keys the way the bar
#: prints them, so a footer token is resolved back before it is pressed.
_DISPATCHER_NAME: dict[str, str] = {name: key for key, name in KEY_NAMES.items()}


def _advertised(route: str, fixture: Fixture) -> list[str]:
    """Return the dispatcher key name of every key the route's footer advertises."""
    session = Session()
    session.route = route
    names: list[str] = []
    for entry in route_keys(session, fixture, route):
        for token in entry.keys:
            glyphs = list(token) if all(ch in _DISPATCHER_NAME for ch in token) else [token]
            names.extend(_DISPATCHER_NAME.get(glyph, glyph) for glyph in glyphs)
    return names


@pytest.mark.parametrize("route", BOUND_ROUTES)
def test_the_epoch_one_frame_resolves_every_key_its_footer_advertises(route: str) -> None:
    """A footer that advertises a key nothing handles promises an action it does not have."""
    fixture = _fixture()
    unclaimed: list[str] = []
    for key in _advertised(route, fixture):
        session = Session()
        session.route = route
        session.subj_id = OWN_SUBJECT[route]
        render_route(View(session=session, fixture=fixture, w=120, h=30))
        ctx = Ctx(session=session, fixture=fixture, host=_Host(), w=120, h=30)
        dispatch(ctx, key, False)
        if session.trace is None or session.trace.endswith("unclaimed"):
            unclaimed.append(f"{route}:{key}")
    assert not unclaimed, f"advertised but unhandled: {', '.join(unclaimed)}"


@pytest.mark.parametrize("route", BOUND_ROUTES)
def test_every_bound_route_advertises_at_least_one_key(route: str) -> None:
    """A frame with no footer key is one an operator cannot leave."""
    assert _advertised(route, _fixture())


def test_an_unadvertised_key_is_recorded_as_unclaimed() -> None:
    """The claim check has teeth: a key no route binds reads as unclaimed."""
    fixture = _fixture()
    session = Session()
    session.route = ROUTE
    render_route(View(session=session, fixture=fixture, w=120, h=30))
    dispatch(Ctx(session=session, fixture=fixture, host=_Host(), w=120, h=30), "Q", False)
    assert session.trace is not None
    assert session.trace.endswith("unclaimed")


# ---------- the focus and navigation grammar ----------
#
# Each test below names the console requirement row it proves. The keys go through the
# production dispatcher over the tracked registers, the way the app presses them.


def _draw(session: Session) -> list[str]:
    """Compose the full frame the app paints, which also clears last frame's published rows."""
    return compose_frame(View(session=session, fixture=_fixture(), w=120, h=30))


def _session(route: str, subj: str | None = None) -> Session:
    """Return a session on ``route`` whose frame has been drawn once, as the app does."""
    session = Session()
    session.route = route
    session.subj_id = subj
    _draw(session)
    return session


def _press(session: Session, *keys: str, shift: bool = False, projection: Any = None) -> None:
    """Dispatch ``keys`` in order, drawing the frame after each as the app does."""
    fixture = _fixture()
    for key in keys:
        ctx = Ctx(
            session=session, fixture=fixture, host=_Host(), w=120, h=30, projection=projection
        )
        dispatch(ctx, key, shift)
        if projection is None:
            _draw(session)


def test_con_013_no_route_declares_more_than_three_regions_and_bucket_routes_two() -> None:
    """CON-013: at most three regions, and a bucket-rail route at most window and detail."""
    assert all(len(regions_of(spec.id)) <= FOCUS_REGION_LIMIT for spec in ROUTES)
    for route in ("activity", "attention"):
        assert len(regions_of(route)) <= 2
        assert "buckets" not in regions_of(route)


def test_con_013_tab_and_shift_tab_cycle_the_declared_regions() -> None:
    """CON-013: Tab walks the route's regions forward and Shift-Tab walks them back."""
    session = _session("task.detail", "EAWF-0054")
    assert focused_region(session) == "criteria"
    _press(session, "Tab")
    assert focused_region(session) == "runs"
    _press(session, "Tab")
    assert focused_region(session) == "criteria"
    _press(session, "Tab", shift=True)
    assert focused_region(session) == "runs"
    assert session.trace == "S-Tab → focus → runs"


def test_con_013_the_home_focus_treatment_moves_with_the_region() -> None:
    """CON-013: the focused home pane is drawn differently in text, not in colour alone."""
    session = _session("scope.home")
    before = _draw(session)
    _press(session, "Tab")
    after = _draw(session)
    assert session.home_region in regions_of("scope.home")
    assert session.home_region == "attention"
    assert [str(row) for row in before] != [str(row) for row in after]


def test_con_014_arrows_never_leave_the_focused_region() -> None:
    """CON-014: holding a direction stays inside the region, at either end of it."""
    session = _session("scope.home")
    _press(session, *(["ArrowDown"] * 40))
    assert session.home_region != "attention"
    _press(session, *(["ArrowUp"] * 40))
    assert session.home_region != "attention"
    detail = _session("task.detail", "EAWF-0054")
    _press(detail, "Tab", *(["ArrowDown"] * 12), *(["ArrowUp"] * 12), "ArrowLeft", "ArrowRight")
    assert focused_region(detail) == "runs"
    assert (detail.route, detail.subj_id) == ("task.detail", "EAWF-0054")


def _selected_on(spine: SpineView, session: Session) -> tuple[int, str | None]:
    """Draw ``spine`` into ``session`` and return the caret offset and selected id."""
    _render(spine, session)
    return session.sel, session.sel_id


def test_con_015_sel_id_survives_patch_sort_filter_and_replay_repair() -> None:
    """CON-015: the four events move the caret with its row and never change ``sel_id``."""
    seam = _seam_holding(_projection())
    session = Session()
    session.route, session.sel, session.sel_id = ROUTE, 1, "TRK-0003"
    assert _selected_on(build_spine_view(seam.projection), session) == (1, "TRK-0003")

    asyncio.run(seam.apply_patch(_patch("TRK-0001", sequence=41209)))
    assert _selected_on(build_spine_view(seam.projection), session) == (2, "TRK-0003")

    spine = build_spine_view(seam.projection)
    resorted = dataclasses.replace(spine, rows=tuple(reversed(spine.rows)))
    assert _selected_on(resorted, session) == (1, "TRK-0003")

    session.typing = True
    _press(session, "T", projection=spine)
    assert session.sel_id == "TRK-0003"
    session.typing = False
    assert _selected_on(spine, session) == (2, "TRK-0003")

    extra = {**TRACKS, "TRK-0000": {**TRACKS["TRK-0002"], "urn": "urn:eawf:EAWF:track:TRK-0000"}}
    seam._projection = _projection(41300, tracks=extra)
    assert _selected_on(build_spine_view(seam.projection), session) == (2, "TRK-0003")


def test_con_015_a_back_step_carries_the_selected_id() -> None:
    """CON-015: the back stack stores the id, and Escape restores it."""
    session = _session("activity")
    session.sel_id = "RUN-f1bbcd9c"
    _press(session, "Enter")
    assert session.back.items()[-1].sel_id == "RUN-f1bbcd9c"
    _press(session, "Escape")
    assert session.route == "activity"
    assert session.sel_id == "RUN-f1bbcd9c"


def test_con_016_escape_is_history_first_and_u_is_containment() -> None:
    """CON-016: from a Run reached through Activity, Escape returns and ``u`` climbs."""
    session = _session("activity")
    _press(session, "Enter")
    run = session.subj_id
    assert (session.route, run) == ("run.detail", "RUN-538453eb")
    _press(session, "Escape")
    assert session.route == "activity"
    _press(session, "Enter", "u")
    assert (session.route, session.subj_id) == ("task.detail", "EAWF-0042")
    _press(session, "Escape")
    assert (session.route, session.subj_id) == ("run.detail", run)


def test_con_016_escape_with_an_empty_stack_climbs_the_containment_chain() -> None:
    """CON-016: a Task climbs to its Batch, and the Batch to its Milestone."""
    session = _session("task.detail", "EAWF-0054")
    _press(session, "Escape")
    assert (session.route, session.subj_id) == ("batch.detail", "BAT-0001")
    _press(session, "Escape")
    assert (session.route, session.subj_id) == ("milestone", "MLS-0001")


def test_con_016_brackets_walk_the_siblings_at_the_current_depth() -> None:
    """CON-016: ``]`` and ``[`` step through the parent's children and wrap, pushing nothing."""
    session = _session("batch.detail", "BAT-0001")
    _press(session, "]")
    assert (session.route, session.subj_id) == ("batch.detail", "BAT-0002")
    _press(session, "]")
    assert session.subj_id == "BAT-0001"
    _press(session, "[")
    assert session.subj_id == "BAT-0002"
    assert len(session.back) == 0


def test_con_016_brackets_on_a_subject_with_no_siblings_say_so() -> None:
    """CON-016: a route with no containment field has no sibling walk; nothing moves."""
    session = _session("activity")
    _press(session, "]")
    assert session.route == "activity"
    assert session.trace == "] → no sibling at this depth"


def test_con_016_u_at_the_root_climbs_nowhere() -> None:
    """CON-016: scope home has no parent; ``u`` says so and the quit stays unarmed."""
    session = _session("scope.home")
    _press(session, "u")
    assert session.route == "scope.home"
    assert session.last_esc == pytest.approx(0.0)
    assert session.trace == "u → at the top of the containment chain · nothing above"


def test_con_016_depth_keys_do_nothing_while_an_overlay_owns_the_keys() -> None:
    """CON-016: an overlay's key table does not take ``u`` or the brackets."""
    session = _session("task.detail", "EAWF-0054")
    session.overlay = "help"
    _press(session, "u", "]")
    assert (session.route, session.subj_id, session.overlay) == (
        "task.detail",
        "EAWF-0054",
        "help",
    )


#: The depth keys: Enter drills, Escape returns, ``u`` climbs the containment chain, and
#: ``[`` and ``]`` walk the siblings at the current depth. Breadth is the ``g`` prefix.
DEPTH_KEYS: tuple[str, ...] = ("Enter", "Escape", "u", "[", "]")


@pytest.mark.parametrize("route", ["track", "milestone", "batch.detail"])
def test_con_016_the_depth_keys_are_bound_on_every_detail_route(route: str) -> None:
    """CON-016: Enter, Escape, ``u`` and the bracket pair move through depth."""
    session = Session()
    session.route = route
    assert set(DEPTH_KEYS) <= allowlist(session, _fixture())


def test_con_017_no_climb_or_sibling_step_lands_on_a_global_route() -> None:
    """CON-017: Escape and ``u`` climb to the root or to the entity a sub-surface is about.

    Every ``g`` destination is top level: its own parent is the root, and a route that
    climbs onto one is a surface about it, never another ``g`` destination.
    """
    globals_ = set(REGISTRY.go_map.values()) - {ROOT_ROUTE}
    for route in globals_:
        assert REGISTRY.escapes[route].route == ROOT_ROUTE
    for route, escape in REGISTRY.escapes.items():
        if escape.route in globals_:
            assert REGISTRY.by_id[route].go_letter is None, route
    for route in ("batch.detail", "task.detail", "run.detail", "milestone"):
        assert REGISTRY.escapes[route].route not in globals_


def test_con_017_a_go_destination_is_top_level() -> None:
    """CON-017: ``g`` clears the history, so the destination is never a child."""
    session = _session("activity")
    _press(session, "Enter")
    assert len(session.back) == 1
    _press(session, "g", "n")
    assert (session.route, len(session.back)) == ("attention", 0)


@pytest.mark.parametrize("size", [0, 2])
def test_con_018_the_drilled_detail_is_its_own_route_at_every_width(size: int) -> None:
    """CON-018: Enter opens the same route onto the same subject at 80 and 160 columns."""
    session = _session("activity")
    session.size = size
    _press(session, "Enter")
    assert (session.route, session.subj_id) == ("run.detail", "RUN-538453eb")


@pytest.mark.parametrize("pushes", [31, 32, 33])
def test_con_019_the_back_stack_caps_at_32_dropping_the_oldest(pushes: int) -> None:
    """CON-019: the stack holds 32 steps at most and forgets the oldest first."""
    stack = Session().back
    for n in range(pushes):
        stack.push(route="task.detail", sel=0, subj=f"EAWF-{n:04d}")
    assert len(stack) == min(pushes, BACK_CAP)
    assert stack.items()[-1].subj == f"EAWF-{pushes - 1:04d}"
    assert stack.items()[0].subj == f"EAWF-{max(0, pushes - BACK_CAP):04d}"


def test_con_019_consecutive_steps_onto_one_place_coalesce() -> None:
    """CON-019: a second step onto the same route and subject replaces the first."""
    stack = Session().back
    stack.record(BackEntry(route="activity", sel=1, subj=None))
    stack.record(BackEntry(route="activity", sel=4, subj=None, sel_id="RUN-1"))
    stack.record(BackEntry(route="activity", sel=2, subj="RUN-1"))
    assert [(e.sel, e.subj) for e in stack.items()] == [(4, None), (2, "RUN-1")]
    assert stack.items()[0].sel_id == "RUN-1"


def test_con_019_a_pop_restores_every_field_its_push_recorded() -> None:
    """CON-019: bucket, filter, scroll, anchor, region and id all come back on Escape."""
    session = _session("activity")
    session.bucket, session.evt = "perm", "EVT-0001"
    session.filters["activity"] = "RUN-"
    session.sel, session.sel_id = 1, "RUN-f1bbcd9c"
    _draw(session)
    pushed = remember(session)
    _press(session, "Enter")
    assert session.back.items()[-1] == pushed
    _press(session, "Escape")
    assert remember(session) == pushed


def test_con_019_recall_puts_back_every_cursor_a_step_carries() -> None:
    """CON-019: the restore is field for field, the ones a renderer might clamp included."""
    entry = BackEntry(
        route="task.detail",
        sel=3,
        subj="EAWF-0054",
        sel_id="RUN-1",
        bucket="perm",
        filter="seal",
        scroll=5,
        evt="EVT-0001",
        region="runs",
    )
    session = Session()
    recall(session, entry)
    assert remember(session) == entry


def test_con_019_a_jump_clears_the_stack_and_self_navigation_pushes_nothing() -> None:
    """CON-019: a palette pick is a jump, and opening the current place is not a step."""
    session = _session("activity")
    _press(session, "Enter")
    assert len(session.back) == 1
    _press(session, "/", "h", "o", "m", "e", "Enter")
    assert (session.route, len(session.back)) == ("scope.home", 0)
    ctx = Ctx(session=session, fixture=_fixture(), host=_Host(), w=120, h=30)
    assert go(ctx, "scope.home", "test") is False
    assert len(session.back) == 0


def test_con_019_the_breadcrumb_renders_the_back_stack() -> None:
    """CON-019: while history exists the crumb names the step it came from."""
    session = _session("activity")
    _press(session, "Enter")
    crumb = _draw(session)[0]
    assert "RUN-538453eb" in crumb
    assert crumb.index(REGISTRY.step_leaf("activity", None)) < crumb.index("RUN-538453eb")


def test_con_021_a_back_step_restored_at_a_newer_revision_keeps_its_id() -> None:
    """CON-021: a pop onto a frame whose revision advanced keeps the selected identifier."""
    session = Session()
    session.route, session.sel, session.sel_id = ROUTE, 1, "TRK-0003"
    session.back.record(remember(session))
    session.route, session.subj_id, session.sel, session.sel_id = "track", "TRK-0003", 0, None
    _press(session, "Escape", projection=build_spine_view(_projection()))
    assert (session.route, session.sel_id) == (ROUTE, "TRK-0003")

    seam = _seam_holding(_projection())
    asyncio.run(seam.apply_patch(_patch("TRK-0001", sequence=41209)))
    assert _selected_on(build_spine_view(seam.projection), session) == (2, "TRK-0003")


def test_con_022_tab_walks_activity_buckets_in_register_order_and_arrows_stay_on_rows() -> None:
    """CON-022: Tab cycles every bucket then all; an arrow never changes the bucket."""
    fixture = _fixture()
    session = _session("activity")
    seen: list[str | None] = []
    for _ in range(len(fixture.proto.buckets) + 1):
        _press(session, "Tab")
        seen.append(session.bucket)
        _press(session, "ArrowDown")
        assert session.bucket == seen[-1]
    assert seen == [*(b.key for b in fixture.proto.buckets), None]
    assert focused_region(session) is None


def test_con_022_tab_walks_attention_buckets_and_sub_buckets_in_register_order() -> None:
    """CON-022: the attention register's order, sub-buckets under their bucket."""
    fixture = _fixture()
    order: list[str | None] = []
    for bucket in fixture.proto.xbuckets:
        order.append(bucket.key)
        order.extend(sub.key for sub in bucket.sub or ())
    session = _session("attention")
    seen: list[str | None] = []
    for _ in range(len(order) + 1):
        _press(session, "Tab")
        seen.append(session.bucket)
    assert seen == [*order, None]
    assert order[:6] == [
        "failed",
        "lost",
        "needs",
        "needs.permission",
        "needs.answer",
        "needs.readiness",
    ]


@pytest.mark.parametrize("route", ["activity", "attention"])
def test_con_022_the_first_escape_clears_the_bucket_and_the_second_leaves(route: str) -> None:
    """CON-022: Escape clears a chosen bucket before it goes back."""
    session = _session("scope.home")
    ctx = Ctx(session=session, fixture=_fixture(), host=_Host(), w=120, h=30)
    go(ctx, route, "test")
    _press(session, "Tab")
    assert session.bucket is not None
    _press(session, "Escape")
    assert (session.route, session.bucket) == (route, None)
    _press(session, "Escape")
    assert session.route == "scope.home"


def test_con_026_pane_geometry_answers_to_the_terminal_shape_alone() -> None:
    """CON-026: the console binds no pointer handler, so no drag can resize a pane."""
    own = vars(ConsoleApp)
    assert not [name for name in own if "mouse" in name or "click" in name or "drag" in name]
    session = _session("activity")
    assert _draw(session) == _draw(session)


def test_con_027_focus_returns_to_the_invoking_row_after_the_palette_closes() -> None:
    """CON-027: the palette borrows the row cursor, and Escape gives the row back."""
    session = _session("activity")
    _press(session, "ArrowDown", "ArrowDown")
    assert session.sel == 2
    _press(session, "/", "ArrowDown", "ArrowDown", "ArrowDown")
    assert session.overlay == "palette"
    _press(session, "Escape")
    assert (session.overlay, session.sel, session.focus_return) == (None, 2, None)


def test_con_027_focus_returns_after_a_dismissal_and_after_a_confirmed_verb() -> None:
    """CON-027: a consequence card closed either way leaves focus on the row it came from."""
    session = _session("attention")
    _press(session, "ArrowDown")
    at = session.sel
    _press(session, "a")
    assert session.overlay == "consequence"
    _press(session, "Escape")
    assert (session.overlay, session.sel) == (None, at)
    _press(session, "a", "Enter")
    assert (session.overlay, session.sel) == (None, at)


def test_con_027_focus_returns_after_a_refused_verb() -> None:
    """CON-027: a refusal leaves focus where it was, overlay or not."""
    session = _session("activity")
    _press(session, "ArrowDown")
    _press(session, "p")
    assert (session.route, session.sel, session.overlay) == ("activity", 1, None)


def test_con_027_a_jump_from_an_overlay_arrives_fresh_rather_than_returning() -> None:
    """CON-027: focus return is for closing on the same place; a palette pick is a jump."""
    session = _session("activity")
    _press(session, "ArrowDown", "/", "h", "o", "m", "e", "Enter")
    assert (session.route, session.sel, session.focus_return) == ("scope.home", 0, None)


@pytest.mark.parametrize(
    "route", [route for route, regions in REGISTRY.focus_regions.items() if len(regions) > 1]
)
def test_con_028_every_tab_stop_is_a_region_the_registry_declares(route: str) -> None:
    """CON-028: a region cycle stops only on the route's declared regions, each once."""
    session = Session()
    session.route = route
    stops: list[str] = []
    for _ in range(len(regions_of(route))):
        region = cycle_region(session)
        assert region is not None
        stops.append(region)
    assert sorted(stops) == sorted(regions_of(route))


def test_con_028_the_native_home_frame_keeps_the_focus_with_nothing_waiting() -> None:
    """CON-028: with nothing in the attention list, Tab on home has no list to focus."""
    spine = build_spine_view(_projection())
    session = Session()
    session.route = "scope.home"
    _press(session, "Tab", projection=spine)
    assert focused_region(session) == "outcomes"
    assert session.log[0].note == "nothing is waiting — no list to focus"


def test_con_028_a_one_region_route_has_no_tab_stop() -> None:
    """CON-028: a route with one region gives Tab nowhere to go, and says nothing moved."""
    session = _session("batch.detail", "BAT-0001")
    _press(session, "Tab")
    assert session.region is None
    assert cycle_region(session) is None


def test_con_028_a_rail_declared_as_a_focus_region_refuses_to_build() -> None:
    """CON-028: a bucket rail is a display region; declaring it a focus region is refused."""
    rows = tuple(
        dataclasses.replace(spec, focus_regions=("buckets",)) if spec.id == "activity" else spec
        for spec in ROUTES
    )
    with pytest.raises(ValueError, match="rail a focus region: activity"):
        RouteRegistry(rows)


def test_con_028_a_region_from_another_route_reads_as_the_first() -> None:
    """CON-028: a stale region name never becomes a Tab stop the registry did not declare."""
    session = Session()
    session.route, session.region = "task.detail", "outcomes"
    assert focused_region(session) == "criteria"


# ---------- CON-149: the three global keys no golden exercises, as journeys ----------


def test_con_149_u_from_a_run_reached_through_activity_lands_where_escape_would_climb() -> None:
    """``u`` lands on the Task an empty-stack Escape would climb to; Escape still goes back."""
    session = _session("activity")
    _press(session, "Enter")
    run = session.subj_id
    climbed = _session("run.detail", run)
    _press(climbed, "Escape")
    _press(session, "u")
    assert (session.route, session.subj_id) == (climbed.route, climbed.subj_id)
    assert session.route == "task.detail"
    reached = _session("activity")
    _press(reached, "Enter", "Escape")
    assert reached.route == "activity"


def test_con_149_brackets_on_a_task_walk_its_batch_and_keep_the_crumb_depth() -> None:
    session = _session("task.detail", "EAWF-0042")
    depth = _draw(session)[0].count("▸")
    peers = siblings_of(session, _fixture())
    assert len(peers) > 1 and "EAWF-0042" in peers
    _press(session, "]")
    after = peers[(peers.index("EAWF-0042") + 1) % len(peers)]
    assert (session.route, session.subj_id) == ("task.detail", after)
    assert _draw(session)[0].count("▸") == depth
    _press(session, "[")
    assert session.subj_id == "EAWF-0042"
    assert len(session.back) == 0


def test_con_149_bang_lands_on_the_top_open_action_from_any_route() -> None:
    """``!`` opens the Attention route on the top-ranked open action, never on a notice."""
    session = _session("run.detail", "RUN-538453eb")
    _press(session, "!")
    fixture = _fixture()
    top = open_actions(fixture)[0]
    assert not is_notice(top)
    assert (session.route, session.sel_id) == ("attention", top.id)


def test_con_149_bang_on_a_held_register_selects_this_principals_top_action() -> None:
    session = _session("activity")
    ctx = Ctx(
        session=session,
        fixture=_fixture(),
        host=_Host(),
        w=120,
        h=30,
        attention=bodies._projection("attention"),
        principal=bodies.ME,
    )
    dispatch(ctx, "!", False)
    assert (session.route, session.sel_id) == ("attention", "ACT-0001")


def test_con_149_bang_with_nothing_open_says_so_and_moves_nowhere() -> None:
    session = _session("activity")
    only_sealed = {
        **bodies.DOCUMENT,
        "pending_action": {"ACT-0003": bodies.DOCUMENT["pending_action"]["ACT-0003"]},
    }
    ctx = Ctx(
        session=session,
        fixture=_fixture(),
        host=_Host(),
        w=120,
        h=30,
        attention=bodies._projection("attention", only_sealed),
        principal=bodies.ME,
    )
    dispatch(ctx, "!", False)
    assert session.route == "activity"
    assert session.trace == "! → nothing needs you"


# ---------- CON-151: a docked readout reports on the cursor's row by its id ----------


def _readout_frame(route: str, **kwargs: Any) -> list[str]:
    return frames._frame(route, **kwargs)


def _readout(frame: list[str], label: str) -> list[str]:
    """Return the readout rows: the labelled head and its continuation, above the keybar."""
    at = next(i for i, row in enumerate(frame) if row.startswith(f" {label}"))
    return frame[at:-1]


@pytest.mark.parametrize(
    ("route", "label", "kwargs"),
    [
        ("trust", "FIELD", {"subject": "MLS-0101"}),
        ("health", "REPAIR", {}),
    ],
)
def test_con_151_the_readout_names_its_subject_and_docks_to_the_foot(
    route: str, label: str, kwargs: dict[str, Any]
) -> None:
    frame = _readout_frame(route, w=80, **kwargs)
    rows = _readout(frame, label)
    assert rows, route
    # docked: nothing but the keybar sits under it
    assert len(rows) <= 3 and frame.index(rows[0]) + len(rows) == len(frame) - 1
    # the head names the focused row by its stable identifier, then what it is
    cursor = next(row for row in frame if row.lstrip().startswith("▸"))
    key = cursor.lstrip("▸ ").split()[0]
    assert key in rows[0]
    assert " · " in rows[0]


@pytest.mark.parametrize(
    ("route", "label", "kwargs"),
    [
        ("trust", "FIELD", {"subject": "MLS-0101"}),
        ("health", "REPAIR", {}),
    ],
)
def test_con_151_the_readout_draws_no_caret_and_is_no_focus_region(
    route: str, label: str, kwargs: dict[str, Any]
) -> None:
    rows = _readout(_readout_frame(route, **kwargs), label)
    assert not any("▸" in row for row in rows)
    assert all(label.lower() not in region for region in regions_of(route))


def test_con_151_the_readout_follows_the_cursor_by_id_not_by_offset() -> None:
    document = {
        **frames.DOCUMENT,
        "health_view": {
            "hv-0001": frames._row("health_view", "hv-0001", "OK"),
            "hv-0002": frames._row("health_view", "hv-0002", "FAILED"),
        },
    }
    session = Session()
    session.route = "health"
    session.sel_id = "hv-0002"
    model = frames._model("health", document)
    view = View(session=session, fixture=_fixture(), w=120, h=30, projection=model)
    first = render_route(view)
    assert "hv-0002" in _readout(first, "REPAIR")[0]
    # a new check sorting above the cursor moves the row; the readout stays on hv-0002
    document["health_view"]["hv-0000"] = frames._row("health_view", "hv-0000", "OK")
    moved = View(
        session=session,
        fixture=_fixture(),
        w=120,
        h=30,
        projection=frames._model("health", document),
    )
    again = render_route(moved)
    assert "hv-0002" in _readout(again, "REPAIR")[0]
    assert session.sel_id == "hv-0002"


# ---------- CON-152: every cursor is text, and what Enter opens is what is marked ----------


def _lane_row(frame: list[str]) -> str:
    return next(row for row in frame if row.startswith("▸") and "[" in row)


def test_con_152_the_roadmap_marker_cursor_is_drawn_in_text() -> None:
    session = _session("timeline")
    frame = _draw(session)
    assert re.search(r"\[[●○]\]", _lane_row(frame))
    marker = next(row for row in frame if row.startswith(" MARKER"))
    assert re.match(r"^ MARKER    MLS-\d{4} · Runtime · 1 of \d+", marker)


def test_con_152_moving_the_marker_changes_the_text_not_only_the_colour() -> None:
    session = _session("timeline")
    frames = [_draw(session)]
    for _ in range(2):
        _press(session, "ArrowRight")
        frames.append(_draw(session))
    assert len({"\n".join(frame) for frame in frames}) == 3
    positions = [_lane_row(frame).index("[") for frame in frames]
    assert positions == sorted(positions) and len(set(positions)) == 3
    assert "3 of" in next(row for row in frames[-1] if row.startswith(" MARKER"))


def test_con_152_the_marker_is_part_of_the_projection_journeys_assert() -> None:
    session = _session("timeline")
    _press(session, "ArrowRight")
    assert session.projection()["mark"] == 1


def test_con_152_enter_opens_the_milestone_the_frame_marks() -> None:
    session = _session("timeline")
    _press(session, "ArrowRight", "ArrowRight")
    marked = next(row for row in _draw(session) if row.startswith(" MARKER")).split()[1]
    _press(session, "Enter")
    assert session.overlay == "marker"
    assert marked in _draw(session)[0]


@pytest.mark.parametrize("route", ["activity", "attention", "scope.home", "run.detail"])
def test_con_152_the_row_cursor_is_a_text_caret(route: str) -> None:
    frame = render_route(bodies._view(route, subject="RUN-00000002"))
    carets = [row for row in frame if row.lstrip().startswith("▸")]
    assert len(carets) <= 1
    if route != "run.detail":
        assert carets


# ---------- CON-020: a target that no longer resolves opens its resolution card ----------


def test_con_020_a_purged_target_opens_the_card_naming_its_ending_not_a_dead_route() -> None:
    session = _session("history")
    session.sel = 3
    _press(session, "Enter")
    assert session.route == "history"
    assert session.overlay == "resolution"
    assert session.resolution_ending == Ending.PURGED.value
    rows = _draw(session)
    assert rows[0].startswith(f" Eä ▸ resolution · {session.ov_subject}")
    assert f" ENDING    {ENDINGS[Ending.PURGED].ending}" in "\n".join(rows)


def test_con_020_escape_from_the_card_returns_to_the_row_it_was_opened_from() -> None:
    session = _session("history")
    session.sel = 3
    _press(session, "Enter", "Escape")
    assert (session.route, session.overlay, session.sel) == ("history", None, 3)


# ---------- CON-025: three pointer bindings, each mirroring a key; the rest are no-ops ----------

_POINTER_HANDLERS = re.compile(r"^_?on_(?:mouse|click|scroll|drag)")


def _pointer_handlers(cls: type) -> set[str]:
    return {name for name in vars(cls) if _POINTER_HANDLERS.match(name)}


def test_con_025_the_breadcrumb_click_is_the_only_pointer_handler_the_console_binds() -> None:
    assert _pointer_handlers(ConsoleApp) == set()
    assert _pointer_handlers(ProjectionHeader) == {"on_click"}
    for widget in (Body, KeybarRow):
        assert _pointer_handlers(widget) == set(), widget


def _driven(*gestures: tuple[str, tuple[int, int], int]) -> tuple[Any, Any, list[str], list[str]]:
    async def body() -> tuple[Any, Any, list[str], list[str]]:
        app = ConsoleApp(_fixture(), FakeClock())
        async with app.run_test(size=(120, 30)) as pilot:
            app.reset(SessionSetup(route="activity"))
            await pilot.pause()
            before, frame = app.session.projection(), list(app.frame_rows)
            for gesture, offset, button in gestures:
                if gesture == "double":
                    await pilot.double_click("#body", offset=offset)
                else:
                    await pilot.click("#body", offset=offset, button=button)
                await pilot.pause()
            return before, app.session.projection(), frame, list(app.frame_rows)

    return asyncio.run(body())


def test_con_025_a_right_click_and_a_double_click_are_defined_no_ops() -> None:
    before, after, frame, drawn = _driven(("click", (6, 4), 3), ("double", (6, 5), 1))
    assert after == before
    assert drawn == frame


def test_con_025_the_breadcrumb_click_walks_to_the_step_as_escape_would() -> None:
    async def body() -> tuple[str, str]:
        app = ConsoleApp(_fixture(), FakeClock())
        async with app.run_test(size=(120, 30)) as pilot:
            app.reset(SessionSetup(route="activity"))
            app.press_key("Enter")
            await pilot.pause()
            opened = app.session.route
            row = app.frame_rows[0]
            await pilot.click("#header", offset=(row.index("Activity") + 2, 0))
            await pilot.pause()
            return opened, app.session.route

    assert asyncio.run(body()) == ("run.detail", "activity")


@pytest.mark.xfail(
    strict=True,
    reason="no pointer adapter maps a body click to a row or a bucket: only the breadcrumb "
    "click is wired, so a row click selects nothing and a bucket click applies nothing",
)
def test_con_025_a_click_on_a_row_selects_it() -> None:
    before, after, _frame, _drawn = _driven(("click", (6, 5), 1))
    assert after["sel"] != before["sel"]
