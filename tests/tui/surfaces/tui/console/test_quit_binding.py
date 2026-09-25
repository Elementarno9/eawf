"""Ctrl+C's guarded quit: the SURF binding's gate-fire proof, plus its help-card listing.

Before W30, Ctrl+C reached :meth:`ConsoleApp.on_key` and was silently swallowed by
``event.prevent_default()`` -- the pre-existing defect the P34 brief's F4 names. Ctrl+C
now runs the same double-press guard Esc Esc uses at scope home (:mod:`.clock`), honoured
on every route, overlay and drawer since it is handled ahead of the route dispatcher
rather than through it.
"""

from __future__ import annotations

import asyncio

from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.clock import QUIT_CEILING, QUIT_FLOOR, FakeClock
from eawf.surfaces.tui.console.keymap import GLOBAL_HELP
from eawf.surfaces.tui.console.session import SessionSetup


def test_global_help_lists_ctrl_c() -> None:
    """The help card's EVERYWHERE block advertises the binding (D-TUI-7)."""
    rows = [(token, key) for token, _text, key in GLOBAL_HELP]
    assert ("Ctrl+C", "ctrl+c") in rows


async def _press_ctrl_c_twice(clock: FakeClock, *, gap: float, setup: SessionSetup) -> ConsoleApp:
    app = ConsoleApp(clock=clock)
    async with app.run_test(size=(80, 24)) as pilot:
        app.reset(setup)
        await pilot.press("ctrl+c")
        clock.advance(gap)
        await pilot.press("ctrl+c")
        await pilot.pause()
    return app


def test_ctrl_c_arms_the_guard_on_the_first_press() -> None:
    clock = FakeClock()

    async def drive() -> str | None:
        app = ConsoleApp(clock=clock)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+c")
            await pilot.pause()
            return app.session.trace

    trace = asyncio.run(drive())
    assert trace == "Ctrl+C → press again within 1.5s to quit"


def test_ctrl_c_quits_on_a_second_press_inside_the_guard_window() -> None:
    """Gate-fire proof: reverting the W30 Ctrl+C binding reds this (app stays running)."""
    clock = FakeClock()
    app = asyncio.run(
        _press_ctrl_c_twice(clock, gap=(QUIT_FLOOR + QUIT_CEILING) / 2, setup=SessionSetup())
    )
    assert app.return_code == 0


def test_ctrl_c_too_fast_to_be_two_presses_stays_armed() -> None:
    """A second press inside the burst floor does not count as the confirming one."""
    clock = FakeClock()
    app = asyncio.run(_press_ctrl_c_twice(clock, gap=QUIT_FLOOR / 2, setup=SessionSetup()))
    assert app.return_code is None
    assert app.session.trace == "Ctrl+C → too fast to be two presses - still armed"


def test_ctrl_c_past_the_ceiling_rearms_instead_of_quitting() -> None:
    clock = FakeClock()
    app = asyncio.run(_press_ctrl_c_twice(clock, gap=QUIT_CEILING + 1.0, setup=SessionSetup()))
    assert app.return_code is None
    assert app.session.trace == "Ctrl+C → press again within 1.5s to quit"


def test_ctrl_c_quits_from_a_route_where_escape_means_back() -> None:
    """The pre-W30 defect: Ctrl+C did nothing anywhere. It now quits off the home route."""
    clock = FakeClock()
    app = asyncio.run(
        _press_ctrl_c_twice(
            clock,
            gap=(QUIT_FLOOR + QUIT_CEILING) / 2,
            setup=SessionSetup(route="activity"),
        )
    )
    assert app.return_code == 0


def test_ctrl_c_quits_while_an_overlay_is_open() -> None:
    """The pre-W30 defect again: an open overlay used to own every key, Ctrl+C included."""
    clock = FakeClock()
    app = asyncio.run(
        _press_ctrl_c_twice(
            clock,
            gap=(QUIT_FLOOR + QUIT_CEILING) / 2,
            setup=SessionSetup(route="scope.home", overlay="help"),
        )
    )
    assert app.return_code == 0
