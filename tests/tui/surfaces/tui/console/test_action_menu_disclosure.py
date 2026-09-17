"""The command palette and the action menu disclose only what the route binds.

The action menu lists exactly its route's verbs, light first, refused ones with their
reasons, draws no caret, and is refused at construction when a menu names an unregistered
route, repeats a letter, or holds a light verb the registry gives no door. One judgement
decides what a pressed verb does. The palette reaches routes and entities and never a verb,
and its window keeps the cursor on screen. Both render as the test-only chassis renders
them, the palette's edge markers under the port's ``… N above`` and ``… N below`` wording.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from eawf.surfaces.tui.console.action_menu import (
    ACTIONS_OVERLAY,
    NO_ACTIONS_TEXT,
    NO_ACTIONS_TITLE,
    VERB_COLUMN,
    ActionMenus,
    Availability,
    MenuVerb,
    Outcome,
    VerbWeight,
    availability,
    menu_order,
    menu_rows,
    outcome,
    toggle,
)
from eawf.surfaces.tui.console.action_menu import PAIRS as MENU_PAIRS
from eawf.surfaces.tui.console.header import header_row
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.palette import (
    CHROME_ROWS,
    CRUMB,
    Hit,
    HitKind,
    PaletteEntity,
    hits,
    palette_rows,
    window,
)
from eawf.surfaces.tui.console.palette import PAIRS as PALETTE_PAIRS
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.session import SIZES, Session
from eawf.surfaces.tui.console.tokens import CARET
from eawf.surfaces.tui.console.width import cell_len

WIDTHS = [w for w, _h in SIZES]
CHASSIS = "tests.snapshots.tui.console.console_chassis.chassis"
PROTO = Path(__file__).resolve().parents[4] / "fixtures" / "console" / "golden" / "fixture"
_MORE = re.compile(r"^   \.\.\. \((\d+) more\)\s*$")


def _verb(row: Sequence[str]) -> MenuVerb:
    """Build a menu verb from one golden fixture action row."""
    key, verb, available, reason, authority, effects, non_effects, *rest = row
    return MenuVerb(
        key=key,
        verb=verb,
        available=available == "yes",
        reason=reason,
        authority=authority,
        effects=effects,
        non_effects=non_effects,
        weight=VerbWeight(rest[0]) if rest else VerbWeight.HEAVY,
        target=rest[1] if len(rest) > 1 else None,
    )


def _golden_rows() -> dict[str, list[list[str]]]:
    proto = json.loads((PROTO / "proto.json").read_text(encoding="utf-8"))
    rows: dict[str, list[list[str]]] = proto["actions"]
    return rows


ROWS = _golden_rows()
VERBS = {route: [_verb(row) for row in rows] for route, rows in ROWS.items()}
MENUS = ActionMenus(VERBS)
LIVE = Availability(True)


def _live(verb: MenuVerb) -> Availability:
    return availability(verb, mutable=True, refusal="")


def _light(key: str = "l", target: str | None = "transcript") -> MenuVerb:
    return MenuVerb(key=key, verb="open", available=True, weight=VerbWeight.LIGHT, target=target)


def _heavy(key: str = "c", *, available: bool = True, authority: str = "control") -> MenuVerb:
    return MenuVerb(
        key=key,
        verb="cancel",
        available=available,
        reason="" if available else "the run has ended",
        authority=authority,
    )


# ---------- the action menu ----------


def test_action_menus_golden_fixture_builds_against_the_registry() -> None:
    assert set(ROWS) <= set(REGISTRY.ids)
    for route in ROWS:
        assert MENUS.verbs(route)


@pytest.mark.parametrize("route", sorted(ROWS))
def test_action_menus_verbs_are_exactly_the_routes_own(route: str) -> None:
    listed = MENUS.verbs(route)
    assert listed == menu_order(VERBS[route])
    assert {(v.key, v.verb) for v in listed} == {(row[0], row[1]) for row in ROWS[route]}
    assert len({v.key for v in listed}) == len(listed)


@pytest.mark.parametrize("route", sorted(set(REGISTRY.ids) - set(ROWS)))
def test_action_menus_route_without_verbs_discloses_nothing(route: str) -> None:
    assert MENUS.verbs(route) == ()
    assert MENUS.verb(route, "a") is None


def test_action_menus_verb_resolves_only_its_routes_letter() -> None:
    steer = MENUS.verb("run.detail", "s")
    assert steer is not None and steer.verb == "steer"
    assert MENUS.verb("run.detail", "@") is None
    assert MENUS.verb("attention", "@") is not None


def test_menu_order_puts_light_verbs_first_in_declared_order() -> None:
    assert [v.key for v in MENUS.verbs("run.detail")] == list("lbmsnctfp")
    assert [v.key for v in MENUS.verbs("history")] == ["d", "y", "Y", "o"]
    assert menu_order([]) == ()


def test_action_menus_unregistered_route_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unregistered routes: nowhere"):
        ActionMenus({"nowhere": [_heavy()]})


def test_action_menus_repeated_letter_raises_value_error() -> None:
    with pytest.raises(ValueError, match="binds c to two verbs"):
        ActionMenus({"run.detail": [_heavy("c"), _heavy("c", available=False)]})


@pytest.mark.parametrize(
    ("route", "verb"),
    [
        ("run.detail", _light("z", "transcript")),
        ("run.detail", _light("d", "history.diff")),
        ("history", _light("l", "transcript")),
    ],
)
def test_action_menus_light_verb_without_registry_door_raises_value_error(
    route: str, verb: MenuVerb
) -> None:
    with pytest.raises(ValueError, match="through no registry door"):
        ActionMenus({route: [verb]})


def test_menu_verb_heavy_with_target_raises_value_error() -> None:
    with pytest.raises(ValueError, match="opens a card"):
        MenuVerb(key="l", verb="open", available=True, target="transcript")


def test_menu_verb_refused_without_reason_raises_value_error() -> None:
    with pytest.raises(ValueError, match="refused verb needs a reason"):
        MenuVerb(key="t", verb="retry", available=False)


def test_menu_verb_available_with_reason_raises_value_error() -> None:
    with pytest.raises(ValueError, match="available verb takes no reason"):
        MenuVerb(key="t", verb="retry", available=True, reason="stale")


@pytest.mark.parametrize("key", ["", " ", "ab", "中"])
def test_menu_verb_key_not_one_printable_cell_raises_value_error(key: str) -> None:
    with pytest.raises(ValueError, match="one printable character"):
        MenuVerb(key=key, verb="retry", available=True)


@pytest.mark.parametrize("verb", ["", "   ", "v" * (VERB_COLUMN + 1)])
def test_menu_verb_name_outside_its_column_raises_value_error(verb: str) -> None:
    with pytest.raises(ValueError, match=f"1 to {VERB_COLUMN} cells"):
        MenuVerb(key="t", verb=verb, available=True)


def test_menu_verb_name_at_the_column_width_is_accepted() -> None:
    assert MenuVerb(key="t", verb="v" * VERB_COLUMN, available=True).verb == "v" * VERB_COLUMN


def test_availability_refused_verb_keeps_its_own_reason() -> None:
    assert availability(_heavy(available=False), mutable=True, refusal="x") == Availability(
        False, "the run has ended"
    )


def test_availability_write_under_read_only_state_takes_the_live_refusal() -> None:
    guard = availability(_heavy(), mutable=False, refusal="the daemon cannot be reached")
    assert guard == Availability(False, "the daemon cannot be reached")


def test_availability_read_only_verb_acts_in_any_state() -> None:
    assert availability(_heavy(authority=""), mutable=False, refusal="x") == LIVE
    assert availability(_heavy(), mutable=True, refusal="x") == LIVE


@pytest.mark.parametrize(
    ("verb", "guard", "expected"),
    [
        (_light(), LIVE, Outcome.OPEN),
        (_light(target=None), LIVE, Outcome.COPY),
        (_light(), Availability(False, "no"), Outcome.REFUSED_TOAST),
        (_heavy(), LIVE, Outcome.CONSEQUENCE),
        (_heavy(), Availability(False, "no"), Outcome.REFUSED),
    ],
)
def test_outcome_light_acts_at_once_and_heavy_previews(
    verb: MenuVerb, guard: Availability, expected: Outcome
) -> None:
    assert outcome(verb, guard) == expected


@pytest.mark.parametrize("w", WIDTHS)
def test_menu_rows_list_refused_verbs_with_reasons_and_no_caret(w: int) -> None:
    lines = menu_rows(MENUS.verbs("run.detail"), guard=_live, w=w)
    assert lines[0].startswith("   ACTIONS   KEY  VERB                 REASON")
    assert len(lines) == 1 + len(ROWS["run.detail"])
    assert all(cell_len(line) == w for line in lines)
    assert not any(CARET in line for line in lines)
    retry = next(line for line in lines if " retry " in line)
    assert retry.rstrip().endswith("the run has not ended")
    steer = next(line for line in lines if " steer " in line)
    assert steer.rstrip().endswith("steer")


def test_menu_rows_clip_a_long_reason_to_its_column() -> None:
    verb = MenuVerb(key="t", verb="retry", available=False, reason="word " * 20)
    (line,) = menu_rows([verb], guard=_live, w=80)[1:]
    assert cell_len(line) == 80
    assert line.rstrip().endswith("…")


def test_menu_rows_empty_menu_says_so() -> None:
    lines = menu_rows([], guard=_live, w=80)
    assert lines[1].rstrip() == "   No verb is defined for this route."


def test_menu_rows_narrower_than_the_columns_raises_value_error() -> None:
    with pytest.raises(ValueError, match="needs 39 cells"):
        menu_rows([], guard=_live, w=38)


def test_toggle_opens_and_closes_the_drawer() -> None:
    session = Session(route="run.detail", c_target={"verb": "cancel"})
    assert toggle(session, MENUS) is True
    assert session.overlay == ACTIONS_OVERLAY
    assert session.c_target is None
    assert toggle(session, MENUS) is True
    assert session.overlay is None


def test_toggle_route_without_verbs_opens_nothing() -> None:
    session = Session(route="health")
    assert toggle(session, MENUS) is False
    assert session.overlay is None
    assert (NO_ACTIONS_TITLE, NO_ACTIONS_TEXT) == ("NO ACTIONS", "No action is available here.")


def test_action_menu_keybar_promises_neither_arrows_nor_enter() -> None:
    tokens = " ".join(token for token, _label in MENU_PAIRS)
    assert not set(tokens) & set("↑↓←→")
    assert "Enter" not in tokens
    assert keybar(MENU_PAIRS, 80).rstrip() == " key run · consequence first   Esc close"


# ---------- the command palette ----------


ENTITIES = (
    PaletteEntity(id="RUN-538453eb", route="run.detail", what="EAWF-0042 Bound replay"),
    PaletteEntity(id="BAT-0002", route="batch.detail", what="Replay batch"),
    PaletteEntity(id="EAWF-0042", route="task.detail", what="Bound replay"),
    PaletteEntity(id="RUN-538453eb", route="run.detail", what="a repeated id"),
)
VERB_NAMES = sorted({row[1] for rows in ROWS.values() for row in rows})


@pytest.mark.parametrize("query", ["", "a", "run", "replay", *VERB_NAMES])
def test_hits_disclose_routes_and_entities_never_verbs(query: str) -> None:
    found = hits(query, ENTITIES)
    assert {hit.kind for hit in found} <= {HitKind.ROUTE, HitKind.ENTITY}
    for hit in found:
        if hit.kind == HitKind.ROUTE:
            assert hit.route in REGISTRY.route_list
            assert hit.name == REGISTRY.route_word(hit.route)
            assert hit.subject is None
        else:
            assert hit.route == REGISTRY.by_id[hit.route].id
            assert hit.subject == hit.name


def test_hits_empty_query_lists_every_palette_route_then_every_entity_once() -> None:
    found = hits("", ENTITIES)
    routes = [hit.route for hit in found if hit.kind == HitKind.ROUTE]
    assert routes == list(REGISTRY.route_list)
    assert [hit.name for hit in found if hit.kind == HitKind.ENTITY] == [
        "RUN-538453eb",
        "BAT-0002",
        "EAWF-0042",
    ]


def test_hits_rank_entities_by_how_they_matched() -> None:
    entities = (
        PaletteEntity(id="X-1", route="run.detail", what="mentions bat-0002"),
        PaletteEntity(id="Y-BAT-0002", route="batch.detail", what=""),
        PaletteEntity(id="BAT-00021", route="batch.detail", what=""),
        PaletteEntity(id="BAT-0002", route="batch.detail", what=""),
    )
    assert [hit.name for hit in hits("BAT-0002", entities)] == [
        "BAT-0002",
        "BAT-00021",
        "Y-BAT-0002",
        "X-1",
    ]


def test_hits_query_matching_nothing_is_empty() -> None:
    assert hits("zzz-nothing", ENTITIES) == []


def test_hits_entity_on_an_unregistered_route_raises_value_error() -> None:
    bad = PaletteEntity(id="RUN-1", route="run.nowhere", what="")
    with pytest.raises(ValueError, match=r"unregistered 'run\.nowhere'"):
        hits("", [bad])


def test_palette_pairs_advertise_only_what_the_palette_binds() -> None:
    assert PALETTE_PAIRS == (("type", "search"), ("↑↓", "row"), ("Enter", "go"), ("Esc", "close"))


def _entity_hits(n: int) -> list[Hit]:
    return hits(
        "", [PaletteEntity(id=f"RUN-{i:04d}", route="run.detail", what="") for i in range(n)]
    )


@pytest.mark.parametrize("body", [0, -1])
def test_window_without_rows_raises_value_error(body: int) -> None:
    with pytest.raises(ValueError, match="at least one row"):
        window(_entity_hits(3), sel=0, scroll=0, body=body)


def test_window_empty_hits_shows_nothing() -> None:
    assert window([], sel=0, scroll=0, body=10).count == 0


@pytest.mark.parametrize("sel", [0, 1, 20, 40, 59])
def test_window_keeps_the_cursor_on_screen(sel: int) -> None:
    all_hits = _entity_hits(60)
    view = window(all_hits, sel=sel, scroll=0, body=20)
    assert view.start <= sel < view.start + view.count
    assert view.above == (view.start > 0)
    assert view.below == (view.start + view.count < len(all_hits))


def test_window_trims_a_row_for_the_below_marker() -> None:
    routes = len(REGISTRY.route_list)
    all_hits = _entity_hits(0)
    view = window(all_hits, sel=0, scroll=0, body=routes)
    assert (view.count, view.above, view.below) == (routes, False, False)
    trimmed = window(all_hits, sel=0, scroll=0, body=routes - 1)
    assert trimmed.below
    assert trimmed.count == routes - 2


def test_palette_rows_empty_query_result_says_nothing_matches() -> None:
    session = Session(pq="zzz", sel=3, pscroll=5)
    rows = palette_rows(session, [], w=80, h=24)
    assert rows[0].rstrip() == " / zzz▏"
    assert rows[-1].rstrip() == "   nothing matches"
    assert (session.sel, session.pscroll) == (0, 0)


@pytest.mark.parametrize("w", WIDTHS)
def test_palette_rows_mark_hidden_edges_and_clamp_the_cursor(w: int) -> None:
    all_hits = _entity_hits(200)
    session = Session(sel=500)
    rows = palette_rows(session, all_hits, w=w, h=24)
    assert session.sel == len(all_hits) - 1
    assert all(cell_len(row) == w for row in rows)
    assert len(rows) <= 24 - CHROME_ROWS + 2
    assert rows[2].rstrip().startswith("   … ")
    assert rows[2].rstrip().endswith(" above")
    assert rows[-1].lstrip().startswith(f"{CARET} RUN-0199")


def test_palette_rows_group_large_hidden_counts() -> None:
    session = Session()
    rows = palette_rows(session, _entity_hits(2000), w=80, h=24)
    assert rows[-1].rstrip().endswith("below")
    assert "1,9" in rows[-1]


def test_palette_rows_height_without_a_window_raises_value_error() -> None:
    with pytest.raises(ValueError, match="at least one row"):
        palette_rows(Session(), _entity_hits(1), w=80, h=CHROME_ROWS)


def _port_markers(rows: Sequence[str], w: int) -> list[str]:
    """Rewrite the pack's ``... (N more)`` rows into the port's edge markers."""
    out: list[str] = []
    for index, row in enumerate(rows):
        more = _MORE.match(row)
        if more is None:
            out.append(row)
            continue
        edge = "above" if index == 3 else "below"
        out.append(f"   … {int(more.group(1)):,} {edge}".ljust(w))
    return out


def test_palette_matches_the_test_only_chassis_under_the_port_markers() -> None:
    # parity holds only while both copies exist; the skip marks the chassis' removal
    palette = pytest.importorskip(f"{CHASSIS}.overlays.palette", exc_type=ModuleNotFoundError)
    sessions = pytest.importorskip(f"{CHASSIS}.session", exc_type=ModuleNotFoundError)
    fixtures = pytest.importorskip(f"{CHASSIS}.fixture", exc_type=ModuleNotFoundError)
    attention = pytest.importorskip(f"{CHASSIS}.attention", exc_type=ModuleNotFoundError)
    fixture = fixtures.load_fixture()
    entities = [
        PaletteEntity(id=e["id"], route=e["to"], what=e["what"]) for e in palette.entities(fixture)
    ]
    needs = attention.open_count(fixture)
    compared = 0
    for w, h in SIZES:
        for query in ("", "r", "run-5", "home", "zzz"):
            theirs = sessions.Session(route="activity", overlay="palette", pq=query)
            ours = Session(route="activity", overlay="palette", pq=query)
            total = len(palette.hits(theirs, fixture))
            for sel in [*range(total + 2), total // 2, 0]:
                theirs.sel = ours.sel = sel
                expected = _port_markers(palette.render(theirs, fixture, w, h), w)
                got = [
                    header_row(ours, crumb=CRUMB, scope=fixture.scope, needs=needs, w=w),
                    *palette_rows(ours, hits(query, entities), w=w, h=h),
                ]
                got += [" " * w] * (h - 1 - len(got))
                got.append(keybar(PALETTE_PAIRS, w))
                assert got == expected, (w, query, sel)
                assert (ours.sel, ours.pscroll) == (theirs.sel, theirs.pscroll)
                compared += 1
    assert compared > 0


def test_menu_rows_match_the_test_only_chassis_drawer() -> None:
    # parity holds only while both copies exist; the skip marks the chassis' removal
    drawer = pytest.importorskip(f"{CHASSIS}.drawers.actions", exc_type=ModuleNotFoundError)
    sessions = pytest.importorskip(f"{CHASSIS}.session", exc_type=ModuleNotFoundError)
    fixtures = pytest.importorskip(f"{CHASSIS}.fixture", exc_type=ModuleNotFoundError)
    attention = pytest.importorskip(f"{CHASSIS}.attention", exc_type=ModuleNotFoundError)
    fixture = fixtures.load_fixture()
    for conn in ("LIVE", "OFFLINE SNAPSHOT"):
        for route, rows in ROWS.items():
            theirs = sessions.Session(route=route, conn=conn)
            by_key = {row[0]: tuple(row) for row in rows}

            def guard(verb: MenuVerb, _s: Any = theirs, _rows: Any = by_key) -> Availability:
                ok, why = attention.verb_available(_s, fixture, _rows[verb.key])
                return Availability(ok, why)

            for w, h in SIZES:
                expected = [row.ljust(w) for row in drawer.render(theirs, fixture, w, h)]
                assert menu_rows(MENUS.verbs(route), guard=guard, w=w) == expected, (route, w)
