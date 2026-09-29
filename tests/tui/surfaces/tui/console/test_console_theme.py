"""The console opens on the operator's ``ui.theme`` and, under ``auto``, follows the system.

``auto`` reads the terminal background once, before the toolkit owns the terminal, and a
running console then takes the system's light or dark appearance whenever it changes.
An appearance that cannot be read leaves the theme alone.
"""

from __future__ import annotations

import asyncio

import pytest

from eawf.surfaces.tui.chassis.theme import EA_DARK, EA_LIGHT
from eawf.surfaces.tui.console import app as console_app
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.clock import FakeClock


def test_the_console_opens_dark_when_no_theme_is_named() -> None:
    app = ConsoleApp(clock=FakeClock())
    assert (app.theme, app.follows_system) == (EA_DARK.name, False)


def test_a_named_theme_opens_that_palette() -> None:
    assert ConsoleApp(clock=FakeClock(), theme="light").theme == EA_LIGHT.name


def test_an_unknown_theme_is_refused() -> None:
    with pytest.raises(ValueError, match="not a logical theme name"):
        ConsoleApp(clock=FakeClock(), theme="sepia")


def test_auto_opens_on_the_terminal_background(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(console_app, "detect_auto_theme", lambda: "light")
    app = ConsoleApp(clock=FakeClock(), theme="auto")
    assert (app.theme, app.follows_system) == (EA_LIGHT.name, True)


@pytest.mark.parametrize(
    ("read", "expected"),
    [("dark", EA_DARK.name), ("light", EA_LIGHT.name), (None, EA_LIGHT.name)],
)
def test_a_running_console_follows_the_system_appearance(
    monkeypatch: pytest.MonkeyPatch, read: str | None, expected: str
) -> None:
    monkeypatch.setattr(console_app, "detect_auto_theme", lambda: "light")
    monkeypatch.setattr(console_app, "detect_os_appearance", lambda: read)
    app = ConsoleApp(clock=FakeClock(), theme="auto")
    asyncio.run(app.follow_appearance())
    assert app.theme == expected


def test_the_light_theme_repaints_the_canvas() -> None:
    async def canvas(theme: str) -> str:
        app = ConsoleApp(clock=FakeClock(), theme=theme)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            return str(app.screen.styles.background)

    assert asyncio.run(canvas("light")) != asyncio.run(canvas("dark"))
