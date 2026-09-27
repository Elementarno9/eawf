"""CON-129: an overlay is a full frame, a drawer is foot-attached, and Escape closes the top.

An overlay names itself and its subject in its crumb, draws its own keybar and hides the
route beneath it. A drawer keeps the route's header and body, swaps the keybar for its own
footer keys and owns the keyboard while it is open: a key the footer does not name acts on
nothing, the route's own key hook included. Escape closes the drawer or the top overlay
before it does anything else. Both refuse through the one connection gate.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.keymap import DRAWER_PAIRS
from eawf.surfaces.tui.console.navigation import open_overlay
from eawf.surfaces.tui.console.reads import mut_reason
from eawf.surfaces.tui.console.registry import SURFACES
from eawf.surfaces.tui.console.session import SIZES
from eawf.surfaces.tui.console.tokens import RULE_HEAVY, RULE_THIN

from .overlay_support import Link, frame_of, press, prototype, session_on, surface_of

RUN = "RUN-538453eb"
SIZE_IDS = range(len(SIZES))
#: The overlays whose pack frame already names overlay and subject in the crumb form.
CRUMBED: tuple[tuple[str, str], ...] = (
    ("help", "activity"),
    ("question", "ACT-0032"),
    ("pause", "RUN-a708a7d6"),
    ("evidence", "EVD-0011"),
    ("acceptance", "MLS-0004"),
    ("readiness", "REL-0001"),
    ("resolution", "RUN-3c6ef367"),
    ("marker", "MLS-0001"),
)


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    """Return the prototype registers the golden contract replays."""
    return prototype()


def _content(rows: list[str]) -> set[str]:
    """Return the body rows that state something, rules and blanks aside."""
    return {row for row in rows[1:-1] if row.strip(f" {RULE_HEAVY}{RULE_THIN}")}


@pytest.mark.parametrize("size", SIZE_IDS)
@pytest.mark.parametrize(("name", "subject"), CRUMBED)
def test_con_129_an_overlay_names_itself_and_its_subject_in_its_crumb(
    name: str, subject: str, size: int, fixture: Fixture
) -> None:
    rows = frame_of(session_on("activity", overlay=name), fixture, size)
    assert rows[0].startswith(f" Eä ▸ {SURFACES[name].title} · {subject}")


@pytest.mark.parametrize("size", SIZE_IDS)
def test_con_129_an_overlay_hides_the_route_and_draws_its_own_keybar(
    size: int, fixture: Fixture
) -> None:
    route = frame_of(session_on("activity"), fixture, size)
    over = frame_of(session_on("activity", overlay="acceptance"), fixture, size)
    assert not _content(route) & _content(over)
    assert over[-1] != route[-1]
    assert over[-1] == keybar([("↑↓", "receipt"), ("y", "copy"), ("Esc", "back")], len(over[-1]))


@pytest.mark.parametrize("size", SIZE_IDS)
@pytest.mark.parametrize("drawer", ["actions", "inspect", "raw"])
def test_con_129_a_drawer_keeps_the_route_header_and_swaps_only_the_keybar(
    drawer: str, size: int, fixture: Fixture
) -> None:
    route = frame_of(session_on("run.detail", subj_id=RUN), fixture, size)
    rows = frame_of(session_on("run.detail", subj_id=RUN, overlay=drawer), fixture, size)
    assert rows[:3] == route[:3]
    assert rows[-1] == keybar(DRAWER_PAIRS[drawer], len(rows[-1]))
    assert len(rows) == SIZES[size][1]


@pytest.mark.parametrize("size", SIZE_IDS)
def test_con_129_the_go_drawer_footer_is_the_prefix_hint(size: int, fixture: Fixture) -> None:
    rows = frame_of(session_on("run.detail", subj_id=RUN, prefix="g"), fixture, size)
    assert rows[-1].split() == ["g", "…", "destination", "Esc", "cancel"]


def test_con_129_escape_closes_the_drawer_before_it_goes_back(fixture: Fixture) -> None:
    session = session_on("run.detail", subj_id=RUN)
    session.back.push(route="activity", sel=0, subj=None)
    press(session, fixture, "i")
    assert session.overlay == "inspect"
    press(session, fixture, "Escape")
    assert (surface_of(session), session.route, len(session.back)) == (None, "run.detail", 1)
    press(session, fixture, "Escape")
    assert session.route == "activity"


def test_con_129_escape_closes_the_top_overlay_only(fixture: Fixture) -> None:
    session = session_on("attention", overlay="question")
    press(session, fixture, "x")
    assert session.overlay == "consequence"
    press(session, fixture, "Escape")
    assert (surface_of(session), session.route) == (None, "attention")


@pytest.mark.parametrize("key", ["g", "?", "/", "i", "r", "Tab", "Y", "Enter", "ArrowDown"])
def test_con_129_the_action_drawer_owns_the_keyboard(key: str, fixture: Fixture) -> None:
    session = session_on("run.detail", subj_id=RUN)
    press(session, fixture, ".")
    assert session.overlay == "actions"
    press(session, fixture, key)
    assert (session.overlay, session.prefix, session.route) == ("actions", None, "run.detail")
    assert session.toasts == []


@pytest.mark.parametrize("drawer", ["inspect", "raw"])
@pytest.mark.parametrize("key", ["g", "?", "/", ".", "Enter", "Tab"])
def test_con_129_a_row_drawer_owns_the_keyboard(drawer: str, key: str, fixture: Fixture) -> None:
    session = session_on("run.detail", subj_id=RUN, overlay=drawer)
    press(session, fixture, key)
    assert (session.overlay, session.prefix, session.route) == (drawer, None, "run.detail")


def test_con_129_no_key_reaches_the_route_hook_under_a_drawer(fixture: Fixture) -> None:
    session = session_on("campaign.artifact", overlay="inspect")
    before = session.art_scroll
    press(session, fixture, "ArrowDown", "ArrowDown")
    assert (session.art_scroll, session.overlay) == (before, "inspect")


def test_con_129_the_action_drawer_refuses_through_the_connection_gate(fixture: Fixture) -> None:
    session, link = session_on("run.detail", subj_id=RUN, conn="OFFLINE SNAPSHOT"), Link()
    press(session, fixture, ".", link=link)
    press(session, fixture, "n", link=link)
    assert session.overlay == "actions"
    assert link.sent == []
    assert session.trace is not None
    assert mut_reason(session, fixture) in session.trace


def test_con_129_an_overlay_opened_over_a_drawer_replaces_it(fixture: Fixture) -> None:
    session = session_on("run.detail", subj_id=RUN, sel=1)
    open_overlay(session, "actions")
    open_overlay(session, "consequence", subject=RUN)
    press(session, fixture, "Escape")
    assert (session.overlay, session.sel) == (None, 1)


@pytest.mark.xfail(
    strict=True,
    reason="the golden contract records the consequence card crumbed without its subject and "
    "the draft card under the route crumb; the cards' rework owns the crumb form",
)
@pytest.mark.parametrize("name", ["consequence", "draft"])
def test_con_129_the_consequence_and_draft_cards_name_overlay_and_subject(
    name: str, fixture: Fixture
) -> None:
    rows = frame_of(session_on("attention", overlay=name), fixture)
    assert rows[0].startswith(f" Eä ▸ {SURFACES[name].title} · ")
