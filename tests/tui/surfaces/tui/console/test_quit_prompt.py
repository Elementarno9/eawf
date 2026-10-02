"""The guarded quit speaks through one toast: ``Press again to exit.`` for the quit window.

The first eligible Escape (or Ctrl+C) arms the guard and raises the prompt, which stands
exactly as long as a second press would still quit. No other guard step raises a toast: a
key that comes between the presses withdraws the prompt and says nothing on the rack, and
a burst press adds nothing. The key log keeps every step, which the verbose row prints.
"""

from __future__ import annotations

import asyncio

import pytest

from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.clock import (
    QUIT_CEILING,
    QUIT_FLOOR,
    QUIT_PROMPT,
    TOAST_DWELL,
    FakeClock,
    sweep_toasts,
)
from eawf.surfaces.tui.console.drill import say_why
from eawf.surfaces.tui.console.frame import toast_box
from eawf.surfaces.tui.console.session import SessionSetup, Toast
from eawf.surfaces.tui.console.tokens import Severity
from tests.tui.surfaces.tui.console.test_guarded_quit import _home, _Host, _press
from tests.tui.surfaces.tui.console.test_native_navigation import _ctx, _milestone


def test_the_first_escape_at_home_raises_exactly_the_quit_prompt() -> None:
    session, host = _home(), _Host()
    _press(session, host, "Escape")
    assert [(t.title, t.text, t.dwell) for t in session.toasts] == [("", QUIT_PROMPT, QUIT_CEILING)]


def test_the_prompt_expires_with_the_quit_window() -> None:
    session, host = _home(), _Host()
    _press(session, host, "Escape")
    host.clock.advance(QUIT_CEILING - QUIT_FLOOR)
    assert not sweep_toasts(session, host.clock)
    host.clock.advance(QUIT_FLOOR)
    assert [t.text for t in sweep_toasts(session, host.clock)] == [QUIT_PROMPT]
    assert session.toasts == []


def test_a_key_between_the_presses_withdraws_the_prompt() -> None:
    session, host = _home(), _Host()
    _press(session, host, "Escape")
    _press(session, host, "ArrowDown")
    assert session.last_esc == pytest.approx(0.0)
    assert QUIT_PROMPT not in [t.text for t in session.toasts]


def test_a_burst_press_adds_no_second_toast() -> None:
    session, host = _home(), _Host()
    _press(session, host, "Escape")
    host.clock.advance(QUIT_FLOOR / 2)
    _press(session, host, "Escape")
    assert [t.text for t in session.toasts] == [QUIT_PROMPT]


def test_the_disarming_note_never_becomes_a_toast() -> None:
    """The live defect: ``d`` between the presses toasted ``quit disarmed · d came between``."""
    view = _milestone("MLS-0100")
    view.session.log_key("Esc", "quit disarmed · d came between the presses")
    assert not say_why(_ctx(view), "d", head=[], toasts=0, still=True)
    assert view.session.toasts == []


def test_ctrl_c_arms_with_the_same_prompt() -> None:
    async def drive() -> list[Toast]:
        app = ConsoleApp(clock=FakeClock())
        async with app.run_test(size=(80, 24)) as pilot:
            app.reset(SessionSetup())
            await pilot.press("ctrl+c")
            await pilot.pause()
            return list(app.session.toasts)

    toasts = asyncio.run(drive())
    assert [(t.text, t.dwell) for t in toasts] == [(QUIT_PROMPT, QUIT_CEILING)]


def test_a_toast_keeps_the_rack_dwell_unless_it_names_its_own() -> None:
    assert Toast(title="copied", text="RUN-1", sev=Severity.OK, at=0.0).dwell == TOAST_DWELL


def test_an_untitled_toast_draws_an_unbroken_top_border() -> None:
    box = toast_box(Toast(title="", text=QUIT_PROMPT, sev=Severity.INFO, at=0.0), 28)
    assert box[0] == "┌" + "─" * 26 + "┐"
    assert box[1] == "│ " + QUIT_PROMPT.ljust(24) + " │"
