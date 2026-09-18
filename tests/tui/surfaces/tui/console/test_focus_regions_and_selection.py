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
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.keybar import KEY_NAMES
from eawf.surfaces.tui.console.keymap import route_keys
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.registry import (
    FOCUS_REGION_LIMIT,
    REGISTRY,
    ROUTES,
    RouteRegistry,
)
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.spine import restore
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import Session

AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
SCOPE = "EAWF"
ROUTE = "scope.home"

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


@pytest.mark.parametrize("route", SPINE_ROUTES)
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
    other = _patch("TRK-0001", sequence=41209).model_copy(update={"routes": ("track",)})
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
