"""The planning and diagnostics routes draw daemon-served rows, and say what is unstated.

These eight routes used to draw registers the console carried itself: a timeline of three
hand-written lanes, a backlog of two fixed groups, a search page whose hits were a tuple in
the module. A frame like that is true of a file rather than of the workspace, and there is
no cursor it stands at, so two surfaces could not be compared.

They now draw a :class:`~eawf.kernel.projection.spine.SpineView` built from the projection
``projection.<route>.read`` answers, which is the same view the five spine routes draw. The
suite pins three things about that.

First, the binding: every planning and diagnostics route names the collections it renders,
is served by both projection verbs, and draws the rows the daemon projected -- including a
register whose producer is a dev4 item, which counts zero honestly rather than being left
out. Second, the unstated columns: a Campaign, Decision or plan-lens field has no epoch-2
producer, so it comes back as an unknown truth field naming why and the frame prints the
truth token for it. Third, the epoch-1 surfaces are untouched: the research_board mode
still resolves every key its footer advertises.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.projection.compute import (
    DIAGNOSTICS_CORPUS,
    ROUTE_COLLECTIONS,
    RouteProjection,
    build_route_projection,
)
from eawf.kernel.projection.connection import READ_METHOD_TEMPLATE, RECONNECT_METHOD_TEMPLATE
from eawf.kernel.projection.spine import (
    DIAGNOSTICS_ROUTES,
    NATIVE_ROUTES,
    PLANNING_ROUTES,
    STATUS_FIELD,
    UNPRODUCED_REASON,
    SpineView,
    build_spine_view,
)
from eawf.kernel.projection.truth import TruthKind, TruthState
from eawf.runtime.daemon.methods.projection import ROUTE_READ_METHODS, ROUTE_RECONNECT_METHODS
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.fixture import load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import Session

#: When the probe projections are stamped. The digest does not cover the stamp; a fixed
#: clock only keeps this suite's output reproducible.
AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

#: The scope every probe projection is built for.
SCOPE = "EAWF"

#: The eight routes this suite is about: the planning group and the diagnostics group.
GALLERY_ROUTES: tuple[str, ...] = (*PLANNING_ROUTES, *DIAGNOSTICS_ROUTES)

#: One row per collection the gallery touches, so no route's register is empty by accident
#: and a route reading across the corpus can be told apart from one reading a single
#: register. The campaign and artifact rows stand in for a producer that ships at dev4.
DOCUMENT: dict[str, Any] = {
    "track": {
        "TRK-0001": {"urn": f"urn:eawf:{SCOPE}:track:TRK-0001", "revision": 1, "status": "ACTIVE"},
    },
    "campaign": {
        "CAM-0001": {
            "urn": f"urn:eawf:{SCOPE}:campaign:CAM-0001",
            "revision": 2,
            "status": "RUNNING",
        },
    },
    "milestone": {
        "MLS-0030": {
            "urn": f"urn:eawf:{SCOPE}:milestone:MLS-0030",
            "revision": 2,
            "status": "PLANNED",
        },
    },
    "batch": {
        "BAT-0001": {"urn": f"urn:eawf:{SCOPE}:batch:BAT-0001", "revision": 1, "status": "OPEN"},
        "BAT-0002": {"urn": f"urn:eawf:{SCOPE}:batch:BAT-0002", "revision": 4, "status": "MERGED"},
    },
    "task": {
        "EAWF-0001": {"urn": f"urn:eawf:{SCOPE}:task:EAWF-0001", "revision": 4, "status": "READY"},
    },
    "run": {
        "RUN-9e3779b1": {
            "urn": f"urn:eawf:{SCOPE}:run:RUN-9e3779b1",
            "revision": 7,
            "status": "RUNNING",
        },
    },
    "artifact": {
        "ART-0001": {"urn": f"urn:eawf:{SCOPE}:artifact:ART-0001", "revision": 1, "status": "KEPT"},
    },
}


def _projection(route: str, *, cursor: int = 41208, document: Any = None) -> RouteProjection:
    """Return one route's projection over the probe document at ``cursor``."""
    return build_route_projection(
        route=route,
        document=DOCUMENT if document is None else document,
        cursor=cursor,
        scope_id=SCOPE,
        generated_at=AT,
    )


def _view(route: str, **kwargs: Any) -> SpineView:
    """Return the native read model of ``route`` over the probe document."""
    return build_spine_view(_projection(route, **kwargs))


def _frame(spine: SpineView, *, width: int = 120) -> list[str]:
    """Return the console frame ``spine`` renders on its own route."""
    session = Session()
    session.route = REGISTRY.by_key[spine.route].id
    view = View(
        session=session,
        fixture=load_fixture(
            Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"
        ),
        w=width,
        h=24,
        projection=spine,
    )
    return render_route(view)


# ---------- the routes, and the verbs that serve them ----------


def test_the_gallery_routes_are_the_planning_and_diagnostics_groups() -> None:
    """The two groups name the eight routes this wave bound, and nothing else."""
    assert PLANNING_ROUTES == (
        "roadmap",
        "backlog",
        "campaign",
        "campaign.step",
        "campaign.artifact",
    )
    assert DIAGNOSTICS_ROUTES == ("history", "history.diff", "search")
    assert set(GALLERY_ROUTES) <= set(NATIVE_ROUTES)


@pytest.mark.parametrize("route", GALLERY_ROUTES)
def test_every_gallery_route_has_both_projection_verbs(route: str) -> None:
    """A route a console draws natively is one the daemon both reads and reconnects."""
    assert READ_METHOD_TEMPLATE.format(route=route) in ROUTE_READ_METHODS
    assert RECONNECT_METHOD_TEMPLATE.format(route=route) in ROUTE_RECONNECT_METHODS
    assert ROUTE_COLLECTIONS[route]


@pytest.mark.parametrize("route", GALLERY_ROUTES)
def test_every_gallery_route_resolves_to_its_declared_read_model(route: str) -> None:
    """The registry row and the kernel declaration name one read model for the route."""
    spec = REGISTRY.by_key[route]
    assert REGISTRY.read_models[spec.id] is not None


def test_the_renamed_route_reads_under_its_port_key() -> None:
    """The pack calls it ``timeline`` and the port calls it ``roadmap``; the seam uses the key."""
    app = ConsoleApp(
        load_fixture(Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture")
    )
    app.session.route = "timeline"

    assert app.route_key == "roadmap"
    assert "roadmap" in ROUTE_COLLECTIONS
    assert app.route_view() is None


# ---------- the rows the daemon served ----------


@pytest.mark.parametrize("route", GALLERY_ROUTES)
def test_every_gallery_route_draws_the_rows_the_daemon_projected(route: str) -> None:
    """Every row the read model holds reaches the frame, by key and by collection."""
    spine = _view(route)
    body = "\n".join(_frame(spine))

    assert spine.rows
    for row in spine.rows:
        assert row.key in body
        assert row.collection.value in body
    assert "cursor 41,208" in body


@pytest.mark.parametrize("route", GALLERY_ROUTES)
def test_every_count_is_the_rows_of_a_register_the_route_binds(route: str) -> None:
    """A count is taken off the view's own rows, one per bound collection."""
    spine = _view(route)

    assert set(spine.counts) == {c.value for c in ROUTE_COLLECTIONS[route]}
    assert sum(spine.counts.values()) == len(spine.rows)


@pytest.mark.parametrize("route", DIAGNOSTICS_ROUTES)
def test_a_diagnostics_route_reads_across_the_whole_corpus(route: str) -> None:
    """History and search are about records other routes render, so they read that corpus."""
    assert ROUTE_COLLECTIONS[route] == DIAGNOSTICS_CORPUS
    assert set(_view(route).counts) == {c.value for c in DIAGNOSTICS_CORPUS}


def test_a_register_whose_producer_ships_at_dev4_counts_zero_rather_than_vanishing() -> None:
    """The empty boundary: a bound register that holds nothing was still read."""
    spine = _view("campaign", document={"track": DOCUMENT["track"]})

    assert spine.counts == {"campaign": 0}
    assert spine.rows == ()


def test_a_single_row_register_counts_one() -> None:
    """The off-by-one boundary below the probe document's two-row register."""
    one = {"batch": {"BAT-0001": DOCUMENT["batch"]["BAT-0001"]}}
    spine = _view("roadmap", document=one)

    assert spine.count("batch") == 1
    assert spine.count("milestone") == 0
    assert len(spine.rows) == 1


@pytest.mark.parametrize("route", GALLERY_ROUTES)
def test_rows_carry_the_status_the_document_states(route: str) -> None:
    """The one produced field is the stored status, and it is stored, not derived."""
    for row in _view(route).rows:
        status = row.field(STATUS_FIELD)
        assert status.state is TruthState.KNOWN
        assert status.truth_kind is TruthKind.STORED
        assert status.value == DOCUMENT[row.collection.value][row.key]["status"]


# ---------- the columns whose producers are dev4 items ----------


@pytest.mark.parametrize("route", GALLERY_ROUTES)
def test_every_dev4_column_is_an_unknown_truth_field_naming_why(route: str) -> None:
    """A Campaign, Decision or plan-lens column is declared and comes back unknown."""
    spine = _view(route)
    unproduced = spine.unproduced()

    assert unproduced
    assert STATUS_FIELD not in unproduced
    for row in spine.rows:
        for name in unproduced:
            field = row.field(name)
            assert field.state is TruthState.UNKNOWN
            assert field.value is None
            assert field.missing_reason == UNPRODUCED_REASON
            assert field.truth_kind is TruthKind.DERIVED


@pytest.mark.parametrize("route", GALLERY_ROUTES)
def test_the_frame_prints_the_unknown_token_beside_every_dev4_column(route: str) -> None:
    """The frame says which columns are silent instead of leaving empty cells."""
    spine = _view(route)
    unstated = next(row for row in _frame(spine) if row.startswith(" UNSTATED"))

    for name in spine.unproduced():
        assert f"{name} ?" in unstated


def test_a_row_names_no_field_the_route_did_not_declare() -> None:
    """Asking a row for an undeclared column raises rather than answering a blank."""
    row = _view("search").rows[0]

    with pytest.raises(KeyError):
        row.field("provider")


# ---------- refusals ----------


def test_a_bound_route_with_no_native_read_model_is_refused() -> None:
    """A projection of another route is not this frame's rows, so it is not adopted."""
    with pytest.raises(ValueError, match="has no native read model"):
        build_spine_view(_projection("activity"))


def test_an_unbound_route_has_no_projection_to_build_from() -> None:
    """A route with no document binding is refused at the projection, not papered over."""
    with pytest.raises(ValueError, match="renders no epoch-2 collection"):
        _projection("settings.stack")


def test_a_negative_cursor_is_refused() -> None:
    """A cursor is a committed ordinal, so there is no projection before the first one."""
    with pytest.raises(ValueError, match="never -1"):
        _projection("campaign", cursor=-1)


# ---------- the epoch-1 surfaces this wave did not touch ----------


def test_the_research_board_mode_resolves_every_key_its_footer_advertises() -> None:
    """The epoch-1 mode keeps working: each advertised token is a key something binds."""
    from eawf.surfaces.tui.app import EaApp
    from eawf.surfaces.tui.modes.research_board import ResearchBoardModeScreen

    bound: set[str] = set()
    for klass in (*ResearchBoardModeScreen.__mro__, EaApp):
        for binding in klass.__dict__.get("BINDINGS", ()):
            key = binding.key if hasattr(binding, "key") else binding[0]
            bound.update(key.split(","))
    spelled = {"↑": "up", "↓": "down", "Enter": "enter", "/": "slash", "?": "question_mark"}

    advertised = [hint.split(" ", 1)[0] for hint in ResearchBoardModeScreen.FOOTER_HINTS]
    tokens = [part for token in advertised for part in token.split("/")]

    assert tokens
    for token in tokens:
        # an arrow run advertises two keys in one token; every other token is one key
        glyphs = list(token) if all(glyph in spelled for glyph in token) else [token]
        for glyph in glyphs:
            assert spelled.get(glyph, glyph.lower()) in bound, f"{token} resolves to nothing"
