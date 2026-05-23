"""Pilot tests for the C06 ``WorkspaceScreen`` (P26-W18).

Covers the strip + zoom composition (StatusPane strip over a RoadmapTree
· GitPane · BacklogTable zoom quadrant) inside the shared chassis, the D3
zero-duplication invariant, the scope-specific footer hints, and a
Pilot-driven first paint under the real palette against a workspace
fixture.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from eawf.tui.app import EaApp
from eawf.tui.scopes import ScopeScreen, WorkspaceScreen
from eawf.tui.screens.overlays.config_modal import ConfigModal
from eawf.tui.widgets.backlog_table import BacklogTable
from eawf.tui.widgets.footer import Footer, Heartbeat
from eawf.tui.widgets.git_pane import GitPane
from eawf.tui.widgets.header import BRAND, Header
from eawf.tui.widgets.roadmap_tree import RoadmapTree
from eawf.tui.widgets.status_pane import StatusPane

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_WORKSPACE = _FIXTURES / "05-workspace-state.json"


# --------------------------------------------------------------------------
# D3 shared chassis — no per-scope chrome duplication
# --------------------------------------------------------------------------


def test_workspace_screen_reuses_shared_chassis_compose() -> None:
    assert WorkspaceScreen.compose is ScopeScreen.compose
    assert WorkspaceScreen.compose_body is not ScopeScreen.compose_body


# --------------------------------------------------------------------------
# Composition — chassis + strip + zoom quadrant widgets
# --------------------------------------------------------------------------


def test_workspace_screen_composes_chassis_and_strip_zoom() -> None:
    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, WorkspaceScreen)
            assert screen.query(Header)
            assert screen.query(Footer)
            assert screen.query(Heartbeat)
            # Strip (StatusPane) + zoom quadrant widgets.
            assert screen.query_one(StatusPane)
            assert screen.query_one(RoadmapTree)
            assert screen.query_one(GitPane)
            assert screen.query_one(BacklogTable)

    asyncio.run(body())


def test_workspace_screen_strip_and_pane_titles_present() -> None:
    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            rendered = app.export_screenshot()
            assert "WORKSPACE" in rendered
            for title in ("ROADMAP", "GIT", "BACKLOG"):
                assert title in rendered

    asyncio.run(body())


# --------------------------------------------------------------------------
# Footer hints + first paint
# --------------------------------------------------------------------------


def test_workspace_screen_footer_hints_applied() -> None:
    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            footer = app.screen.query_one(Footer)
            assert footer.hints == WorkspaceScreen.FOOTER_HINTS
            # Scope-specific "zoom" hint reaches the strip.
            assert "zoom" in app.export_screenshot()

    asyncio.run(body())


# --------------------------------------------------------------------------
# W14 — config opens from the workspace scope (c binding + footer advert)
# --------------------------------------------------------------------------


def test_workspace_screen_advertises_config_hint() -> None:
    assert "c config" in WorkspaceScreen.FOOTER_HINTS


def test_workspace_screen_binds_c_to_open_config() -> None:
    actions = {binding.action for binding in WorkspaceScreen.BINDINGS}
    assert "open_config" in actions
    # The pre-existing zoom binding is untouched.
    assert "zoom_focused" in actions


def test_workspace_c_keypress_opens_config_modal() -> None:
    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigModal)

    asyncio.run(body())


def test_workspace_screen_first_paint_renders_brand() -> None:
    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            rendered = app.export_screenshot()
            assert BRAND in rendered
            assert "workspace" in rendered

    asyncio.run(body())
