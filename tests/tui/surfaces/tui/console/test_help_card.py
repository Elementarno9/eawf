"""The help card and the overlay crumbs: what help teaches, and where an overlay says it is.

Help teaches the keys the frame under it offers and every global key, ``!`` included, and
still carries the quit rule at 80x24. A linked console's overlay names the project it is
open in and the route by its word, never the chrome's placeholder scope or a route id.
"""

from __future__ import annotations

import dataclasses

import pytest

from eawf.surfaces.tui.console.app import compose_frame
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.keybar import KEY
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.session import BackEntry, Session
from tests.tui.surfaces.tui.console import test_native_route_bodies as bodies
from tests.tui.surfaces.tui.console.test_guarded_quit import FIXTURE
from tests.tui.surfaces.tui.console.test_native_navigation import _milestone

from .overlay_support import Host


def _named(view: View) -> View:
    """Return ``view`` as a launched console draws it: attached to the project ``eawf``."""
    return dataclasses.replace(view, scope_name="eawf")


def _over(view: View, overlay: str) -> list[str]:
    """Draw the route once, as the running console has, then the overlay over it."""
    compose_frame(view)
    view.session.overlay = overlay
    return compose_frame(view)


def _this_route(card: list[str]) -> list[str]:
    start = card.index(next(row for row in card if row.startswith(" THIS ROUTE"))) + 1
    end = card.index(next(row for row in card if row.startswith(" EVERYWHERE"))) - 1
    return card[start:end]


# ---------- an overlay names the project and the route by its word ----------


@pytest.mark.parametrize(
    ("overlay", "crumb"),
    [("help", " Eä ▸ eawf ▸ help · home"), ("palette", " Eä ▸ eawf ▸ palette")],
)
def test_an_overlay_crumb_names_the_project(overlay: str, crumb: str) -> None:
    card = _over(_named(bodies._view("scope.home")), overlay)
    assert card[0].startswith(crumb)


def test_an_overlay_over_a_path_names_the_project_never_the_placeholder() -> None:
    view = _named(bodies._view("track", subject="TRK-CORE"))
    view.session.back.record(BackEntry(route="scope.home", sel=0, subj=None))
    head = _over(view, "help")[0]
    assert head.startswith(" Eä ▸ eawf ▸ ")
    assert " ? " not in head
    assert "help · track" in head


def test_the_prototype_replay_keeps_its_recorded_crumb() -> None:
    session = Session()
    session.route = "activity"
    card = _over(View(session=session, fixture=FIXTURE, w=80, h=24), "help")
    assert card[0].startswith(" Eä ▸ help · activity ")


# ---------- help teaches only what the frame under it offers ----------


def test_help_leaves_out_a_key_the_frame_gave_up() -> None:
    """A Milestone with no acceptance bundle offers no digest copy, and help says none."""
    view = _milestone("MLS-0100")
    card = _over(view, "help")
    assert view.session.route_bar_keys is not None
    assert not view.session.route_bar_keys.intersection(KEY["digest"].keys)
    assert not any(KEY["digest"].label in row for row in _this_route(card))


@pytest.mark.parametrize("route", ["scope.home", "activity", "track"])
def test_every_key_help_teaches_for_the_route_is_one_its_bar_offered(route: str) -> None:
    view = bodies._view(route, subject="TRK-CORE" if route == "track" else None)
    card = _over(view, "help")
    offered = view.session.route_bar_keys
    assert offered is not None
    rows = _this_route(card)
    assert rows
    for entry in KEY.values():
        if any(row.lstrip().startswith(f"{entry.token} ") for row in rows):
            assert offered.intersection(entry.keys), entry.token


# ---------- the attention jump is taught, and the quit rule still fits ----------


def test_help_lists_the_attention_jump_and_keeps_the_quit_rule_at_80x24() -> None:
    session = Session()
    session.route = "activity"
    card = _over(View(session=session, fixture=FIXTURE, w=80, h=24), "help")
    assert any(row.startswith("   !          jump to your top attention item") for row in card)
    assert any("80ms to 1.5s apart" in row for row in card)
    assert len(card) == 24


# ---------- a card taller than the frame scrolls rather than cutting a key ----------


def _card(session: Session, h: int) -> list[str]:
    return compose_frame(View(session=session, fixture=FIXTURE, w=80, h=h))


def _key(session: Session, key: str, h: int) -> list[str]:
    """Press ``key`` on the frame drawn at ``h`` rows, then return the frame it leaves."""
    _card(session, h)
    dispatch(Ctx(session=session, fixture=FIXTURE, host=Host(), w=80, h=h), key, False)
    return _card(session, h)


def _scrolled_help(route: str, h: int) -> tuple[Session, list[list[str]]]:
    """Open help over ``route`` in an ``h``-row frame and scroll it to its end, frame by frame."""
    session = Session()
    session.route = route
    frames = [_key(session, "?", h)]
    for _ in range(40):
        if session.help_top >= session.help_max:
            break
        frames.append(_key(session, "PageDown", h))
    return session, frames


def test_help_card_taller_than_the_frame_windows_its_table_and_says_so() -> None:
    session, frames = _scrolled_help("activity", 16)
    first = frames[0]
    assert len(first) == 16
    assert first[0].startswith(" Eä ▸ help · activity")
    assert first[-2].startswith(" WINDOW    1–")  # noqa: RUF001
    assert "PageUp PageDown scroll" in first[-1] and "Esc close" in first[-1]
    assert session.help_max > 0
    assert "80ms to 1.5s apart" not in "".join(first)
    assert "80ms to 1.5s apart" in "".join(frames[-1]), "the quit rule is reachable"


def test_help_card_scrolling_reaches_every_row_of_the_full_card() -> None:
    session = Session()
    session.route = "activity"
    _key(session, "?", 60)
    full = [row.rstrip() for row in _card(session, 60)[3:] if row.strip()][:-1]
    _session, frames = _scrolled_help("activity", 16)
    seen = {row.rstrip() for frame in frames for row in frame}
    assert [row for row in full if row not in seen] == []


def test_help_card_scroll_stops_at_both_ends() -> None:
    session, _frames = _scrolled_help("activity", 16)
    end = session.help_top
    _key(session, "PageDown", 16)
    assert session.help_top == end
    for _ in range(end + 2):
        _key(session, "PageUp", 16)
    assert session.help_top == 0


@pytest.mark.parametrize("key", ["PageDown", "ArrowDown", "j"])
def test_help_card_that_fits_claims_paging_and_arrows_as_a_noop(key: str) -> None:
    session = Session()
    session.route = "activity"
    before = _key(session, "?", 24)
    sel = session.sel
    after = _key(session, key, 24)
    assert (session.overlay, session.sel, session.help_top) == ("help", sel, 0)
    assert after == before
    assert "scroll" not in before[-1]


def test_help_card_taller_than_the_frame_advertises_no_arrow() -> None:
    _session, frames = _scrolled_help("activity", 16)
    assert "↑" not in frames[0][-1] and "↓" not in frames[0][-1]


def test_help_card_reopens_at_its_first_row() -> None:
    session, _frames = _scrolled_help("activity", 16)
    _key(session, "Escape", 16)
    _key(session, "?", 16)
    assert (session.overlay, session.help_top) == ("help", 0)


def test_help_card_over_a_windowed_native_home_at_80x24_keeps_every_key_reachable() -> None:
    view = _named(bodies._view("scope.home", w=80))
    compose_frame(view)
    view.session.route_windowed = True
    view.session.overlay = "help"
    view.session.help_top = 0
    rows: set[str] = set()
    for _ in range(40):
        card = compose_frame(view)
        assert len(card) == 24
        rows.update(card)
        if view.session.help_top >= view.session.help_max:
            break
        view.session.help_top += 1
    text = "\n".join(rows)
    assert "Ctrl+C" in text and "80ms to 1.5s apart" in text


@pytest.mark.parametrize("w", [78, 80, 118])
def test_help_card_wraps_the_quit_rule_rather_than_clipping_it(w: int) -> None:
    session = Session()
    session.route = "activity"
    _card(session, 60)
    session.overlay = "help"
    card = compose_frame(View(session=session, fixture=FIXTURE, w=w, h=60))
    text = " ".join(row.strip() for row in card)
    assert "80ms to 1.5s apart." in text
    assert not any(row.rstrip().endswith("…") for row in card)
