"""Ctrl+C's and ``q``'s guarded quit: the gate-fire proofs, the help card and the keybar.

On a console over the packaged chrome ``q`` steps the same guard as Ctrl+C, and every
route bar ends with ``q quit`` and ``? help``; the prototype replay binds no ``q``.

Before W30, Ctrl+C reached :meth:`ConsoleApp.on_key` and was silently swallowed by
``event.prevent_default()`` -- the pre-existing defect the P34 brief's F4 names. Ctrl+C
now runs the same double-press guard Esc Esc uses at scope home (:mod:`.clock`), honoured
on every route, overlay and drawer since it is handled ahead of the route dispatcher
rather than through it.
"""

from __future__ import annotations

import asyncio

import pytest

from eawf.kernel.projection.spine import NATIVE_ROUTES
from eawf.surfaces.tui.console.app import ConsoleApp, compose_frame
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import QUIT_CEILING, QUIT_FLOOR, QUIT_PROMPT, FakeClock
from eawf.surfaces.tui.console.dispatch import quit_press
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.keymap import GLOBAL_KEYS
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import SIZES, Session, SessionSetup
from tests.tui.surfaces.tui.console.test_chrome_sweeps import GOLDEN_ROOT
from tests.tui.surfaces.tui.console.test_spine_frames import _model


def test_global_help_lists_ctrl_c() -> None:
    """The help card's EVERYWHERE block advertises the binding (D-TUI-7)."""
    rows = [(key.token, key.keys) for key in GLOBAL_KEYS]
    assert ("Ctrl+C", ("ctrl+c",)) in rows


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


# ---------- q opens the same guarded quit, and every route bar says so ----------


async def _press_keys(
    clock: FakeClock, keys: list[tuple[str, float]], setup: SessionSetup
) -> ConsoleApp:
    """Press each key after advancing the clock by its gap, on a console over the chrome."""
    app = ConsoleApp(clock=clock)
    async with app.run_test(size=(120, 30)) as pilot:
        app.reset(setup)
        for key, gap in keys:
            clock.advance(gap)
            await pilot.press(key)
        await pilot.pause()
    return app


_INSIDE = (QUIT_FLOOR + QUIT_CEILING) / 2


def _run(keys: list[tuple[str, float]], route: str = "scope.home") -> ConsoleApp:
    return asyncio.run(_press_keys(FakeClock(), keys, SessionSetup(route=route)))


def test_q_arms_the_guard_with_the_quit_prompt() -> None:
    app = _run([("q", 0.0)])
    assert app.return_code is None
    assert app.session.trace == "q → press again within 1.5s to quit"
    assert [t.text for t in app.session.toasts] == [QUIT_PROMPT]


def test_q_twice_inside_the_window_quits() -> None:
    assert _run([("q", 0.0), ("q", _INSIDE)]).return_code == 0


def test_q_quits_off_scope_home_too() -> None:
    assert _run([("q", 0.0), ("q", _INSIDE)], route="activity").return_code == 0


def test_q_too_fast_to_be_two_presses_stays_armed() -> None:
    app = _run([("q", 0.0), ("q", QUIT_FLOOR / 2)])
    assert app.return_code is None
    assert app.session.trace == "q → too fast to be two presses - still armed"


def test_q_past_the_ceiling_rearms_instead_of_quitting() -> None:
    app = _run([("q", 0.0), ("q", QUIT_CEILING + 1.0)])
    assert app.return_code is None
    assert app.session.trace == "q → press again within 1.5s to quit"


def test_q_and_ctrl_c_share_one_guard() -> None:
    assert _run([("q", 0.0), ("ctrl+c", _INSIDE)]).return_code == 0
    assert _run([("ctrl+c", 0.0), ("q", _INSIDE)]).return_code == 0


def test_q_typed_into_the_filter_is_a_letter_not_a_quit() -> None:
    app = _run([("backslash", 0.0), ("q", 0.0), ("q", _INSIDE)], route="activity")
    assert app.return_code is None
    assert "q" in app.session.filters.get("activity", "")


def test_quit_press_returns_whether_to_quit() -> None:
    clock = FakeClock()
    session = Session()
    assert quit_press(session, clock, "q") is False
    clock.advance(_INSIDE)
    assert quit_press(session, clock, "q") is True
    assert session.last_esc == 0


LINKED = Fixture.from_chrome(load_chrome())


@pytest.mark.parametrize(("w", "h"), SIZES)
@pytest.mark.parametrize("route", sorted(ROUTE_KEYS))
def test_every_route_bar_ends_with_quit_and_help(route: str, w: int, h: int) -> None:
    session = Session()
    session.route = route
    bar = compose_frame(View(session=session, fixture=LINKED, w=w, h=h))[-1]
    assert bar.rstrip().endswith("q quit   ? help"), bar


@pytest.mark.parametrize(("w", "h"), SIZES)
@pytest.mark.parametrize("route", sorted(set(NATIVE_ROUTES) - {"roadmap"}))
def test_every_native_route_bar_ends_with_quit_and_help(route: str, w: int, h: int) -> None:
    session = Session()
    session.route = route
    view = View(session=session, fixture=LINKED, w=w, h=h, projection=_model(route))
    assert render_route(view)[-1].rstrip().endswith("q quit   ? help")


def test_the_prototype_replay_draws_no_quit_pair() -> None:
    session = Session()
    session.route = "activity"
    fixture = load_fixture(GOLDEN_ROOT / "fixture")
    assert "q quit" not in compose_frame(View(session=session, fixture=fixture, w=160, h=40))[-1]


def test_a_bar_under_an_open_field_offers_no_quit() -> None:
    session = Session()
    session.route = "activity"
    session.typing = True
    assert "q quit" not in compose_frame(View(session=session, fixture=LINKED, w=160, h=40))[-1]


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
