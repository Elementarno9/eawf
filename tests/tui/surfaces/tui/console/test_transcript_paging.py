"""Page Up, Page Down and the mouse wheel walk the transcript; the newest block follows.

A page moves the cursor by as many blocks as the last drawn window showed heads for, a
wheel notch by three, and both stop at the first and the newest block. Moving up from
the newest block holds the view and counts what arrives; any move that lands on the
newest block follows the tail again, as End does.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from textual.events import MouseScrollDown, MouseScrollUp

from eawf.surfaces.tui.console.app import Body, ConsoleApp
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.harness import settle
from eawf.surfaces.tui.console.keymap import MOTION, allowlist
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.renderers import render_route, transcript
from eawf.surfaces.tui.console.renderers.transcript import WHEEL_BLOCKS, follow_word
from eawf.surfaces.tui.console.session import Session
from tests.tui.surfaces.tui.console import test_transcript_blocks as tb
from tests.tui.surfaces.tui.console import test_transcript_live as tl

MODEL = tb._view(tuple(tb._event(n) if n % 2 else tb._summarized(n) for n in range(1, 61)))
NEWEST = len(MODEL.blocks) - 1


def _session(sel: int | None = None) -> Session:
    """Return a transcript session on block ``sel``, following when ``sel`` is ``None``."""
    session = Session()
    session.route = tb.TRANSCRIPT_ROUTE
    session.tr_sel = sel
    session.follow = sel is None
    return session


def _draw(session: Session, *, h: int = 30) -> list[str]:
    """Return the native frame drawn for ``session``, which publishes the window's page."""
    return render_route(View(session=session, fixture=tb._fixture(), w=120, h=h, projection=MODEL))


def _press(session: Session, key: str) -> bool:
    ctx = Ctx(
        session=session, fixture=tb._fixture(), host=tb._Host(), w=120, h=30, projection=MODEL
    )
    return transcript.seam(ctx, key, False)


def _heads(rows: list[str]) -> int:
    """Return how many block heads a drawn frame shows."""
    return sum(1 for row in rows if tb._BLOCK_ROW.match(row))


# ---- Page Up and Page Down: one window of blocks, clamped ------------------------------


def test_page_keys_move_by_the_block_heads_the_window_showed() -> None:
    session = _session(30)
    rows = _draw(session)
    shown = _heads(rows)
    assert shown > 1 and session.tr_page == shown
    assert _press(session, "PageUp")
    assert session.tr_sel == 30 - shown and session.follow is False
    assert _press(session, "PageDown")
    assert session.tr_sel == 30


def test_page_keys_count_heads_again_on_every_frame() -> None:
    """A shorter window pages by fewer blocks: the page is the last frame's, not a constant."""
    session = _session(30)
    _draw(session, h=30)
    tall = session.tr_page
    _draw(session, h=16)
    assert 1 <= session.tr_page < tall


@pytest.mark.parametrize(("start", "key", "end"), [(2, "PageUp", 0), (0, "PageUp", 0)])
def test_page_keys_clamp_at_the_first_block(start: int, key: str, end: int) -> None:
    session = _session(start)
    _draw(session)
    _press(session, key)
    assert session.tr_sel == end


@pytest.mark.parametrize("start", [NEWEST - 1, NEWEST - 2])
def test_page_keys_clamp_at_the_newest_block(start: int) -> None:
    session = _session(start)
    _draw(session)
    _press(session, "PageDown")
    assert session.tr_sel == NEWEST


@pytest.mark.parametrize("page", [0, -3])
def test_page_keys_move_at_least_one_block(page: int) -> None:
    """A window that showed no head still pages by one block rather than standing still."""
    session = _session(10)
    session.tr_page = page
    _press(session, "PageUp")
    assert session.tr_sel == 9


def test_page_keys_on_an_empty_run_stay_on_the_first_block() -> None:
    session = _session()
    ctx = Ctx(
        session=session, fixture=tb._fixture(), host=tb._Host(), w=120, h=30, projection=tb._view()
    )
    assert transcript.seam(ctx, "PageDown", False)
    assert session.tr_sel == 0 and session.follow is True


def test_page_keys_are_listed_after_the_block_pair() -> None:
    bar = _draw(_session(30))[-1].split()
    assert bar[:5] == ["↑↓", "block", "PageUp", "PageDown", "page"]


def test_page_keys_leave_a_key_the_seam_does_not_bind() -> None:
    session = _session(30)
    assert _press(session, "Home") is False
    assert session.tr_sel == 30


# ---- the mouse wheel: three blocks a notch, from the app through press_key -------------


def test_wheel_names_pass_the_refusal_gate() -> None:
    assert {"WheelUp", "WheelDown"} <= MOTION
    session = _session(30)
    assert {"WheelUp", "WheelDown"} <= allowlist(session, tb._fixture())


@pytest.mark.parametrize(("start", "key", "end"), [(30, "WheelUp", 27), (30, "WheelDown", 33)])
def test_wheel_notch_moves_three_blocks(start: int, key: str, end: int) -> None:
    assert WHEEL_BLOCKS == 3
    session = _session(start)
    assert _press(session, key)
    assert session.tr_sel == end


@pytest.mark.parametrize(
    ("start", "key", "end"), [(1, "WheelUp", 0), (NEWEST - 1, "WheelDown", NEWEST)]
)
def test_wheel_notch_clamps_at_either_end(start: int, key: str, end: int) -> None:
    session = _session(start)
    _press(session, key)
    assert session.tr_sel == end


def test_wheel_notch_at_an_edge_logs_nothing() -> None:
    """A notch that moves nothing leaves no log line, so no toast answers every notch."""
    session = _session()
    _draw(session)
    log = len(session.log)
    assert _press(session, "WheelDown")
    assert len(session.log) == log and session.follow is True


def _wheel(app: ConsoleApp, up: bool) -> None:
    """Post one wheel notch to the body under the pointer, as the terminal would."""
    body = app.query_one(Body)
    kind = MouseScrollUp if up else MouseScrollDown
    body.post_message(kind(body, 1, 1, 0, -1 if up else 1, 0, False, False, False))


def test_wheel_from_the_app_reaches_the_transcript_seam() -> None:
    daemon = tl._CountingDaemon()

    async def scenario(app: ConsoleApp, pilot: Any) -> None:
        await tl._open_followed(app, pilot)
        s = app.session
        assert (s.tr_sel, s.follow) == (2, True)
        _wheel(app, up=True)
        await settle(pilot)
        assert (s.tr_sel, s.follow) == (0, False)
        assert s.log[0].key == "WheelUp"
        assert "· held · 0 new" in tl._strip(app)
        _wheel(app, up=False)
        await settle(pilot)
        assert (s.tr_sel, s.follow) == (2, True)
        assert s.log[0].key == "WheelDown"
        assert "· following" in tl._strip(app)

    tl._drive(daemon, scenario)


# ---- following: up holds, landing on the newest block follows again -------------------


@pytest.mark.parametrize("key", ["ArrowUp", "PageUp", "WheelUp"])
def test_follow_resumes_only_after_a_move_up_holds(key: str) -> None:
    session = _session()
    _draw(session)
    _press(session, key)
    assert session.follow is False and session.tr_sel < NEWEST
    assert follow_word(session, len(MODEL.blocks)) == "held · 0 new"
    assert follow_word(session, len(MODEL.blocks) + 4) == "held · 4 new"


@pytest.mark.parametrize(
    ("start", "key"),
    [(NEWEST - 1, "ArrowDown"), (NEWEST - 2, "PageDown"), (NEWEST - 3, "WheelDown")],
)
def test_follow_resumes_when_a_move_lands_on_the_newest_block(start: int, key: str) -> None:
    session = _session(start)
    session.tr_held_from = len(MODEL.blocks) - 2
    _draw(session)
    assert _press(session, key)
    assert (session.tr_sel, session.follow, session.tr_held_from) == (NEWEST, True, None)
    assert re.search(r"· following ·", _draw(session)[1])


def test_follow_resumes_and_keeps_the_newest_block_as_blocks_arrive() -> None:
    session = _session(NEWEST - 1)
    _draw(session)
    _press(session, "ArrowDown")
    grown = tb._view(tuple(tb._event(n) if n % 2 else tb._summarized(n) for n in range(1, 65)))
    render_route(View(session=session, fixture=tb._fixture(), w=120, h=30, projection=grown))
    assert session.tr_sel == len(grown.blocks) - 1 and session.follow is True


def test_follow_resumes_not_on_a_move_short_of_the_newest_block() -> None:
    session = _session(NEWEST - 3)
    _draw(session)
    _press(session, "ArrowDown")
    assert (session.tr_sel, session.follow) == (NEWEST - 2, False)
