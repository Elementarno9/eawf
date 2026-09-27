"""CON-097: the command palette is one overlay over two result kinds, routes then entities.

One query filters the route list first -- alphabetical by route id, only routes that open
without a subject, each shown by the word naming what it opens -- then the entities it
matches, ranked by how: the id in full, ids beginning with it, ids containing it, then a
description that mentions it. One dashed rule sits between the kinds, drawn only when an
entity follows. An entity opens its own detail route through the id-prefix map. Verbs are
not a kind, and no key but ``/`` opens a search.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.keymap import GLOBAL_HELP
from eawf.surfaces.tui.console.palette import HitKind, PaletteEntity, hits, palette_rows
from eawf.surfaces.tui.console.registry import REGISTRY, route_for_id
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.tokens import RULE_PALETTE

from .overlay_support import press, prototype, session_on

W, H = 120, 30


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    """Return the prototype registers the golden contract replays."""
    return prototype()


def _entity(entity_id: str, what: str = "", route: str = "run.detail") -> PaletteEntity:
    return PaletteEntity(id=entity_id, route=route, what=what)


def test_con_097_the_route_list_is_alphabetical_and_subject_free() -> None:
    routes = [hit.route for hit in hits("", []) if hit.kind is HitKind.ROUTE]
    assert routes == sorted(routes)
    assert routes
    for route in routes:
        assert not REGISTRY.by_id[route].subject_required


def test_con_097_each_route_row_shows_the_word_naming_what_it_opens() -> None:
    words = {hit.route: hit.name for hit in hits("", []) if hit.kind is HitKind.ROUTE}
    assert words["entry"] == "attach workspace"
    for route, word in words.items():
        assert word == REGISTRY.route_word(route)


def test_con_097_routes_come_first_then_entities_ranked_by_how_they_matched() -> None:
    entities = [
        _entity("EAWF-0001", "mentions run-0001 in passing", "task.detail"),
        _entity("XRUN-0001"),
        _entity("RUN-00012"),
        _entity("RUN-0001"),
    ]
    found = hits("run-0001", entities)
    kinds = [hit.kind for hit in found]
    assert kinds == sorted(kinds, key=lambda kind: kind is HitKind.ENTITY)
    ranked = [hit.name for hit in found if hit.kind is HitKind.ENTITY]
    assert ranked == ["RUN-0001", "RUN-00012", "XRUN-0001", "EAWF-0001"]


def test_con_097_a_query_matching_nothing_draws_nothing_matches() -> None:
    session = Session(pq="zzzz")
    rows = palette_rows(session, hits("zzzz", [_entity("RUN-0001")]), w=W, h=H)
    assert [row.strip() for row in rows[2:]] == ["nothing matches"]


def test_con_097_the_dashed_rule_is_drawn_only_when_an_entity_follows() -> None:
    def rules(query: str) -> int:
        found = hits(query, [_entity("RUN-0001", "act")])
        rows = palette_rows(Session(pq=query), found, w=W, h=H)
        return sum(1 for row in rows if row.strip() and set(row.strip()) == {RULE_PALETTE})

    assert rules("activity") == 0
    assert rules("run-0001") == 0
    assert rules("act") == 1


@pytest.mark.parametrize(
    ("entity_id", "route"),
    [
        ("RUN-0000beef", "run.detail"),
        ("EAWF-0042", "task.detail"),
        ("BAT-0001", "batch.detail"),
        ("MLS-0004", "milestone"),
        ("REL-0001", "release"),
        ("CAM-0001", "campaign"),
        ("EVT-2201", "receipt"),
        ("CLM-0004", "evidence"),
    ],
)
def test_con_097_an_entity_opens_its_detail_route_through_the_id_prefix_map(
    entity_id: str, route: str
) -> None:
    assert route_for_id(entity_id) == route


def test_con_097_enter_on_an_entity_opens_its_detail_with_its_id(fixture: Fixture) -> None:
    session = session_on("scope.home")
    press(session, fixture, "/", *"run-5384", "Enter")
    assert (session.route, session.subj_id, session.overlay) == (
        "run.detail",
        "RUN-538453eb",
        None,
    )


def test_con_097_verbs_are_not_a_palette_kind(fixture: Fixture) -> None:
    assert set(HitKind) == {HitKind.ROUTE, HitKind.ENTITY}
    verbs = {verb.verb for verb in fixture.menus.verbs("run.detail")}
    assert verbs
    names = {hit.name for hit in hits("", [])}
    assert not verbs & names


def test_con_097_no_key_but_the_slash_opens_a_search() -> None:
    assert "search" not in REGISTRY.go_map.values()
    assert [token for token, _text, key in GLOBAL_HELP if key == "/"] == ["/"]
    for table in ROUTE_KEYS.values():
        assert not any("search" in entry.label for entry in table)


def test_con_097_an_entity_naming_an_unregistered_route_is_refused() -> None:
    with pytest.raises(ValueError, match="unregistered"):
        hits("x", [_entity("RUN-1", route="nowhere")])


def test_con_097_escape_leaves_the_palette_and_returns_focus(fixture: Fixture) -> None:
    session = session_on("activity", sel=3)
    press(session, fixture, "/", "ArrowDown", "ArrowDown", "Escape")
    assert (session.overlay, session.route, session.sel, session.pq) == (None, "activity", 3, "")


@pytest.mark.xfail(
    strict=True,
    reason="the golden contract lists search among the palette's routes and ends an "
    "overflowing list in an edge count; the search row is not yet drawn",
)
def test_con_097_search_is_never_a_route_row_and_an_overflow_ends_in_a_search_row() -> None:
    found = hits("", [_entity(f"RUN-{i:04d}") for i in range(80)])
    assert "search" not in {hit.route for hit in found if hit.kind is HitKind.ROUTE}
    rows = palette_rows(Session(), found, w=W, h=H)
    assert "search" in rows[-1]
