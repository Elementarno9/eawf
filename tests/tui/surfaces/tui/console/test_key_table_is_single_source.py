"""CON-098: the help overlay prints the one key table, for this route and everywhere.

Help lists THIS ROUTE -- the route's key table verbatim, including any pair the keybar
dropped for width -- then EVERYWHERE, the global grammar, then the guarded-quit sentence.
Every key it names is one the dispatcher lets through; key names are full names; nothing a
simulator did is listed.
"""

from __future__ import annotations

import re

import pytest

from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.keymap import GLOBAL_HELP, allowlist, route_keys
from eawf.surfaces.tui.console.overlays import render_overlay
from eawf.surfaces.tui.console.session import SIZES

from .overlay_support import frame_of, prototype, session_on, view_of

_ENTRY = re.compile(r"^   (?P<token>\S.*?)  +(?P<label>\S.*?)\s*$")
#: Routes whose table the frame draws natively or from the prototype, every one keyed.
ROUTES: tuple[str, ...] = tuple(sorted(r for r in ROUTE_KEYS if r != "entry"))
_ABBREVIATED = ("PgUp", "PgDn", "↑/↓", "Tab/⇧Tab", "Ctrl-C", "Esc Esc quits only if")
_SIMULATOR = ("cycle frame size", "[ ]", "state N of")


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    """Return the prototype registers the golden contract replays."""
    return prototype()


def _block(rows: list[str], title: str) -> list[tuple[str, str]]:
    """Return the token and label pairs listed under block ``title``."""
    at = next(i for i, row in enumerate(rows) if row.strip() == title)
    pairs: list[tuple[str, str]] = []
    for row in rows[at + 1 :]:
        found = _ENTRY.match(row)
        if found is None:
            break
        pairs.append((found["token"], found["label"]))
    return pairs


def _help(route: str, fixture: Fixture) -> list[str]:
    return render_overlay("help", view_of(session_on(route, overlay="help"), fixture, 2))


@pytest.mark.parametrize("route", ROUTES)
def test_con_098_help_prints_the_routes_key_table_verbatim(route: str, fixture: Fixture) -> None:
    table = route_keys(session_on(route), fixture)
    assert _block(_help(route, fixture), "THIS ROUTE") == [(e.token, e.label) for e in table]


@pytest.mark.parametrize("route", ROUTES)
def test_con_098_help_prints_the_global_grammar_and_the_guarded_quit(
    route: str, fixture: Fixture
) -> None:
    rows = _help(route, fixture)
    assert _block(rows, "EVERYWHERE") == [(token, text) for token, text, _key in GLOBAL_HELP]
    assert any(row.startswith(" Esc Esc quits only at scope home") for row in rows)


@pytest.mark.parametrize("route", ROUTES)
def test_con_098_help_advertises_no_key_the_route_does_not_bind(
    route: str, fixture: Fixture
) -> None:
    session = session_on(route)
    bound = allowlist(session, fixture)
    for entry in route_keys(session, fixture):
        assert set(entry.keys) <= bound, entry.token
    for _token, _text, key in GLOBAL_HELP:
        assert key == "ctrl+c" or key in bound


@pytest.mark.parametrize("route", ROUTES)
def test_con_098_help_names_keys_in_full_and_lists_no_simulator_affordance(
    route: str, fixture: Fixture
) -> None:
    text = "\n".join(_help(route, fixture))
    for token in (*_ABBREVIATED, *_SIMULATOR):
        assert token not in text


def test_con_098_help_lists_a_pair_the_keybar_dropped_for_width(fixture: Fixture) -> None:
    narrow = frame_of(session_on("activity"), fixture, 0)[-1]
    assert ". actions" not in narrow
    rows = render_overlay("help", view_of(session_on("activity", overlay="help"), fixture, 0))
    assert (".", "actions") in _block(rows, "THIS ROUTE")


@pytest.mark.parametrize("size", range(len(SIZES)))
def test_con_098_help_is_the_same_table_at_every_size(size: int, fixture: Fixture) -> None:
    rows = render_overlay("help", view_of(session_on("activity", overlay="help"), fixture, size))
    wide = _help("activity", fixture)
    assert _block(rows, "THIS ROUTE") == _block(wide, "THIS ROUTE")
    assert _block(rows, "EVERYWHERE") == _block(wide, "EVERYWHERE")


def test_con_098_a_route_with_no_table_says_only_the_global_set_applies(fixture: Fixture) -> None:
    rows = _help("no.such.route", fixture)
    assert _block(rows, "THIS ROUTE") == []
    assert "   no route-local key — only the global set below applies" in [r.rstrip() for r in rows]
