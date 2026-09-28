"""The keybar fits its width budget by dropping globals in a fixed order (CON-032).

The budget is the frame width less one margin cell, counted with the leading margin, so a
bar at exactly the budget keeps every pair and one cell over it drops a pair. A route verb
never drops: the global pairs leave in the fixed order ``? help``, ``/ palette``,
``. actions``, ``Esc back``, ``g go``, then the copy and inspect pairs, and every global a
bar drops is still listed in help's EVERYWHERE block. Every route table fits at the three
frame sizes, with the attention bar at 77 cells and the activity bar at 69 cells at 80
columns as the pinned cases.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eawf.surfaces.tui.console.app import compose_frame
from eawf.surfaces.tui.console.fixture import load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.keybar import (
    ABBREVIATED,
    GAP,
    GLOBAL_DROP_ORDER,
    GLOBAL_TOKENS,
    KEY,
    KEY_NAMES,
    MARGIN,
    ROUTE_KEYS,
    KeyEntry,
    assert_full_key_names,
    budget,
    key_token,
    keybar,
    route_bar,
    unregistered_routes,
)
from eawf.surfaces.tui.console.keymap import GLOBAL_KEYS, acts_here
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.session import SIZES, Session
from eawf.surfaces.tui.console.width import assert_known_width, cell_len

WIDTHS = [w for w, _h in SIZES]
ROUTES = sorted(ROUTE_KEYS)
CHASSIS = "tests.snapshots.tui.console.console_chassis.chassis"
SEQUENCES = Path(__file__).resolve().parents[4] / "fixtures" / "console" / "golden" / "sequences"


def _pairs(route: str) -> list[tuple[str, str]]:
    return [entry.pair() for entry in ROUTE_KEYS[route]]


def _used(bar: str) -> int:
    """Return the cells the margin and the pairs fill, without the trailing padding."""
    return cell_len(bar.rstrip())


def _golden_keybars() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for path in sorted(SEQUENCES.glob("frames-*.json")):
        for state in json.loads(path.read_text(encoding="utf-8"))["states"]:
            found.append((state["id"], state["size"][0], state["frame"].split("\n")[-1]))
    return found


def test_budget_leaves_one_margin_cell_at_each_size() -> None:
    assert [budget(w) for w in WIDTHS] == [79, 119, 159]
    assert budget(2) == 1


@pytest.mark.parametrize("w", [1, 0, -5])
def test_budget_too_narrow_raises_value_error(w: int) -> None:
    with pytest.raises(ValueError, match="at least 2 cells"):
        budget(w)


def test_route_keys_cover_every_registry_route_but_the_entry_layer() -> None:
    assert set(ROUTE_KEYS) == set(REGISTRY.ids) - {"entry"}


def test_unregistered_routes_names_only_the_unknown_table_routes() -> None:
    assert unregistered_routes(ROUTE_KEYS) == ()
    tables = {**ROUTE_KEYS, "nowhere": (KEY["esc"],), "also.nowhere": ()}
    assert unregistered_routes(tables) == ("also.nowhere", "nowhere")
    assert unregistered_routes({}) == ()


def _shown(bar: str) -> list[str]:
    return bar[MARGIN:].rstrip().split(GAP)


@pytest.mark.parametrize("w", WIDTHS)
@pytest.mark.parametrize("route", ROUTES)
def test_con032_route_bar_fits_budget_and_never_drops_a_route_verb(route: str, w: int) -> None:
    """CON-032: the bar is the whole route table plus a width-bounded part of the globals."""
    bar = route_bar(route, w)
    assert cell_len(bar) == w
    assert _used(bar) <= budget(w)
    shown = _shown(bar)
    pairs = [f"{token} {label}" for token, label in _pairs(route)]
    verbs = [
        p
        for (token, _label), p in zip(_pairs(route), pairs, strict=True)
        if token not in GLOBAL_TOKENS
    ]
    assert [p for p in shown if p in verbs] == verbs
    assert [p for p in pairs if p in shown] == shown


def _help_tokens(route: str) -> set[str]:
    return {key.token.split(" ")[0] for key in GLOBAL_KEYS if acts_here(key, route)}


@pytest.mark.parametrize("w", WIDTHS)
@pytest.mark.parametrize("route", ROUTES)
def test_con032_every_dropped_global_is_listed_in_help(route: str, w: int) -> None:
    """CON-032: a global the bar drops still acts, and help's EVERYWHERE block lists it."""
    shown = set(_shown(route_bar(route, w)))
    dropped = [token for token, label in _pairs(route) if f"{token} {label}" not in shown]
    assert set(dropped) <= _help_tokens(route), (route, w, dropped)


def test_con032_globals_drop_in_the_fixed_order() -> None:
    """CON-032: ``? / . Esc g`` leave first, in that order, then copy and inspect from the tail."""
    verb = ("Enter", "x" * 20)
    globals_ = [
        ("i", "inspect"),
        ("g", "go"),
        ("y", "copy"),
        ("Esc", "back"),
        (".", "actions"),
        ("?", "help"),
        ("/", "palette"),
        ("-", "dismiss"),
    ]
    pairs = [verb, *globals_]
    order: list[str] = []
    kept = [token for token, _label in pairs]
    w = cell_len(" " + GAP.join(f"{t} {lab}" for t, lab in pairs)) + 1
    while len(kept) > 1:
        w -= 1
        now = [piece.split(" ")[0] for piece in _shown(keybar(pairs, w))]
        order += [token for token in kept if token not in now]
        kept = now
    assert order == [*GLOBAL_DROP_ORDER, "-", "y", "i"]
    assert kept == ["Enter"]


def test_con032_the_first_pair_never_drops_even_when_it_is_a_global() -> None:
    """CON-032: a bar whose lead pair is a global keeps it and drops the pair behind it."""
    bar = keybar([("y", "copy " * 5), ("Enter", "drill " * 5)], 40)
    assert bar.startswith(" y copy")
    assert "Enter" not in bar


def test_con032_a_bar_of_route_verbs_only_drops_from_the_tail_past_its_budget() -> None:
    """CON-032: with no global left, only a table breaking the fit contract loses a verb."""
    pairs = [("a", "x" * 30), ("b", "x" * 30), ("c", "x" * 30)]
    assert _shown(keybar(pairs, 80)) == [f"a {'x' * 30}", f"b {'x' * 30}"]


def test_con032_attention_frame_advertises_enter_and_sits_at_77_cells() -> None:
    """CON-032: the drawn attention bar advertises Enter and drops both globals at 80."""
    root = Path(__file__).resolve().parents[4] / "fixtures" / "console" / "golden" / "fixture"
    session = Session()
    session.route = "attention"
    frame = compose_frame(View(session=session, fixture=load_fixture(root), w=80, h=24))
    assert frame[-1].rstrip() == (
        " ↑↓ row   Enter open   Tab buckets   a answer   x deny   z snooze   v resolve"
    )
    assert _used(frame[-1]) == 77


def test_con032_attention_bucket_bar_reads_clear_bucket_where_it_fits() -> None:
    """CON-032: while a bucket is set, the Escape pair reads ``Esc clear bucket``."""
    root = Path(__file__).resolve().parents[4] / "fixtures" / "console" / "golden" / "fixture"
    session = Session()
    session.route = "attention"
    session.bucket = "needs"
    frame = compose_frame(View(session=session, fixture=load_fixture(root), w=160, h=40))
    assert frame[-1].rstrip().endswith("Esc clear bucket")


def test_route_bar_pinned_attention_and_activity_at_80() -> None:
    attention = route_bar("attention", 80)
    activity = route_bar("activity", 80)
    assert _used(attention) == 77
    assert attention.rstrip().endswith("v resolve")
    assert _used(activity) == 69
    assert activity.rstrip() == (
        " ↑↓ row   PageUp PageDown page   Enter drill   Tab buckets   \\ filter"
    )
    assert ". actions" in route_bar("activity", 120)


def test_route_bar_unknown_route_raises_key_error() -> None:
    with pytest.raises(KeyError):
        route_bar("nowhere", 80)


@pytest.mark.parametrize("w", WIDTHS)
def test_keybar_at_budget_keeps_every_pair(w: int) -> None:
    head = ("Enter", "drill")
    fixed = cell_len(" " * MARGIN + f"Enter drill{GAP}x ")
    pairs = [head, ("x", "y" * (budget(w) - fixed))]
    bar = keybar(pairs, w)
    assert _used(bar) == budget(w)
    assert bar.endswith(" ")
    assert bar.rstrip().endswith("y")


@pytest.mark.parametrize("w", WIDTHS)
def test_keybar_one_cell_over_budget_drops_tail_pair(w: int) -> None:
    fixed = cell_len(" " * MARGIN + f"Enter drill{GAP}x ")
    pairs = [("Enter", "drill"), ("x", "y" * (budget(w) - fixed + 1))]
    assert keybar(pairs, w) == (" Enter drill").ljust(w)


def test_keybar_drops_several_tail_pairs_in_order() -> None:
    pairs = [(f"{n}", "label" * 3) for n in range(10)]
    bar = keybar(pairs, 80)
    shown = bar[MARGIN:].rstrip().split(GAP)
    assert shown == [f"{n} {'label' * 3}" for n in range(len(shown))]
    assert 1 <= len(shown) < 10


def test_keybar_single_pair_wider_than_frame_is_clipped_to_the_frame() -> None:
    bar = keybar([("Enter", "a very long label " * 10)], 80)
    assert cell_len(bar) == 80
    assert bar.startswith(" Enter a very long label")


def test_keybar_empty_pairs_is_a_blank_row() -> None:
    assert keybar([], 80) == " " * 80


def test_keybar_zero_width_raises_value_error() -> None:
    with pytest.raises(ValueError, match="at least 2 cells"):
        keybar([("Esc", "back")], 0)


@pytest.mark.parametrize("abbreviation", sorted(ABBREVIATED))
def test_keybar_abbreviated_key_name_raises_value_error(abbreviation: str) -> None:
    with pytest.raises(ValueError, match=f"abbreviates {ABBREVIATED[abbreviation]}"):
        keybar([("Enter", "drill"), (abbreviation, "page")], 80)


@pytest.mark.parametrize("token", ["↑/↓", "Tab/⇧Tab", "l/L", "PgUp PgDn"])
def test_assert_full_key_names_slashed_or_abbreviated_raises_value_error(token: str) -> None:
    with pytest.raises(ValueError, match=r"slashed pair|abbreviates"):
        assert_full_key_names(token)


@pytest.mark.parametrize("token", ["/", "\\", "g …", "1 2 3", "[ ]", "PageUp PageDown", "?"])
def test_assert_full_key_names_full_or_single_keys_pass(token: str) -> None:
    assert_full_key_names(token)


@pytest.mark.parametrize("token", ["", "   "])
def test_assert_full_key_names_blank_raises_value_error(token: str) -> None:
    with pytest.raises(ValueError, match="at least one key"):
        assert_full_key_names(token)


def test_key_token_spells_full_key_names() -> None:
    assert KEY["page"].token == "PageUp PageDown"
    assert KEY["ends"].token == "Home End"
    assert KEY["esc"].token == "Esc"
    assert KEY["enter"].token == "Enter"
    assert KEY["up"].token == "↑↓"
    assert key_token(("ArrowLeft", "ArrowRight")) == "←→"
    assert key_token(("ArrowUp", "Enter", "ArrowDown")) == "↑ Enter ↓"


def test_key_token_empty_keys_raises_value_error() -> None:
    with pytest.raises(ValueError, match="at least one key"):
        key_token(())


def test_key_token_abbreviated_key_raises_value_error() -> None:
    with pytest.raises(ValueError, match="abbreviates PageDown as PgDn"):
        key_token(("PageUp", "PgDn"))


@pytest.mark.parametrize("label", ["", "  "])
def test_key_entry_blank_label_raises_value_error(label: str) -> None:
    with pytest.raises(ValueError, match="needs a label"):
        KeyEntry(label, ("Enter",))


def test_key_entry_abbreviated_key_raises_value_error() -> None:
    with pytest.raises(ValueError, match="abbreviates PageUp"):
        KeyEntry("page", ("PgUp",))


@pytest.mark.parametrize("w", WIDTHS)
def test_keybar_page_and_ends_pairs_render_full_names(w: int) -> None:
    bar = keybar([KEY["page"].pair(), KEY["ends"].pair()], w)
    assert bar.rstrip() == " PageUp PageDown page   Home End ends"


@pytest.mark.parametrize("w", WIDTHS)
def test_route_bar_never_spells_an_abbreviated_key(w: int) -> None:
    words = {word for route in ROUTES for word in route_bar(route, w).split()}
    assert words.isdisjoint(ABBREVIATED)


@pytest.mark.parametrize("glyph", sorted(set(KEY_NAMES.values()) - {"Esc"}))
def test_key_names_arrow_glyph_has_a_known_single_cell_width(glyph: str) -> None:
    assert_known_width(glyph)
    assert cell_len(glyph) == 1


@pytest.mark.parametrize(("frame_id", "w", "bar"), _golden_keybars())
def test_keybar_budget_holds_on_every_golden_bar(frame_id: str, w: int, bar: str) -> None:
    # a golden bar that filled the whole row would drop a pair under the budget rule
    assert cell_len(bar) == w, frame_id
    assert _used(bar) <= budget(w), frame_id


def test_keybar_matches_the_test_only_chassis_on_every_table() -> None:
    # parity holds only while both copies exist; the skip marks the chassis' removal
    frame = pytest.importorskip(f"{CHASSIS}.frame", exc_type=ModuleNotFoundError)
    keys = pytest.importorskip(f"{CHASSIS}.keys", exc_type=ModuleNotFoundError)
    tables = [[entry.pair() for entry in table] for table in keys.ROUTE_KEYS.values()]
    tables += [list(pairs) for pairs in keys.OVERLAY_PAIRS.values()]
    tables += [list(pairs) for pairs in keys.DRAWER_PAIRS.values()]
    full = {"PgUp PgDn": KEY["page"].token}
    compared = 0
    for table in tables:
        pairs = [(full.get(token, token), label) for token, label in table]
        for w in WIDTHS:
            theirs = frame.keybar(pairs, w)
            assert keybar(pairs, w) == theirs
            compared += 1
    assert compared == len(tables) * len(WIDTHS)
