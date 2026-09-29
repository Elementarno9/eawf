"""The route registry is the closed route set, and every frame is composed from its rows.

One module holds the eight packet rows that close the registry, each section named for
its row so a failure points at the obligation it breaks:

- ``UI-058`` every route carries a route group, a sub-surface takes its parent's, and
  the Escape parent table is total: every route climbs to the root, and a sub-surface
  climbs to the entity it is about.
- ``UI-059`` the registry is the closed route set: routes, entity sub-surfaces, the
  global notifications route and the entry states, each row with its key, label, group,
  question, projection, read model, subject rule, rail and Escape parent; every shipped
  renderer has a row and every row a renderer; the palette and ``g`` tables derive from
  the subject rule.
- ``UI-039`` design coverage is declared per renderer: each row has a golden frame at
  every recorded size or a declared hole with its reason, and no hole outlives its frame.
- ``UI-033`` a route composes as a pane stack with at most one rail, and a rail renders
  only where its row declares one, at the widths it names.
- ``UI-034`` one route at one revision agrees across 80, 120 and 160 columns on the
  header, the selection, the row order, every count and every verdict.
- ``UI-004`` every internal link resolves to a registered, rendered route, and a link to
  something nobody holds renders its absence rather than another record.
- ``UI-011`` every route opens by keys alone, every journey is keys alone, and every route
  renders to plain text with the facts the styled frame carries.
- ``UI-040`` each composition fact has one owner: the rail breakpoints, the ``g`` letters,
  the overlay set and the focus regions are read from the registry tables, never restated
  by the frame that draws them.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.surfaces.tui.console.chrome import ConsoleChrome, load_chrome
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch, parent_of
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View, strip_chips
from eawf.surfaces.tui.console.harness import Contract, FrameState, load_contract
from eawf.surfaces.tui.console.keymap import DRAWER_KEYS
from eawf.surfaces.tui.console.navigation import Ctx, go
from eawf.surfaces.tui.console.overlays import OVERLAY_RENDERERS
from eawf.surfaces.tui.console.plain import plain_rows, render_plain
from eawf.surfaces.tui.console.registry import (
    DRILL_PREFIXES,
    ENTRY_STATE_IDS,
    ENTRY_STATES,
    OVERLAYS,
    REGISTRY,
    ROOT_ROUTE,
    ROUTE_FOR_ID,
    ROUTE_OF,
    ROUTES,
    Door,
    DoorKind,
    Escape,
    Rail,
    RouteGroup,
    RouteRegistry,
    RouteSpec,
    escape_defects,
)
from eawf.surfaces.tui.console.renderers import ROUTE_MODULES, activity, render_route
from eawf.surfaces.tui.console.session import SIZES, Session, SessionSetup

TESTS_ROOT = Path(__file__).resolve().parents[4]
GOLDEN = TESTS_ROOT / "fixtures" / "console" / "golden"
WIDTHS: tuple[int, ...] = tuple(w for w, _ in SIZES)

#: A typed id as a frame prints it; the ids a row order and a selection are compared by.
TYPED_ID = re.compile(
    r"(?<![\w-])(?:RUN-[0-9a-f]{8}|EAWF-\d{4}|BAT-\d{4}|MLS-\d{4}|REL-\d{4}|CAM-\d{4}"
    r"|CLM-\d{4}|ACT-\d{4}|EVT-\d{4}|RCP-\d{4})(?![\w-])"
)
#: A verdict or state word: an upper-case token a row states about its record.
VERDICT = re.compile(r"(?<![\w-])[A-Z][A-Z-]{2,}(?![\w-])")
NUMBER = re.compile(r"(?<![\w.-])\d+(?![\w.-])")
#: The shortest run of rows a pane divider spans; a shorter run is a marker, not a pane.
DIVIDER_RUN = 3
DIVIDER = "│"


#: Each route group's routes, the sub-surfaces by their parent, and the declared holes,
#: indexed off the registry's own rows.
GROUPS: dict[RouteGroup, tuple[str, ...]] = {
    g: tuple(s.id for s in REGISTRY.routes if s.group is g) for g in RouteGroup
}
SUB_SURFACES: dict[str, str] = {s.id: s.sub_surface_of for s in REGISTRY.routes if s.sub_surface_of}
HOLES: dict[str, str] = {s.id: s.hole for s in REGISTRY.routes if s.hole}


@pytest.fixture(scope="module")
def contract() -> Contract:
    """Return the tracked golden contract."""
    return load_contract(GOLDEN / "sequences")


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    """Return the prototype registers the golden contract replays."""
    return load_fixture(GOLDEN / "fixture")


def _row(route: str, group: RouteGroup, **columns: Any) -> RouteSpec:
    """Return a probe row over a stated question and a root Escape parent."""
    base: dict[str, Any] = {
        "question": "probe?",
        "needs": "nothing",
        "escape": Escape(route=ROOT_ROUTE),
        "palette_visible": True,
    }
    merged = {**base, **columns}
    return RouteSpec(id=route, group=group, **merged)


def _replace(route: str, **columns: Any) -> tuple[RouteSpec, ...]:
    """Return the shipped rows with ``route``'s row changed by ``columns``."""
    return tuple(
        dataclasses.replace(spec, **columns) if spec.id == route else spec for spec in ROUTES
    )


def _route_states(contract: Contract) -> dict[str, dict[int, FrameState]]:
    """Return the route frame states by base id, then by width."""
    found: dict[str, dict[int, FrameState]] = {}
    for state in contract.states:
        if state.kind == "route":
            base, _, _ = state.id.rpartition("@")
            found.setdefault(base, {})[state.size[0]] = state
    return found


def _golden_routes(contract: Contract) -> dict[str, set[int]]:
    """Return, per route id, the widths the contract records a route frame at."""
    found: dict[str, set[int]] = {}
    for state in contract.states:
        if state.kind == "route" and state.setup.route:
            found.setdefault(state.setup.route, set()).add(state.size[0])
    return found


def _render(fixture: Fixture, setup: SessionSetup) -> list[str]:
    """Return the frame ``setup`` renders through the route renderer, markers stripped."""
    session = Session()
    session.reset(setup, settings_section_order=fixture.settings.section_order)
    w, h = SIZES[session.size]
    rows = render_route(View(session=session, fixture=fixture, w=w, h=h, held=True))
    return [strip_chips(row) for row in rows]


def _size_index(w: int) -> int:
    return WIDTHS.index(w)


def pane_dividers(rows: Sequence[str]) -> list[int]:
    """Return the columns a pane divider runs down from the first body row.

    A divider is a vertical rule inside the frame, not at its edge, that starts on the
    first row under the heavy rule and holds for :data:`DIVIDER_RUN` rows: the seam
    between a pane stack and a rail, which a section rule joins with a tee. A bordered
    card's edges sit at the frame edge and a marker column crosses the rows it runs
    through, so neither counts.
    """
    body = list(rows[3:-1])
    if not body:
        return []
    w = len(rows[0])
    columns: list[int] = []
    for col, ch in enumerate(body[0]):
        if ch != DIVIDER or col in (0, w - 1):
            continue
        run = 0
        for row in body:
            if col < len(row) and row[col] in (DIVIDER, "├", "┤"):
                run += 1
            else:
                break
        if run >= DIVIDER_RUN:
            columns.append(col)
    return columns


def _selected(rows: Sequence[str]) -> str | None:
    """Return the typed id, else the first word, of the row the caret marks."""
    for row in rows[3:-1]:
        found = re.match(r"^\s*▸\s*(\S+)", row)
        if found:
            ids = TYPED_ID.findall(row)
            return ids[0] if ids else found.group(1)
    return None


def _verdicts(rows: Sequence[str]) -> dict[str, set[str]]:
    """Return, per typed id, the verdict words on the first row that names it."""
    out: dict[str, set[str]] = {}
    for row in rows[3:-1]:
        ids = TYPED_ID.findall(row)
        if ids and ids[0] not in out:
            out[ids[0]] = set(VERDICT.findall(row)) - set(ids[0].split("-"))
    return out


def triple_disagreements(frames: Mapping[int, Sequence[str]]) -> list[str]:
    """Return every way one route's frames at several widths disagree.

    The narrowest frame is the reference. A wider frame agrees when its header states
    the same words, the caret marks the same record, every id the narrow frame lists
    appears in the same order, the context row keeps every count, and every record keeps
    its verdicts; it may add rows, columns and counts of its own.
    """
    widths = sorted(frames)
    narrow = frames[widths[0]]
    problems: list[str] = []
    ids_narrow = list(dict.fromkeys(TYPED_ID.findall("\n".join(narrow[3:-1]))))
    for w in widths[1:]:
        wide = frames[w]
        if narrow[0].split() != wide[0].split():
            problems.append(f"{w}: header differs")
        if _selected(narrow) != _selected(wide):
            problems.append(f"{w}: selection {_selected(narrow)} vs {_selected(wide)}")
        ids_wide = list(dict.fromkeys(TYPED_ID.findall("\n".join(wide[3:-1]))))
        missing = [i for i in ids_narrow if i not in ids_wide]
        if missing:
            problems.append(f"{w}: rows dropped {missing}")
        elif [i for i in ids_wide if i in ids_narrow] != ids_narrow:
            problems.append(f"{w}: row order differs")
        if not set(NUMBER.findall(narrow[1])) <= set(NUMBER.findall(wide[1])):
            problems.append(f"{w}: counts differ")
        verdicts_wide = _verdicts(wide)
        for record, words in _verdicts(narrow).items():
            if record in verdicts_wide and not words <= verdicts_wide[record]:
                problems.append(f"{w}: verdicts of {record} differ")
    return problems


# ---------- UI-058: route groups and the total Escape parent table ----------


def test_ui_058_route_groups_are_the_eight_the_packet_names() -> None:
    assert [g.value for g in RouteGroup] == [
        "entry",
        "spine",
        "live",
        "acceptance",
        "planning",
        "diagnostics",
        "verification",
        "operations",
    ]


def test_ui_058_every_route_carries_a_route_group_never_a_family() -> None:
    fields = {f.name for f in dataclasses.fields(RouteSpec)}
    assert "group" in fields
    assert "family" not in fields
    assert all(isinstance(spec.group, RouteGroup) for spec in ROUTES)
    assert set().union(*map(set, GROUPS.values())) == set(REGISTRY.ids)


@pytest.mark.parametrize(("route", "parent"), sorted(SUB_SURFACES.items()))
def test_ui_058_a_sub_surface_takes_its_parent_group_and_escapes_to_it(
    route: str, parent: str
) -> None:
    spec = REGISTRY.by_id[route]
    assert spec.group is REGISTRY.by_id[parent].group
    assert spec.escape is not None and spec.escape.route == parent


def test_ui_058_a_receipt_takes_the_group_of_the_task_its_row_names_first() -> None:
    assert SUB_SURFACES["receipt"] == "task.detail"
    assert REGISTRY.by_id["receipt"].group is RouteGroup.SPINE


def test_ui_058_the_escape_parent_table_is_total() -> None:
    assert escape_defects(ROUTES) == []
    assert REGISTRY.by_id[ROOT_ROUTE].escape is None
    assert set(REGISTRY.escapes) == set(REGISTRY.ids) - {ROOT_ROUTE}
    for route in REGISTRY.ids:
        step, seen = route, 0
        while step != ROOT_ROUTE:
            step = REGISTRY.escapes[step].route
            seen += 1
            assert seen <= len(REGISTRY.ids)


@pytest.mark.parametrize(
    ("route", "parent", "via"),
    [
        ("task.detail", "batch.detail", "BATCH"),
        ("batch.detail", "milestone", "MILESTONE"),
        ("milestone", "track", "TRACK"),
        ("run.detail", "task.detail", "SCOPE"),
    ],
)
def test_ui_058_the_spine_climbs_its_record_parents(route: str, parent: str, via: str) -> None:
    assert REGISTRY.escapes[route] == Escape(route=parent, via=via)


@pytest.mark.parametrize(
    ("route", "subject", "expected"),
    [
        ("task.detail", "EAWF-0042", ("batch.detail", "BAT-0002")),
        ("batch.detail", "BAT-0002", ("milestone", "MLS-0001")),
        ("milestone", "MLS-0001", ("track", "Runtime")),
        ("run.detail", "RUN-538453eb", ("task.detail", "EAWF-0042")),
        ("evidence.digest", None, ("evidence", None)),
        ("campaign.step", None, ("campaign", None)),
        ("history.diff", None, ("history", None)),
        ("activity", None, (ROOT_ROUTE, None)),
        # a record that names no parent climbs to the root rather than nowhere
        ("task.detail", "EAWF-9999", (ROOT_ROUTE, None)),
        ("task.detail", None, (ROOT_ROUTE, None)),
        (ROOT_ROUTE, None, None),
    ],
)
def test_ui_058_escape_on_an_empty_back_stack_climbs_the_registry_parent(
    fixture: Fixture,
    route: str,
    subject: str | None,
    expected: tuple[str, str | None] | None,
) -> None:
    session = Session()
    session.route, session.subj_id = route, subject
    assert parent_of(session, fixture) == expected


class _Host:
    """The dispatcher's host: a held clock and a quit that records it was asked."""

    def __init__(self) -> None:
        self._clock = FakeClock()
        self.quits = 0

    @property
    def clock(self) -> Clock:
        """Return the console clock."""
        return self._clock

    def quit(self) -> None:
        """Record that the console was asked to end."""
        self.quits += 1


def test_ui_058_the_escape_key_walks_the_parent_table(fixture: Fixture) -> None:
    session = Session()
    session.route, session.subj_id = "task.detail", "EAWF-0042"
    ctx = Ctx(session=session, fixture=fixture, host=_Host(), w=80, h=24)
    dispatch(ctx, "Escape")
    assert (session.route, session.subj_id) == ("batch.detail", "BAT-0002")
    dispatch(ctx, "Escape")
    assert (session.route, session.subj_id) == ("milestone", "MLS-0001")


def test_ui_058_a_sub_surface_escaping_past_its_parent_refuses_to_build() -> None:
    rows = _replace("settings.stack", escape=Escape(route=ROOT_ROUTE))
    with pytest.raises(
        ValueError, match=re.escape("sub-surface settings.stack escapes past its parent")
    ):
        RouteRegistry(rows)


def test_ui_058_a_sub_surface_outside_its_parent_group_refuses_to_build() -> None:
    rows = _replace("settings.stack", group=RouteGroup.OPERATIONS)
    with pytest.raises(ValueError, match="sub-surfaces outside their parent's group"):
        RouteRegistry(rows)


def test_ui_058_a_second_root_refuses_to_build() -> None:
    with pytest.raises(ValueError, match=re.escape("no Escape parent must be scope.home alone")):
        RouteRegistry(_replace("health", escape=None))


def test_ui_058_an_escape_loop_refuses_to_build() -> None:
    rows = _replace("history", escape=Escape(route="history.diff"))
    with pytest.raises(ValueError, match="Escape from history loops"):
        RouteRegistry(rows)


def test_ui_058_an_escape_to_an_unregistered_route_refuses_to_build() -> None:
    with pytest.raises(ValueError, match=re.escape("unregistered routes: spike.gone")):
        RouteRegistry(_replace("health", escape=Escape(route="spike.gone")))


def test_ui_058_an_escape_with_a_fixed_subject_and_a_field_is_refused() -> None:
    with pytest.raises(ValueError, match="names a subject and a via field"):
        Escape(route="milestone", subject="MLS-0001", via="MILESTONE")


# ---------- UI-059: the closed route set ----------


def test_ui_059_every_shipped_renderer_has_a_row_and_every_row_a_renderer() -> None:
    assert set(ROUTE_MODULES) == set(REGISTRY.ids)


@pytest.mark.parametrize("route", REGISTRY.ids)
def test_ui_059_every_row_states_its_declared_columns(route: str) -> None:
    spec = REGISTRY.by_id[route]
    assert spec.key
    assert REGISTRY.step_leaf(route, None) or route == ROOT_ROUTE
    assert isinstance(spec.group, RouteGroup)
    assert spec.question.strip().endswith("?")
    assert spec.needs.strip()
    assert spec.read_model is not None
    assert (spec.escape is None) == (route == ROOT_ROUTE)
    assert spec.rail is None or isinstance(spec.rail, Rail)


def test_ui_059_the_route_set_holds_the_notifications_route_as_a_global_row() -> None:
    spec = REGISTRY.by_id["notifications"]
    assert spec.sub_surface_of is None
    assert spec.group is RouteGroup.DIAGNOSTICS
    assert spec.escape == Escape(route=ROOT_ROUTE)


def test_ui_059_the_entry_states_are_registry_rows_the_chrome_must_match() -> None:
    assert tuple(state.id for state in load_chrome().entry) == ENTRY_STATE_IDS
    assert [s.key for s in ENTRY_STATES] == [
        "resolving",
        "resolution.ambiguous",
        "resolution.failed",
        "migration.required",
        "migration.interrupted",
        "schema.unsupported",
        "offline.snapshot",
        "onboarding",
    ]
    assert all(s.label and s.question.endswith("?") for s in ENTRY_STATES)


def test_ui_059_a_chrome_with_an_unregistered_entry_state_is_refused() -> None:
    raw = load_chrome().model_dump(by_alias=True)
    raw["entry"] = [*raw["entry"], {**raw["entry"][0], "id": "spike.state"}]
    with pytest.raises(ValidationError, match="not the registry's"):
        ConsoleChrome.model_validate(raw)


def test_ui_059_a_chrome_missing_an_entry_state_is_refused() -> None:
    raw = load_chrome().model_dump(by_alias=True)
    raw["entry"] = raw["entry"][:-1]
    with pytest.raises(ValidationError, match="not the registry's"):
        ConsoleChrome.model_validate(raw)


def test_ui_059_overlays_and_drawers_are_render_forms_never_rows() -> None:
    assert set(OVERLAY_RENDERERS) == set(OVERLAYS)
    assert set(DRAWER_KEYS).isdisjoint(REGISTRY.ids)
    # an overlay sharing a name with a route is drawn by the overlay table, not the route's
    for name in OVERLAYS:
        if name in ROUTE_MODULES:
            assert OVERLAY_RENDERERS[name] is not ROUTE_MODULES[name].render


def test_ui_059_the_palette_and_go_tables_derive_from_the_subject_rule() -> None:
    needs_subject = {s.id for s in ROUTES if s.subject_required or s.sub_surface_of}
    assert needs_subject.isdisjoint(REGISTRY.route_list)
    assert needs_subject.isdisjoint(REGISTRY.go_map.values())


@pytest.mark.parametrize(
    "columns",
    [{"subject_required": True, "go_letter": "z"}, {"subject_required": True}],
)
def test_ui_059_a_route_needing_a_subject_offered_by_g_or_the_palette_is_refused(
    columns: dict[str, Any],
) -> None:
    row = _row("spike.subject", RouteGroup.DIAGNOSTICS, **columns)
    with pytest.raises(ValueError, match="routes needing a subject offered by g or the palette"):
        RouteRegistry((*ROUTES, row))


@pytest.mark.parametrize("columns", [{"question": ""}, {"needs": "   "}])
def test_ui_059_a_row_stating_no_question_or_projection_is_refused(
    columns: dict[str, Any],
) -> None:
    row = _row("spike.silent", RouteGroup.DIAGNOSTICS, **columns)
    with pytest.raises(
        ValueError, match=re.escape("routes stating no question or projection: spike.silent")
    ):
        RouteRegistry((*ROUTES, row))


# ---------- UI-039: design coverage declared per renderer ----------


@pytest.mark.parametrize("route", REGISTRY.ids)
def test_ui_039_every_row_has_goldens_at_every_size_or_a_declared_hole(
    contract: Contract, route: str
) -> None:
    drawn = _golden_routes(contract).get(route, set())
    hole = HOLES.get(route, "")
    if hole:
        assert not drawn, f"{route} declares a hole but the contract draws it at {drawn}"
    else:
        assert drawn == set(WIDTHS), f"{route} has goldens at {sorted(drawn)} and no hole"


def test_ui_039_the_entry_states_are_golden_at_every_size(contract: Contract) -> None:
    drawn = {state.id for state in contract.states if state.kind == "entry"}
    for state in ENTRY_STATES:
        assert {f"entry/{state.id}@{w}" for w in WIDTHS} <= drawn


def test_ui_039_the_open_holes_are_the_undrawn_sub_surfaces() -> None:
    assert sorted(HOLES) == ["campaign.artifact", "campaign.step", "evidence.digest"]
    assert all(HOLES[route].strip() for route in HOLES)
    assert set(HOLES) <= set(SUB_SURFACES)


def test_ui_039_a_stale_hole_is_detected(contract: Contract) -> None:
    stale = RouteRegistry(_replace("health", hole="undrawn"))
    drawn = _golden_routes(contract)
    assert [s.id for s in stale.routes if s.hole and drawn.get(s.id)] == ["health"]


# ---------- UI-033: pane stack and at most one declared rail ----------


def test_ui_033_the_rails_are_the_three_the_registry_declares() -> None:
    assert dict(REGISTRY.rails) == {
        "activity": Rail(name="buckets", min_width=120),
        "attention": Rail(name="buckets", min_width=120),
        "settings": Rail(name="categories", min_width=1),
    }


@pytest.mark.parametrize(
    ("route", "w", "expected"),
    [
        ("activity", 80, None),
        ("activity", 119, None),
        ("activity", 120, "buckets"),
        ("activity", 160, "buckets"),
        ("attention", 80, None),
        ("attention", 120, "buckets"),
        ("settings", 80, "categories"),
        ("health", 160, None),
        ("spike.gone", 160, None),
    ],
)
def test_ui_033_rail_at_answers_the_declared_rail_from_its_width(
    route: str, w: int, expected: str | None
) -> None:
    rail = REGISTRY.rail_at(route, w)
    assert (rail.name if rail else None) == expected


@pytest.mark.parametrize("width", [0, -1])
def test_ui_033_a_rail_with_no_width_to_start_at_is_refused(width: int) -> None:
    with pytest.raises(ValueError, match="starts at"):
        Rail(name="buckets", min_width=width)


def test_ui_033_a_rail_named_as_a_focus_region_is_refused() -> None:
    rows = _replace("track", rail=Rail(name="queue", min_width=120))
    with pytest.raises(ValueError, match="routes naming their rail a focus region: track"):
        RouteRegistry(rows)


def test_ui_033_every_golden_frame_draws_a_rail_exactly_where_one_is_declared(
    contract: Contract,
) -> None:
    wrong: list[str] = []
    for state in contract.states:
        if state.kind != "route" or not state.setup.route:
            continue
        dividers = pane_dividers(state.frame.split("\n"))
        declared = REGISTRY.rail_at(state.setup.route, state.size[0]) is not None
        if len(dividers) > 1 or bool(dividers) != declared:
            wrong.append(f"{state.id}: {dividers} declared={declared}")
    assert not wrong


def test_ui_033_every_rendered_frame_draws_a_rail_exactly_where_one_is_declared(
    contract: Contract, fixture: Fixture
) -> None:
    wrong: list[str] = []
    for state in contract.states:
        if state.kind != "route" or not state.setup.route:
            continue
        rows = _render(fixture, state.setup)
        dividers = pane_dividers(rows)
        declared = REGISTRY.rail_at(state.setup.route, state.size[0]) is not None
        if len(dividers) > 1 or bool(dividers) != declared:
            wrong.append(f"{state.id}: {dividers} declared={declared}")
    assert not wrong


def test_ui_033_pane_dividers_find_one_rail_and_ignore_a_card_or_a_marker() -> None:
    w = 20
    head = ["h" * w, "c" * w, "=" * w]
    rail = [f"{'a' * 10}│{'b' * 9}"] * 4
    card = ["┌" + "─" * (w - 2) + "┐", *(["│" + " " * (w - 2) + "│"] * 3)]
    marker = [f"{'a' * 10}│{'b' * 9}", f"{'a' * 10}┼{'b' * 9}", " " * w, " " * w]
    three = [f"{'a' * 5}│{'b' * 6}│{'c' * 7}"] * 4
    assert pane_dividers([*head, *rail, "k" * w]) == [10]
    assert pane_dividers([*head, *card, "k" * w]) == []
    assert pane_dividers([*head, *marker, "k" * w]) == []
    assert pane_dividers([*head, *three, "k" * w]) == [5, 12]
    assert pane_dividers([*head, "k" * w]) == []


# ---------- UI-034: one route, three sizes, one answer ----------


def test_ui_034_every_golden_triple_agrees(contract: Contract) -> None:
    problems = {
        base: triple_disagreements({w: s.frame.split("\n") for w, s in states.items()})
        for base, states in _route_states(contract).items()
        if set(states) == set(WIDTHS)
    }
    assert {k: v for k, v in problems.items() if v} == {}


def test_ui_034_every_rendered_triple_agrees(contract: Contract, fixture: Fixture) -> None:
    problems: dict[str, list[str]] = {}
    for base, states in _route_states(contract).items():
        if set(states) != set(WIDTHS):
            continue
        frames = {w: _render(fixture, s.setup) for w, s in states.items()}
        problems[base] = triple_disagreements(frames)
    assert problems
    assert {k: v for k, v in problems.items() if v} == {}


def _frame(header: str, ctx: str, *body: str) -> list[str]:
    return [header, ctx, "=" * 10, *body, "keys"]


@pytest.mark.parametrize(
    ("wide", "problem"),
    [
        (_frame("Eä ▸ x !3", "4 runs", "▸ RUN-00000001", "  RUN-00000002"), "header differs"),
        (_frame("Eä ▸ x !4", "4 runs", "  RUN-00000001", "▸ RUN-00000002"), "selection"),
        (_frame("Eä ▸ x !4", "4 runs", "▸ RUN-00000001"), "rows dropped"),
        (_frame("Eä ▸ x !4", "4 runs", "  RUN-00000002", "▸ RUN-00000001"), "row order differs"),
        (_frame("Eä ▸ x !4", "5 runs", "▸ RUN-00000001", "  RUN-00000002"), "counts differ"),
        (
            _frame("Eä ▸ x !4", "4 runs", "▸ RUN-00000001 FAILED", "  RUN-00000002"),
            "verdicts of RUN-00000001 differ",
        ),
    ],
)
def test_ui_034_a_disagreeing_triple_is_a_failure(wide: list[str], problem: str) -> None:
    narrow = _frame("Eä ▸ x !4", "4 runs", "▸ RUN-00000001 RUNNING", "  RUN-00000002")
    if "verdicts" not in problem:
        narrow = _frame("Eä ▸ x !4", "4 runs", "▸ RUN-00000001", "  RUN-00000002")
    found = triple_disagreements({80: narrow, 120: wide})
    assert any(problem in item for item in found), found


def test_ui_034_a_wider_frame_may_add_rows_and_counts() -> None:
    narrow = _frame("Eä ▸ x !4", "4 runs", "▸ RUN-00000001")
    wide = _frame("Eä ▸ x  !4", "4 runs · 9 blocks", "▸ RUN-00000001 RUNNING", "  RUN-00000003")
    assert triple_disagreements({80: narrow, 160: wide}) == []


# ---------- UI-004: every internal link resolves or states its absence ----------


def test_ui_004_every_link_the_registry_declares_opens_a_rendered_route() -> None:
    targets = {route for _, route in ROUTE_FOR_ID} | set(ROUTE_OF.values())
    targets |= set(REGISTRY.go_map.values()) | set(REGISTRY.route_list)
    targets |= {escape.route for escape in REGISTRY.escapes.values()}
    targets |= {d.origin for s in ROUTES for d in s.doors if d.origin}
    assert targets <= set(ROUTE_MODULES)


@pytest.mark.parametrize(
    ("route", "subject"),
    [
        ("task.detail", "EAWF-9999"),
        ("batch.detail", "BAT-9999"),
        ("run.detail", "RUN-99999999"),
        ("milestone", "MLS-9999"),
        ("campaign", "CAM-9999"),
        ("evidence", "CLM-9999"),
        ("release", "REL-9999"),
        ("track", "TRK-9999"),
    ],
)
def test_ui_004_a_drill_onto_a_record_nobody_holds_renders_its_absence(
    fixture: Fixture, route: str, subject: str
) -> None:
    session = Session()
    session.route, session.subj_id = route, subject
    rows = render_route(View(session=session, fixture=fixture, w=80, h=24))
    text = "\n".join(rows)
    assert session.absent_frame, f"{route} drew a frame for {subject} without saying it is absent"
    assert subject in rows[0] or subject in rows[1]
    assert "∅" in text


@pytest.mark.parametrize(("route", "subject"), [("campaign", "CAM-0001"), ("release", None)])
def test_ui_004_the_recorded_subject_still_renders_its_record(
    fixture: Fixture, route: str, subject: str | None
) -> None:
    session = Session()
    session.route, session.subj_id = route, subject
    render_route(View(session=session, fixture=fixture, w=80, h=24))
    assert not session.absent_frame


@pytest.mark.parametrize("route", [r for r in REGISTRY.ids if r != "entry"])
@pytest.mark.parametrize("w", WIDTHS)
def test_ui_004_a_route_with_nothing_read_says_what_it_would_answer(route: str, w: int) -> None:
    session = Session()
    session.route = route
    fixture = Fixture.from_chrome(load_chrome())
    rows = render_route(View(session=session, fixture=fixture, w=w, h=SIZES[_size_index(w)][1]))
    text = " ".join(" ".join(row.split()) for row in rows)
    spec = REGISTRY.by_id[route]
    assert "NOT HELD" in rows[1]
    assert f"ANSWERS {' '.join(spec.question.split())}" in text
    assert f"NEEDS {' '.join(spec.needs.split())}" in text
    assert all(len(row) == w for row in rows)


def test_ui_004_go_refuses_an_unregistered_route_and_names_it(fixture: Fixture) -> None:
    session = Session()
    ctx = Ctx(session=session, fixture=fixture, host=_Host(), w=80, h=24)
    assert not go(ctx, "spike.gone", "probe")
    assert session.route == ROOT_ROUTE
    assert "spike.gone is designed but not bound here" in session.log[0].note


# ---------- UI-011: keys alone and plain text ----------

_KEYBOARD_DOORS = frozenset(DoorKind)


@pytest.mark.parametrize("route", REGISTRY.ids)
def test_ui_011_every_route_opens_by_keys_alone(route: str) -> None:
    doors = REGISTRY.doors(route)
    assert doors
    assert {door.kind for door in doors} <= _KEYBOARD_DOORS
    for door in doors:
        if door.kind is DoorKind.DRILL:
            assert door.key in DRILL_PREFIXES[route]


def test_ui_011_every_journey_is_driven_by_keys_alone(contract: Contract) -> None:
    keys = {step.key for journey in contract.journeys for step in journey.steps if step.key}
    assert keys
    assert all(re.fullmatch(r"[ -~]|Arrow(Up|Down|Left|Right)|Enter|Escape|Tab", k) for k in keys)


@pytest.mark.parametrize("route", [r for r in REGISTRY.ids if r != "entry"])
@pytest.mark.parametrize("size", range(len(SIZES)))
def test_ui_011_every_route_renders_to_plain_text_without_losing_a_fact(
    fixture: Fixture, route: str, size: int
) -> None:
    setup = SessionSetup(route=route, size=size)
    rows = render_plain(fixture, setup)
    w, h = SIZES[size]
    assert len(rows) == h
    assert all(len(row) == w and row.isascii() for row in rows)
    styled = _render(fixture, setup.model_copy(update={"conn": "OFFLINE SNAPSHOT"}))
    # the words of the styled frame survive the ASCII twin, glyphs aside
    words = {t for row in styled[1:] for t in row.split() if t.isascii() and len(t) > 2}
    plain = {t for row in rows[1:] for t in row.split()}
    assert words <= plain


@pytest.mark.parametrize("index", range(len(ENTRY_STATES)))
def test_ui_011_every_entry_state_renders_to_plain_text(fixture: Fixture, index: int) -> None:
    rows = render_plain(fixture, SessionSetup.model_validate({"route": "entry", "entrySel": index}))
    assert len(rows) == SIZES[0][1]
    assert all(len(row) == SIZES[0][0] and row.isascii() for row in rows)


def test_ui_011_plain_rows_of_an_empty_frame_is_empty() -> None:
    assert plain_rows([]) == []


# ---------- UI-040: one owner per composition fact ----------


def test_ui_040_the_rail_breakpoint_is_read_from_the_registry(
    monkeypatch: pytest.MonkeyPatch, fixture: Fixture
) -> None:
    setup = SessionSetup(route="activity", size=0)
    assert pane_dividers(_render(fixture, setup)) == []
    moved = RouteRegistry(_replace("activity", rail=Rail(name="buckets", min_width=80)))
    monkeypatch.setattr(activity, "REGISTRY", moved)
    assert len(pane_dividers(_render(fixture, setup))) == 1


def test_ui_040_the_go_drawer_reads_its_letters_from_the_registry() -> None:
    assert set(DRAWER_KEYS["go"]) == {*REGISTRY.go_map, "Escape"}


def test_ui_040_a_route_door_names_a_door_kind_the_registry_owns() -> None:
    with pytest.raises(ValueError, match="declares go doors"):
        _row("spike.a", RouteGroup.SPINE, doors=(Door(kind=DoorKind.GO, key="q"),))
