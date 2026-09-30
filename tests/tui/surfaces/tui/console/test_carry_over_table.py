"""The epoch-1 surfaces the design pack carries over each have a console place that renders them.

The design pack judged every epoch-1 TUI surface once: 47 carry to an epoch-2 route, 27
are chassis and 8 are deleted. Retiring the epoch-1 app may lose none of the 47, so the
carry-over table records, per carried surface, the console places that answer its
operator question: a registered route, optionally opened onto a subject, a section, an
overlay or drawer, or one of the entry layer's pre-session states, with the words that
place draws where the surface's function shows.

Two things are checked. The walk resolves every place against the route registry and the
overlay, drawer and entry-state tables, so a route renamed or removed out from under a
carried surface reds here rather than silently dropping the surface. The render draws
every place from the design pack's own fixture at the widest size and finds the words the
row names, so a place that is registered but no longer shows the function reds too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.surfaces.tui.console.app import compose_frame
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.registry import (
    DRAWERS,
    ENTRY_STATE_IDS,
    OVERLAYS,
    REGISTRY,
    RouteRegistry,
)
from eawf.surfaces.tui.console.session import SIZES, Session, SessionSetup

FIXTURES = Path(__file__).resolve().parents[4] / "fixtures" / "console"
TABLE_PATH = FIXTURES / "carry-over.json"
PACK_FIXTURE = FIXTURES / "golden" / "fixture"

#: The pack's verdict census: the carries are the surfaces this table must hold.
CARRIED = 47

#: The size every place is drawn at: the widest, where no pane folds away.
WIDEST = len(SIZES) - 1

ENTRY_ROUTE = "entry"


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Place(_Closed):
    """One console place a carried surface's function is drawn in.

    Attributes:
        route: The registered route id.
        subject: The entity the route opens onto; ``None`` opens it without one.
        section: The Tab section the route opens at.
        overlay: The overlay or drawer drawn over the route.
        entry_state: The entry layer's pre-session state; only on the entry route.
        conn: The connection state the frame is drawn under.
        anchors: The words the place draws where the function shows.
    """

    route: str
    subject: str | None = None
    section: int = Field(default=0, ge=0)
    overlay: str | None = None
    entry_state: str | None = None
    conn: str | None = None
    anchors: tuple[str, ...] = Field(min_length=1)


class CarriedSurface(_Closed):
    """One epoch-1 surface the pack carries, and where the console draws its function.

    Attributes:
        surface: The epoch-1 surface as the pack's verdict names it.
        function: The operator question the surface answers.
        target: The epoch-2 route the pack's verdict names.
        places: The console places that render the function, at least one.
        note: Where the console's place differs from the verdict's target, and why.
    """

    surface: str
    function: str
    target: str
    places: tuple[Place, ...] = Field(min_length=1)
    note: str = ""


class CarryOverTable(_Closed):
    """The carry-over table: every carried surface, each once."""

    surfaces: tuple[CarriedSurface, ...]


def load_table(path: Path) -> CarryOverTable:
    """Return the table at ``path``, validated.

    Raises:
        OSError: the file cannot be read.
        ValidationError: the document is not a carry-over table.
    """
    return CarryOverTable.model_validate(json.loads(path.read_text(encoding="utf-8")))


def unrouted(table: CarryOverTable, registry: RouteRegistry = REGISTRY) -> list[str]:
    """Return one line per place the console cannot open, in table order.

    A place is openable when its route is registered, its overlay is a registered overlay
    or drawer, and its entry state is one the entry route draws, on that route only.
    """
    missing: list[str] = []
    for row in table.surfaces:
        for place in row.places:
            if place.route not in registry.ids:
                missing.append(f"{row.surface}: route {place.route!r} is not registered")
            if place.overlay is not None and place.overlay not in (*OVERLAYS, *DRAWERS):
                missing.append(f"{row.surface}: overlay {place.overlay!r} is not registered")
            if place.entry_state is not None and (
                place.route != ENTRY_ROUTE or place.entry_state not in ENTRY_STATE_IDS
            ):
                missing.append(f"{row.surface}: entry state {place.entry_state!r} is not drawn")
    return missing


def render_place(place: Place, fixture: Fixture) -> str:
    """Return the frame ``place`` draws from ``fixture`` at the widest size, as one text."""
    setup = SessionSetup(
        route=place.route,
        subj_id=place.subject,
        overlay=place.overlay,
        section=place.section,
        conn=place.conn,
        entry_sel=ENTRY_STATE_IDS.index(place.entry_state) if place.entry_state else 0,
        size=WIDEST,
    )
    session = Session()
    session.reset(setup, settings_section_order=fixture.settings.section_order, now=0.0)
    w, h = SIZES[session.size]
    return "\n".join(compose_frame(View(session=session, fixture=fixture, w=w, h=h, held=True)))


TABLE = load_table(TABLE_PATH)
PACK = load_fixture(PACK_FIXTURE)


def test_cr_022_table_holds_every_carried_surface_once() -> None:
    surfaces = [row.surface for row in TABLE.surfaces]
    assert len(surfaces) == CARRIED
    assert len(set(surfaces)) == CARRIED


@pytest.mark.parametrize("row", TABLE.surfaces, ids=lambda row: row.surface)
def test_cr_022_every_place_renders_the_function(row: CarriedSurface) -> None:
    for place in row.places:
        frame = render_place(place, PACK)
        absent = [anchor for anchor in place.anchors if anchor not in frame]
        assert not absent, f"{row.surface} on {place.route}: {absent} not drawn\n{frame}"


def test_cr_023_walk_finds_every_carried_surface_routed() -> None:
    assert unrouted(TABLE) == []


def _one_row(**place: object) -> CarryOverTable:
    return CarryOverTable.model_validate(
        {
            "surfaces": [
                {
                    "surface": "widgets/probe.py",
                    "function": "What is probed?",
                    "target": "Probe",
                    "places": [{"anchors": ["PROBE"], **place}],
                }
            ]
        }
    )


@pytest.mark.parametrize(
    ("place", "said"),
    [
        ({"route": "wave.detail"}, "route 'wave.detail' is not registered"),
        ({"route": "run.detail", "overlay": "tour"}, "overlay 'tour' is not registered"),
        ({"route": "entry", "entry_state": "wizard"}, "entry state 'wizard' is not drawn"),
        ({"route": "scope.home", "entry_state": "offline"}, "entry state 'offline' is not drawn"),
    ],
)
def test_cr_023_walk_fails_a_surface_with_no_registered_place(
    place: dict[str, object], said: str
) -> None:
    assert unrouted(_one_row(**place)) == [f"widgets/probe.py: {said}"]


def test_cr_023_walk_passes_a_registered_drawer_and_entry_state() -> None:
    assert unrouted(_one_row(route="run.detail", overlay="raw")) == []
    assert unrouted(_one_row(route="entry", entry_state="offline")) == []


def test_cr_023_walk_of_an_empty_table_finds_nothing() -> None:
    assert unrouted(CarryOverTable(surfaces=())) == []


@pytest.mark.parametrize(
    "document",
    [
        {"surfaces": [{"surface": "x", "function": "q", "target": "t", "places": []}]},
        {
            "surfaces": [
                {"surface": "x", "function": "q", "target": "t", "places": [{"route": "h"}]}
            ]
        },
        {
            "surfaces": [
                {
                    "surface": "x",
                    "function": "q",
                    "target": "t",
                    "places": [{"route": "h", "anchors": ["A"], "size": 2}],
                }
            ]
        },
        {"surfaces": "all of them"},
        {},
    ],
    ids=["no-place", "no-anchor", "unknown-key", "wrong-type", "missing-key"],
)
def test_cr_023_table_refuses_a_malformed_document(document: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CarryOverTable.model_validate(document)


def test_cr_023_place_refuses_a_negative_section() -> None:
    with pytest.raises(ValidationError):
        Place(route="campaign", section=-1, anchors=("PLAN",))


def test_cr_023_load_table_refuses_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        load_table(tmp_path / "carry-over.json")
