"""A native table is windowed around its cursor, so the caret never leaves the screen.

A daemon-served read model can hold far more rows than a terminal is tall. The native
register, spine and read-model frames draw only the rows that fit, move that window only
when the cursor would leave it, and say on a ``WINDOW`` line which slice of the table is
on screen. Their keybars advertise the page and ends keys that move the window a screen
or a table at a time.

The journey here is the one that broke: 1,490 Runs, fifty ``ArrowDown`` presses, then
``PageDown``, ``End``, ``Home`` and ``PageUp``, re-rendering after every press the way
the console does.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.projection.compute import build_route_projection
from eawf.kernel.projection.operations import build_operations_view
from eawf.kernel.projection.registers import build_register_view
from eawf.kernel.projection.spine import build_spine_view
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import RowWindow, View, window_rows
from eawf.surfaces.tui.console.keybar import KEY, ROUTE_KEYS
from eawf.surfaces.tui.console.keymap import native_keys
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import Session

AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
SCOPE = "EAWF"
ROWS = 1490
PRESSES = 50
W, H = 120, 40

#: What a caret row opens with: the frame's one-cell margin, then the caret.
_CARET = " ▸ "
_WINDOW = re.compile(r"^ WINDOW    (?:(?P<a>[\d,]+)–(?P<b>[\d,]+)|0) of (?P<n>[\d,]+)")  # noqa: RUF001


def _fixture() -> Fixture:
    """Return the tracked console fixture the epoch-1 mode draws."""
    return load_fixture(Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture")


def _runs(n: int) -> dict[str, Any]:
    """Return ``n`` Runs in three statuses, keyed so they sort in the order written."""
    statuses = ("RUNNING", "BLOCKED", "CANCELLED")
    return {
        f"RUN-{i:08x}": {
            "urn": f"{SCOPE}/run/RUN-{i:08x}",
            "revision": 1,
            "status": statuses[i % 3],
        }
        for i in range(n)
    }


def _tracks(n: int) -> dict[str, Any]:
    """Return ``n`` tracks, keyed so they sort in the order written."""
    return {
        f"TRK-{i:04d}": {
            "urn": f"urn:eawf:{SCOPE}:track:TRK-{i:04d}",
            "revision": 1,
            "status": "ACTIVE",
        }
        for i in range(n)
    }


def _projection(route: str, document: dict[str, Any]) -> Any:
    """Return ``route``'s projection over ``document``."""
    return build_route_projection(
        route=route, document=document, cursor=41208, scope_id=SCOPE, generated_at=AT
    )


def register_view(session: Session, n: int = ROWS) -> View:
    """Return a view of the native Activity register over ``n`` Runs."""
    session.route = "activity"
    register = build_register_view(_projection("activity", {"run": _runs(n)}))
    return View(session=session, fixture=_fixture(), w=W, h=H, register=register)


def spine_view(session: Session, n: int = ROWS, *, route: str = "track") -> View:
    """Return a view of a native spine route over ``n`` tracks."""
    session.route = route
    spine = build_spine_view(_projection(route, {"track": _tracks(n)}))
    return View(session=session, fixture=_fixture(), w=W, h=H, projection=spine)


def home_view(session: Session, n: int = ROWS) -> View:
    """Return a view of the native scope home over ``n`` tracks.

    Home is the one spine route whose epoch-1 frame walks a tree of its own, so it is the
    one whose arrows must be seen reaching the native rows.
    """
    return spine_view(session, n, route="scope.home")


def read_model_view(session: Session, n: int = ROWS) -> View:
    """Return a view of the native crash-recovery read model over ``n`` Runs."""
    session.route = "crash.recovery"
    model = build_operations_view(_projection("crash.recovery", {"run": _runs(n)}))
    return View(session=session, fixture=_fixture(), w=W, h=H, projection=model)


#: One builder per native frame family; each takes the session and, optionally, the rows.
FAMILIES: dict[str, Callable[..., View]] = {
    "register": register_view,
    "spine": spine_view,
    "home": home_view,
    "read_model": read_model_view,
}


class _Host:
    """The dispatcher's host: a held clock and a quit nothing asks for."""

    def __init__(self) -> None:
        self._clock = FakeClock()

    @property
    def clock(self) -> Clock:
        """Return the console clock."""
        return self._clock

    def quit(self) -> None:
        """Record nothing; no key in the journey quits."""


def _keys(view: View) -> list[str]:
    """Return the row keys the view's table holds, in drawing order."""
    model = view.register if view.register is not None else view.projection
    assert model is not None
    return [row.key for row in model.rows]


def window_of(frame: list[str]) -> tuple[int, int, int]:
    """Return the ``(first, last, total)`` the frame's ``WINDOW`` line states, 1-based."""
    for row in frame:
        found = _WINDOW.match(row)
        if found:
            a, b = found.group("a"), found.group("b")
            total = int(found.group("n").replace(",", ""))
            if a is None or b is None:
                return (0, 0, total)
            return (int(a.replace(",", "")), int(b.replace(",", "")), total)
    raise AssertionError("the frame states no WINDOW line")


def assert_cursor_visible(view: View, frame: list[str]) -> None:
    """Fail unless the selected row is drawn with the caret above the keybar."""
    key = _keys(view)[view.session.sel]
    body = frame[:-1]
    assert any(row.startswith(_CARET + key) for row in body), f"the caret on {key} is off screen"


def assert_window_holds_cursor(view: View, frame: list[str]) -> None:
    """Fail unless the ``WINDOW`` line's range holds the selected row."""
    first, last, _total = window_of(frame)
    assert first <= view.session.sel + 1 <= last


def press(view: View, key: str) -> list[str]:
    """Dispatch ``key`` against the rendered view, then return the next frame.

    The context carries the held read model the way the console app builds it, so a
    route hook sees the same native mode the frame was drawn in.
    """
    ctx = Ctx(
        session=view.session,
        fixture=view.fixture,
        host=_Host(),
        w=view.w,
        h=view.h,
        projection=view.projection,
    )
    dispatch(ctx, key, False)
    return render_route(view)


def journey(view: View) -> list[list[str]]:
    """Walk ``PRESSES`` rows down, asserting the caret is on screen after every press."""
    frames = [render_route(view)]
    assert_cursor_visible(view, frames[0])
    for _ in range(PRESSES):
        frames.append(press(view, "ArrowDown"))
        assert_cursor_visible(view, frames[-1])
    return frames


# ---------- the journey ----------


@pytest.mark.parametrize("family", FAMILIES)
def test_native_activity_keeps_cursor_visible_at_1490_rows(family: str) -> None:
    """Fifty presses down a 1,490-row table leave the caret on screen after every one."""
    view = FAMILIES[family](Session())
    frames = journey(view)
    assert view.session.sel == PRESSES
    assert_window_holds_cursor(view, frames[-1])
    first, last, total = window_of(frames[-1])
    assert total == ROWS
    assert last == PRESSES + 1
    assert last - first + 1 == view.session.visible
    assert "of 1,490" in next(row for row in frames[-1] if row.startswith(" WINDOW"))


@pytest.mark.parametrize("family", FAMILIES)
def test_page_down_moves_the_window_a_screen(family: str) -> None:
    """PageDown moves the cursor and the window by the rows the frame shows."""
    view = FAMILIES[family](Session())
    render_route(view)
    before = window_of(render_route(view))
    frame = press(view, "PageDown")
    assert_cursor_visible(view, frame)
    assert_window_holds_cursor(view, frame)
    first, _last, _total = window_of(frame)
    assert view.session.sel == view.session.visible
    assert first > before[0]


@pytest.mark.parametrize("family", FAMILIES)
def test_end_and_home_reach_the_ends_of_the_table(family: str) -> None:
    """End lands on the last row with the window at the table's foot; Home comes back."""
    view = FAMILIES[family](Session())
    render_route(view)
    frame = press(view, "End")
    assert view.session.sel == ROWS - 1
    assert_cursor_visible(view, frame)
    assert_window_holds_cursor(view, frame)
    first, last, total = window_of(frame)
    assert (last, total) == (ROWS, ROWS)
    assert first == ROWS - view.session.visible + 1

    frame = press(view, "Home")
    assert view.session.sel == 0
    assert_cursor_visible(view, frame)
    assert_window_holds_cursor(view, frame)
    assert window_of(frame)[:2] == (1, view.session.visible)


@pytest.mark.parametrize("family", FAMILIES)
def test_page_up_at_the_top_stays_on_the_first_row(family: str) -> None:
    """PageUp never pages past the first row."""
    view = FAMILIES[family](Session())
    render_route(view)
    frame = press(view, "PageUp")
    assert view.session.sel == 0
    assert window_of(frame)[0] == 1


@pytest.mark.parametrize("family", FAMILIES)
def test_the_window_moves_only_when_the_cursor_would_leave_it(family: str) -> None:
    """A press inside the window keeps it where it is."""
    view = FAMILIES[family](Session())
    render_route(view)
    press(view, "End")
    frame = press(view, "ArrowUp")
    first, last, _total = window_of(frame)
    assert (first, last) == (ROWS - view.session.visible + 1, ROWS)


@pytest.mark.parametrize("family", FAMILIES)
def test_a_frame_stays_inside_its_height(family: str) -> None:
    """The windowed frame is exactly H rows, keybar last, however long the table."""
    view = FAMILIES[family](Session())
    frame = render_route(view)
    assert len(frame) == H


@pytest.mark.parametrize("family", FAMILIES)
def test_a_short_table_fits_whole(family: str) -> None:
    """A table shorter than the window is drawn whole and the window says so."""
    frame = render_route(FAMILIES[family](Session(), 3))
    assert window_of(frame) == (1, 3, 3)


@pytest.mark.parametrize("family", FAMILIES)
def test_an_empty_table_states_a_zero_window(family: str) -> None:
    """A table with no rows says so, and the window names nothing on screen."""
    frame = render_route(FAMILIES[family](Session(), 0))
    assert window_of(frame) == (0, 0, 0)
    assert any("holds no record" in row for row in frame)


# ---------- the keybar ----------


@pytest.mark.parametrize("family", FAMILIES)
def test_the_native_keybar_advertises_paging_by_full_key_names(family: str) -> None:
    """The bar spells the arrows as glyphs and the paging keys in full."""
    view = FAMILIES[family](Session())
    bar = render_route(view)[-1]
    assert "PageUp PageDown page" in bar
    assert "Home End ends" in bar
    assert "PgUp" not in bar
    assert "PgDn" not in bar


def test_native_keys_follow_the_arrow_entry() -> None:
    """Paging sits right after the row keys, route verbs after it."""
    keys = native_keys("crash.recovery")
    assert keys[:3] == (KEY["up"], KEY["page"], KEY["ends"])
    assert keys[3:] == ROUTE_KEYS["crash.recovery"][1:]


def test_native_keys_do_not_repeat_an_entry_the_route_already_has() -> None:
    """Activity already pages, so only the ends entry is added."""
    keys = native_keys("activity")
    assert keys.count(KEY["page"]) == 1
    assert keys[:3] == (KEY["up"], KEY["page"], KEY["ends"])


def test_native_keys_lead_with_paging_on_a_route_without_arrows() -> None:
    """A route whose table has no arrow entry still pages, from the front of its bar."""
    keys = native_keys("receipt")
    assert keys[:2] == (KEY["page"], KEY["ends"])
    assert keys[2:] == ROUTE_KEYS["receipt"]


def test_native_keys_refuse_an_unknown_route() -> None:
    """A route with no key table has no native keybar either."""
    with pytest.raises(KeyError):
        native_keys("no.such.route")


# ---------- the window arithmetic ----------


def _bare(h: int, *, scroll: int = 0, reserved: int = 0) -> View:
    """Return a view whose only use is the window arithmetic."""
    session = Session()
    session.scroll, session.reserved = scroll, reserved
    return View(session=session, fixture=_fixture(), w=W, h=h)


def test_window_rows_empty_table() -> None:
    """No rows: an empty window, and a line that names nothing on screen."""
    win = window_rows(_bare(20), total=0, cursor=0, chrome=5)
    assert (win.start, win.stop) == (0, 0)
    assert win.line() == " WINDOW    0 of 0"


def test_window_rows_single_row() -> None:
    """One row is drawn whole."""
    win = window_rows(_bare(20), total=1, cursor=0, chrome=5)
    assert (win.start, win.stop) == (0, 1)


def test_window_rows_exactly_fits() -> None:
    """A table exactly the room tall never scrolls, even on its last row."""
    view = _bare(20)
    win = window_rows(view, total=14, cursor=13, chrome=5)
    assert (win.start, win.stop, view.session.visible) == (0, 14, 14)


def test_window_rows_one_row_over() -> None:
    """One row more than the room scrolls by exactly one on the last row."""
    win = window_rows(_bare(20), total=15, cursor=14, chrome=5)
    assert (win.start, win.stop) == (1, 15)


def test_window_rows_keeps_one_row_on_a_frame_with_no_room() -> None:
    """A frame whose chrome fills it still shows the cursor's row."""
    view = _bare(6)
    win = window_rows(view, total=100, cursor=40, chrome=10)
    assert (win.start, win.stop, view.session.visible) == (40, 41, 1)


def test_window_rows_leaves_the_rack_its_rows() -> None:
    """Rows the notification rack reserves come out of the window."""
    view = _bare(20, reserved=4)
    window_rows(view, total=100, cursor=0, chrome=5)
    assert view.session.visible == 10


def test_window_rows_clamps_a_stale_scroll() -> None:
    """A scroll past the table's foot is pulled back so the window is full."""
    win = window_rows(_bare(20, scroll=500), total=30, cursor=29, chrome=5)
    assert (win.start, win.stop) == (16, 30)


def test_row_window_line_groups_and_marks_a_partial_table() -> None:
    """Counts group by thousands; a table that is not complete says its total is known."""
    win = RowWindow(start=1440, stop=1490, total=1490)
    assert win.line() == " WINDOW    1,441–1,490 of 1,490"  # noqa: RUF001
    assert win.line(complete=False).endswith(" known")
