"""Pilot tests for the C06 ``UserScreen`` (P26-W18).

Covers the three-section composition (StatusPane attention · EUBar effort
· BacklogTable portfolio) inside the shared chassis, the D3
zero-duplication invariant, the weighted section titles, and a
Pilot-driven first paint. The user scope launches with no resolved
``state.json`` (``state_path=None`` per D10), so this also exercises the
None-state render path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from eawf.tui_v2.app import EaApp
from eawf.tui_v2.scopes import ScopeScreen, UserScreen
from eawf.tui_v2.widgets.backlog_table import BacklogTable
from eawf.tui_v2.widgets.eu_bar import EUBar
from eawf.tui_v2.widgets.footer import Footer, Heartbeat
from eawf.tui_v2.widgets.header import BRAND, DEFAULT_PROJECT_CODE, Header
from eawf.tui_v2.widgets.status_pane import StatusPane

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PORTFOLIO = _FIXTURES / "07-decisions-and-backlog.json"


# --------------------------------------------------------------------------
# D3 shared chassis — no per-scope chrome duplication
# --------------------------------------------------------------------------


def test_user_screen_reuses_shared_chassis_compose() -> None:
    assert UserScreen.compose is ScopeScreen.compose
    assert UserScreen.compose_body is not ScopeScreen.compose_body


# --------------------------------------------------------------------------
# Composition — chassis + three sections
# --------------------------------------------------------------------------


def test_user_screen_composes_chassis_and_three_sections() -> None:
    async def body() -> None:
        app = EaApp(scope="user", state_path=None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, UserScreen)
            assert screen.query(Header)
            assert screen.query(Footer)
            assert screen.query(Heartbeat)
            # Three weighted sections.
            assert screen.query_one(StatusPane)
            assert screen.query_one(EUBar)
            assert screen.query_one(BacklogTable)

    asyncio.run(body())


def test_user_screen_section_titles_present() -> None:
    async def body() -> None:
        app = EaApp(scope="user", state_path=None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            rendered = app.export_screenshot()
            assert "ATTENTION" in rendered
            assert "EFFORT" in rendered
            assert "PORTFOLIO" in rendered

    asyncio.run(body())


# --------------------------------------------------------------------------
# First paint — None-state (user scope) + populated portfolio fixture
# --------------------------------------------------------------------------


def test_user_screen_none_state_first_paint_renders_brand() -> None:
    async def body() -> None:
        app = EaApp(scope="user", state_path=None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            rendered = app.export_screenshot()
            assert BRAND in rendered
            # No resolved state => default-code breadcrumb.
            assert DEFAULT_PROJECT_CODE in rendered

    asyncio.run(body())


def test_user_screen_portfolio_table_binds_state() -> None:
    async def body() -> None:
        app = EaApp(scope="user", state_path=_PORTFOLIO)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(BacklogTable)
            assert table.state is not None

    asyncio.run(body())
