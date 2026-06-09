"""Pilot tests for the C06 ``RepoScreen`` (P26-W18).

Covers the 2x2 quadrant composition (RoadmapTree · StatusPane / GitPane ·
BacklogTable) inside the shared chassis, the D3 zero-duplication
invariant (the screen reuses :meth:`ScopeScreen.compose` rather than
re-declaring the Header/Footer chrome), live state binding into the
composed widgets, and a Pilot-driven first paint under the real palette.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.scopes import RepoScreen, ScopeScreen
from eawf.surfaces.tui.snapshot import capture_screen_text
from eawf.surfaces.tui.widgets.backlog_table import BacklogTable
from eawf.surfaces.tui.widgets.footer import Footer, Heartbeat
from eawf.surfaces.tui.widgets.git_pane import GitPane
from eawf.surfaces.tui.widgets.header import BRAND, Header
from eawf.surfaces.tui.widgets.roadmap_tree import RoadmapTree
from eawf.surfaces.tui.widgets.status_pane import StatusPane

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"


# --------------------------------------------------------------------------
# D3 shared chassis — no per-scope chrome duplication
# --------------------------------------------------------------------------


def test_repo_screen_reuses_shared_chassis_compose() -> None:
    # D3: RepoScreen does not override compose / on_mount — the
    # Header + Footer chrome comes from the shared ScopeScreen base.
    assert RepoScreen.compose is ScopeScreen.compose
    assert "on_mount" not in RepoScreen.__dict__
    assert RepoScreen.compose_body is not ScopeScreen.compose_body


# --------------------------------------------------------------------------
# Composition — chassis + the 4 quadrant widgets present
# --------------------------------------------------------------------------


def test_repo_screen_composes_chassis_and_quadrant() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, RepoScreen)
            # Shared chassis.
            assert screen.query(Header)
            assert screen.query(Footer)
            assert screen.query(Heartbeat)
            # 2x2 quadrant widgets.
            assert screen.query_one(RoadmapTree)
            assert screen.query_one(StatusPane)
            assert screen.query_one(GitPane)
            assert screen.query_one(BacklogTable)

    asyncio.run(body())


def test_repo_screen_quadrant_pane_titles_present() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            rendered = app.export_screenshot()
            for title in ("ROADMAP", "STATUS", "GIT", "BACKLOG"):
                assert title in rendered

    asyncio.run(body())


# --------------------------------------------------------------------------
# Live state binding — composed widgets receive the bound state
# --------------------------------------------------------------------------


def test_repo_screen_first_paint_renders_brand_and_data() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            # Plain-text capture: the SVG export splits the two-tone
            # wordmark across text spans, so the contiguous brand pair
            # only survives in the text capture.
            rendered = capture_screen_text(app)
            assert BRAND in rendered
            # Status pane + roadmap tree both surface the project code.
            assert "QR" in rendered

    asyncio.run(body())


def test_repo_screen_roadmap_tree_binds_state() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            tree = app.screen.query_one(RoadmapTree)
            # The tree seeded from the app's reactive state on mount.
            assert tree.state is not None
            assert tree.state.scope_kind.value == "repo"

    asyncio.run(body())
