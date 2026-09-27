"""CON-141: the rack clear passes every gate, and an overlay's keys match the cursor it draws.

``-`` is the console's: it clears the rack under every overlay and drawer, leaving the
surface beneath exactly as it was. An overlay that draws no cursor advertises no arrow and
no ``Enter`` and swallows the arrows; an overlay that draws one advertises the arrows it
binds, moves that cursor with them and names the row it is on in its foot. The four
decision overlays step through their state models on ``s``.
"""

from __future__ import annotations

import re

import pytest

from eawf.surfaces.tui.console.clock import FakeClock, notify
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.navigation import open_overlay
from eawf.surfaces.tui.console.overlays.chassis import cursor_foot
from eawf.surfaces.tui.console.overlays.states import OV_MODEL, step_state
from eawf.surfaces.tui.console.registry import DRAWERS, OVERLAY_ARROWS, OVERLAYS, SURFACES
from eawf.surfaces.tui.console.session import SIZES, Session
from eawf.surfaces.tui.console.tokens import Severity

from .overlay_support import chrome, frame_of, press, prototype, session_on

#: The overlays CON-141 names cursorless, whatever their option rows.
CURSORLESS: tuple[str, ...] = ("help", "resolution", "marker", "question", "pause")
#: The route each cursorless overlay is opened from.
OPENED_FROM: dict[str, str] = {
    "help": "activity",
    "resolution": "history",
    "marker": "timeline",
    "question": "attention",
    "pause": "attention",
}
#: The overlays CON-141 names as drawing a cursor, each with its route and its foot label.
CURSORED: tuple[tuple[str, str, str], ...] = (
    ("evidence", "campaign", "RECEIPT"),
    ("acceptance", "milestone", "RECEIPT"),
    ("readiness", "release", "SIGNAL"),
    ("draft", "backlog", "FIELD"),
)
_FOOT = re.compile(r"^ (?P<label>[A-Z]+)\s+(?P<n>\d+) of (?P<total>\d+)\s*$")


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    """Return the prototype registers the golden contract replays."""
    return prototype()


def _foot(rows: list[str]) -> tuple[str, int, int]:
    """Return the label, the row and the total a cursor overlay's foot names."""
    found = [m for m in (_FOOT.match(row) for row in rows[1:-1]) if m is not None]
    assert len(found) == 1, "a cursor overlay names its row exactly once"
    return found[0]["label"], int(found[0]["n"]), int(found[0]["total"])


def _with_rack(session: Session) -> Session:
    notify(session, FakeClock(), text="copied", title="copied", sev=Severity.OK)
    return session


def test_con_141_the_registry_declares_a_cursor_per_entry() -> None:
    assert {name for name, _route, _label in CURSORED} == set(OVERLAY_ARROWS)
    for name in CURSORLESS:
        assert not SURFACES[name].cursor


@pytest.mark.parametrize("size", range(len(SIZES)))
@pytest.mark.parametrize("name", CURSORLESS)
def test_con_141_a_cursorless_overlay_advertises_no_arrow_and_no_enter(
    name: str, size: int, fixture: Fixture
) -> None:
    bar = frame_of(session_on("attention", overlay=name), fixture, size)[-1]
    assert "↑" not in bar
    assert "↓" not in bar
    assert "Enter" not in bar


@pytest.mark.parametrize("name", CURSORLESS)
@pytest.mark.parametrize("key", ["ArrowDown", "ArrowUp", "j", "k"])
def test_con_141_a_cursorless_overlay_swallows_the_arrows(
    name: str, key: str, fixture: Fixture
) -> None:
    session = session_on(OPENED_FROM[name], overlay=name, sel=1)
    before = frame_of(session, fixture)
    sel = session.sel
    press(session, fixture, key)
    assert (session.overlay, session.sel) == (name, sel)
    assert frame_of(session, fixture) == before


@pytest.mark.parametrize("size", range(len(SIZES)))
@pytest.mark.parametrize(("name", "route", "label"), CURSORED)
def test_con_141_a_cursor_overlay_advertises_its_arrows_and_names_its_row(
    name: str, route: str, label: str, size: int, fixture: Fixture
) -> None:
    rows = frame_of(session_on(route, overlay=name), fixture, size)
    assert "↑↓" in rows[-1]
    found, n, total = _foot(rows)
    assert (found, n) == (label, 1)
    assert total > 1


@pytest.mark.parametrize(("name", "route", "label"), CURSORED)
def test_con_141_every_arrow_moves_the_cursor_the_foot_names(
    name: str, route: str, label: str, fixture: Fixture
) -> None:
    session = session_on(route, overlay=name)
    total = _foot(frame_of(session, fixture))[2]
    press(session, fixture, "ArrowDown")
    assert _foot(frame_of(session, fixture)) == (label, 2, total)
    press(session, fixture, *["ArrowDown"] * (total + 2))
    assert _foot(frame_of(session, fixture)) == (label, total, total)
    press(session, fixture, "ArrowUp")
    assert _foot(frame_of(session, fixture)) == (label, total - 1, total)


def test_con_141_the_foot_is_one_labelled_row() -> None:
    assert cursor_foot("RECEIPT", 1, 3) == " RECEIPT   1 of 3"
    assert cursor_foot("", 0, 0) == "           0 of 0"


@pytest.mark.parametrize("name", [n for n in OVERLAYS if n != "palette"])
def test_con_141_the_rack_clear_acts_under_every_overlay(name: str, fixture: Fixture) -> None:
    session = _with_rack(session_on("attention", sel=1))
    open_overlay(session, name, subject="ACT-0031")
    press(session, fixture, "-")
    assert session.toasts == []
    assert (session.overlay, session.sel) == (name, 1)


@pytest.mark.parametrize("drawer", [d for d in DRAWERS if d != "go"])
def test_con_141_the_rack_clear_acts_under_every_drawer(drawer: str, fixture: Fixture) -> None:
    session = _with_rack(session_on("run.detail", subj_id="RUN-538453eb", overlay=drawer))
    press(session, fixture, "-")
    assert (session.toasts, session.overlay) == ([], drawer)


def test_con_141_the_rack_clear_keeps_the_go_prefix_armed(fixture: Fixture) -> None:
    session = _with_rack(session_on("run.detail", subj_id="RUN-538453eb"))
    press(session, fixture, "g", "-")
    assert (session.toasts, session.prefix) == ([], "g")


def test_con_141_the_rack_clear_acts_under_an_overlay_holding_nothing() -> None:
    session = _with_rack(session_on("activity", overlay="question"))
    press(session, chrome(), "-")
    assert (session.toasts, session.overlay) == ([], "question")


def test_con_141_the_palette_query_takes_the_dash_that_ids_carry(fixture: Fixture) -> None:
    """The palette's query is a text field and an entity id carries ``-``, so it types it."""
    session = _with_rack(session_on("scope.home", overlay="palette"))
    press(session, fixture, "r", "u", "n", "-")
    assert session.pq == "run-"
    assert len(session.toasts) == 1


@pytest.mark.parametrize("name", sorted(OV_MODEL))
def test_con_141_a_decision_overlay_steps_through_its_state_model(
    name: str, fixture: Fixture
) -> None:
    session = session_on("attention", overlay=name)
    words = [step_state(session, name) for _ in OV_MODEL[name]]
    assert words[-1] == OV_MODEL[name][0][0]
    assert set(words) == {word for word, _ends in OV_MODEL[name]}


def test_con_141_stepping_an_overlay_with_no_state_model_is_nothing() -> None:
    assert step_state(session_on("activity"), "help") is None
