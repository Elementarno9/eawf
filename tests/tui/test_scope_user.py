"""Pilot tests for the C06 ``UserScreen`` portfolio table (P27-I04-W07).

Covers the full-screen per-repo :class:`PortfolioTable` (the reused W06
workspace-table family) — >=1 row even at N=1, the large-N scroll without
breaking column widths, the Enter → repo-detail overlay (the user scope
has **no** zoom quadrant), the ``↑↓`` focus movement, the ``z`` no-op, the
D3 zero-duplication invariant, the scope-specific footer hints, the empty
registry boundary, and the ``c`` config binding.

Determinism: every test that triggers a git probe awaits
``app.workers.wait_for_complete()`` (per the project Pilot-worker rule —
``pilot.pause()`` is CPU-idle-based, not worker-aware) so a probe's
deferred repaint lands before the assertion. Repo codes are abstract
placeholders (ABC / DEF / GHI / ...), never real-looking project names.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson
import pytest

from eawf.state.models import State
from eawf.tui.app import EaApp
from eawf.tui.scopes import ScopeScreen, UserScreen
from eawf.tui.scopes.user import PortfolioTable
from eawf.tui.screens.overlays.config_modal import ConfigModal
from eawf.tui.screens.overlays.detail import DetailModal
from eawf.tui.widgets.footer import Footer, Heartbeat
from eawf.tui.widgets.git_pane import GitFields
from eawf.tui.widgets.header import BRAND, DEFAULT_PROJECT_CODE, Header
from eawf.tui.widgets.workspace_table import WorkspaceTable

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_WORKSPACE = _FIXTURES / "05-workspace-state.json"


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the git probe to a deterministic clean tree.

    The portfolio table inherits the workspace table's live git column,
    which shells out via ``git_pane.gather_git_fields``; stubbing it keeps
    the rendered git column deterministic regardless of cwd / platform /
    parallel worker.
    """
    monkeypatch.setattr(
        "eawf.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(
            branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=()
        ),
    )


def _multi_repo_state(codes: list[str]) -> State:
    """Return a workspace state seeded with the given abstract repo *codes*."""
    payload = orjson.loads(_WORKSPACE.read_bytes())
    payload["workspace"]["repos"] = {
        code: {
            "code": code,
            "path": f"/abs/path/{code.lower()}",
            "state_urn": f"urn:eawf:v1:repo:{code}",
            "project_code": code,
            "title": f"{code} repo",
            "status": "active",
        }
        for code in codes
    }
    payload["workspace"]["current_repo_code"] = codes[0] if codes else None
    return State.model_validate(payload)


def _empty_registry_state() -> State:
    """Return a workspace state with an empty repo registry (N=0)."""
    return _multi_repo_state([])


# --------------------------------------------------------------------------
# D3 shared chassis — no per-scope chrome duplication
# --------------------------------------------------------------------------


def test_user_screen_reuses_shared_chassis_compose() -> None:
    assert UserScreen.compose is ScopeScreen.compose
    assert UserScreen.compose_body is not ScopeScreen.compose_body


def test_portfolio_table_reuses_workspace_family() -> None:
    """The portfolio table subclasses the W06 workspace table (DRY reuse)."""
    assert issubclass(PortfolioTable, WorkspaceTable)


# --------------------------------------------------------------------------
# Portfolio table renders rows reusing the workspace family
# --------------------------------------------------------------------------


def test_user_portfolio_table_renders() -> None:
    """The portfolio DataTable renders repo rows reusing the workspace family."""

    async def body() -> None:
        app = EaApp(scope="user", state_path=_WORKSPACE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            table = app.screen.query_one(PortfolioTable)
            assert isinstance(table, WorkspaceTable)
            assert table.row_count >= 1
            rows = table.rows_data()
            assert len(rows) >= 1
            assert rows[0].code == "QR"

    asyncio.run(body())


def test_user_portfolio_table_columns_match_workspace_family() -> None:
    """The portfolio grid carries the workspace family's five columns."""

    async def body() -> None:
        app = EaApp(scope="user", state_path=_WORKSPACE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            table = app.screen.query_one(PortfolioTable)
            assert len(table.columns) == 5
            assert table.get_cell("QR", "repo") == "QR"
            assert table.get_cell("QR", "git") == "clean"

    asyncio.run(body())


def test_user_portfolio_table_seeds_render_mode_from_app() -> None:
    """The portfolio table seeds its bar render mode off the app reactive."""

    async def body() -> None:
        app = EaApp(scope="user", state_path=_WORKSPACE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            table = app.screen.query_one(PortfolioTable)
            assert table.render_mode == app.render_mode

    asyncio.run(body())


# --------------------------------------------------------------------------
# N=1 boundary — exactly one row (not a fallback panel)
# --------------------------------------------------------------------------


def test_user_portfolio_table_renders_one_row_at_n1() -> None:
    async def body() -> None:
        app = EaApp(scope="user", state_path=_WORKSPACE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            table = app.screen.query_one(PortfolioTable)
            assert table.row_count == 1
            assert table.focused_repo() == "QR"

    asyncio.run(body())


# --------------------------------------------------------------------------
# N=0 boundary — empty registry renders no rows without crashing
# --------------------------------------------------------------------------


def test_user_portfolio_table_empty_registry_renders_no_rows() -> None:
    async def body() -> None:
        app = EaApp(scope="user", state_path=_WORKSPACE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.state = _empty_registry_state()
            await pilot.pause()
            await app.workers.wait_for_complete()
            table = app.screen.query_one(PortfolioTable)
            assert table.row_count == 0
            assert table.focused_repo() is None
            # The five columns persist even with no rows (not a fallback panel).
            assert len(table.columns) == 5

    asyncio.run(body())


# --------------------------------------------------------------------------
# Large-N — rows scroll within the table; column widths stay stable
# --------------------------------------------------------------------------


def test_user_portfolio_large_n_scrolls() -> None:
    """A 30-repo registry scrolls within the table; every column stays stable."""

    async def body() -> None:
        app = EaApp(scope="user", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.state = _multi_repo_state([f"R{n:02d}" for n in range(30)])
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.screen.query_one(PortfolioTable)
            assert table.row_count == 30
            # Column count + the off-screen tail row both render — the table
            # scrolls (more rows than the viewport height) without dropping
            # columns or clipping the last repo's code.
            assert len(table.columns) == 5
            assert table.get_cell("R00", "repo") == "R00"
            assert table.get_cell("R29", "repo") == "R29"

    asyncio.run(body())


# --------------------------------------------------------------------------
# Enter opens repo detail (no zoom quadrant); ↑↓ focus; z no-op
# --------------------------------------------------------------------------


def test_user_enter_opens_repo_detail_not_zoom() -> None:
    """Enter on a repo row opens the detail overlay — no zoom quadrant."""

    async def body() -> None:
        app = EaApp(scope="user", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, UserScreen)
            screen.query_one(PortfolioTable).focus()
            await pilot.press("enter")
            await pilot.pause()
            # The user scope opens a detail overlay, never a quadrant.
            assert isinstance(app.screen, DetailModal)
            assert not app.screen.query("#zoom-quadrant")

    asyncio.run(body())


def test_user_down_arrow_moves_focus() -> None:
    """``↓`` moves the row focus to the next repo (arrows are primary)."""

    async def body() -> None:
        app = EaApp(scope="user", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.state = _multi_repo_state(["ABC", "DEF", "GHI"])
            await pilot.pause()
            await app.workers.wait_for_complete()
            table = app.screen.query_one(PortfolioTable)
            table.focus()
            assert table.focused_repo() == "ABC"
            await pilot.press("down")
            await pilot.pause()
            assert table.focused_repo() == "DEF"

    asyncio.run(body())


def test_user_z_is_noop_no_zoom() -> None:
    """``z`` is inert in the user scope — no overlay, no quadrant."""

    async def body() -> None:
        app = EaApp(scope="user", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, UserScreen)
            screen.query_one(PortfolioTable).focus()
            await pilot.press("z")
            await pilot.pause()
            # Still on the UserScreen — no detail overlay, no quadrant.
            assert isinstance(app.screen, UserScreen)
            assert not app.screen.query("#zoom-quadrant")

    asyncio.run(body())


# --------------------------------------------------------------------------
# Composition — chassis + table; first paint
# --------------------------------------------------------------------------


def test_user_screen_composes_chassis_and_table() -> None:
    async def body() -> None:
        app = EaApp(scope="user", state_path=_WORKSPACE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, UserScreen)
            assert screen.query(Header)
            assert screen.query(Footer)
            assert screen.query(Heartbeat)
            assert screen.query_one(PortfolioTable)

    asyncio.run(body())


def test_user_screen_portfolio_title_present() -> None:
    async def body() -> None:
        app = EaApp(scope="user", state_path=_WORKSPACE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert "PORTFOLIO" in app.export_screenshot()

    asyncio.run(body())


def test_user_screen_none_state_first_paint_renders_brand() -> None:
    """The user scope launches with no resolved state (state_path=None per D10)."""

    async def body() -> None:
        app = EaApp(scope="user", state_path=None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            rendered = app.export_screenshot()
            assert BRAND in rendered
            # No resolved state => default-code breadcrumb.
            assert DEFAULT_PROJECT_CODE in rendered

    asyncio.run(body())


# --------------------------------------------------------------------------
# Footer hints + config binding
# --------------------------------------------------------------------------


def test_user_screen_footer_hints_applied() -> None:
    async def body() -> None:
        app = EaApp(scope="user", state_path=_WORKSPACE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            footer = app.screen.query_one(Footer)
            assert footer.hints == UserScreen.FOOTER_HINTS

    asyncio.run(body())


def test_user_screen_advertises_config_hint() -> None:
    assert "c config" in UserScreen.FOOTER_HINTS


def test_user_screen_binds_c_to_open_config() -> None:
    actions = {binding.action for binding in UserScreen.BINDINGS}
    assert "open_config" in actions


def test_user_c_keypress_opens_config_modal() -> None:
    # The user scope launches with no resolved state.json (state_path=None
    # per D10), so this also confirms config opens on the global layer with
    # no repo anchor.
    async def body() -> None:
        app = EaApp(scope="user", state_path=None)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigModal)

    asyncio.run(body())
