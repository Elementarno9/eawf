"""Pilot tests for the C06 ``WorkspaceScreen`` per-repo table + zoom (P27-I04-W06).

Covers the table-browse mode (a per-repo :class:`WorkspaceTable` with
>=1 row even at N=1, status-tinted completion + EU-burn bars), the
Enter zoom into a 2x2 quadrant scoped to the focused repo, the Esc
return, the live git column (refresh on tick, dim to ``git?`` on a probe
failure), the re-zoom-reloads-current-focus invariant, the D3
zero-duplication invariant, the scope-specific footer hints, and a
Pilot-driven first paint under the real palette against a workspace
fixture.

Determinism: every test that triggers a git probe awaits
``app.workers.wait_for_complete()`` (per the project Pilot-worker rule —
``pilot.pause()`` is CPU-idle-based, not worker-aware) so a probe's
deferred repaint lands before the assertion. Repo codes are abstract
placeholders (ABC / DEF / GHI), never real-looking project names.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson
import pytest
from rich.text import Text

from eawf.state.models import State
from eawf.tui.app import EaApp
from eawf.tui.scopes import ScopeScreen, WorkspaceScreen
from eawf.tui.screens.overlays.config_modal import ConfigModal
from eawf.tui.widgets.backlog_table import BacklogTable
from eawf.tui.widgets.footer import Footer, Heartbeat
from eawf.tui.widgets.git_pane import DASH, GitFields
from eawf.tui.widgets.header import BRAND, Header
from eawf.tui.widgets.roadmap_tree import RoadmapTree
from eawf.tui.widgets.status_pane import StatusPane
from eawf.tui.widgets.workspace_table import (
    GIT_UNAVAILABLE_CELL,
    RepoRow,
    WorkspaceTable,
    _eu_cell,
    _phase_cell,
    build_repo_rows,
    completion_pair,
    eu_pair,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_WORKSPACE = _FIXTURES / "05-workspace-state.json"


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the git probe to a deterministic clean tree by default.

    The workspace table shells out via ``git_pane.gather_git_fields``;
    stubbing it keeps the rendered git column deterministic regardless of
    cwd / platform / parallel worker. Tests that exercise the
    GIT_UNAVAILABLE path override this with a per-test monkeypatch.
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
    payload["workspace"]["current_repo_code"] = codes[0]
    return State.model_validate(payload)


# --------------------------------------------------------------------------
# D3 shared chassis — no per-scope chrome duplication
# --------------------------------------------------------------------------


def test_workspace_screen_reuses_shared_chassis_compose() -> None:
    assert WorkspaceScreen.compose is ScopeScreen.compose
    assert WorkspaceScreen.compose_body is not ScopeScreen.compose_body


# --------------------------------------------------------------------------
# Pure row builders — completion + EU pairs from a per-repo state dict
# --------------------------------------------------------------------------


def test_completion_pair_counts_closed_over_total() -> None:
    repo_state = {
        "waves": {
            "W1": {"status": "closed"},
            "W2": {"status": "pending"},
            "W3": {"status": "closed"},
        }
    }
    assert completion_pair(repo_state) == (2, 3)


def test_completion_pair_none_or_malformed_state_is_zero() -> None:
    assert completion_pair(None) == (0, 0)
    assert completion_pair({}) == (0, 0)
    assert completion_pair({"waves": "not-a-dict"}) == (0, 0)


def test_eu_pair_sums_actual_and_estimate_eu() -> None:
    repo_state = {
        "actuals": {"A1": {"elapsed_eu": 4.0}, "A2": {"elapsed_eu": 2.0}},
        "estimates": {"E1": {"expected_eu": 10.0}},
    }
    assert eu_pair(repo_state) == (6.0, 10.0)


def test_eu_pair_none_or_malformed_state_is_zero() -> None:
    assert eu_pair(None) == (0.0, 0.0)
    assert eu_pair({"actuals": None, "estimates": None}) == (0.0, 0.0)


def test_build_repo_rows_orders_by_code() -> None:
    state = _multi_repo_state(["DEF", "ABC", "GHI"])
    rows = build_repo_rows(state)
    assert [row.code for row in rows] == ["ABC", "DEF", "GHI"]


def test_build_repo_rows_none_state_is_empty() -> None:
    assert build_repo_rows(None) == []


def test_phase_and_eu_cells_render_bars() -> None:
    """A populated row renders a completion ``done/total`` cell + EU markup bar."""
    row = RepoRow(
        code="ABC",
        path="/abs/path/abc",
        phase_done=3,
        phase_total=6,
        eu_consumed=8.0,
        eu_total=12.0,
        age="2h",
    )
    phase_cell = _phase_cell(row, mode="braille")
    eu_cell = _eu_cell(row, mode="braille")
    assert "      3/6" in phase_cell  # counter right-aligned in a fixed 7-cell field
    # The EU bar is status-tinted with a resolved hex (not a Textual $var):
    # the cell is Rich-parsed, so the markup must contain a #rrggbb span and
    # parse without raising MarkupError.
    assert "no data" not in eu_cell
    assert "#" in eu_cell and "$" not in eu_cell
    Text.from_markup(eu_cell)  # regression guard: must not raise


def test_eu_cell_markup_is_rich_parseable_across_bands() -> None:
    """The EU-burn cell parses as Rich markup for every colour band.

    Regression (W22): the cell formerly emitted Textual ``[$ok|$warn|$err]``
    palette vars, which a Rich-parsed :class:`~textual.widgets.DataTable`
    ``str`` cell rejects with ``MarkupError`` ("closing tag has nothing to
    close"). Over-budget burn (the original crash) is covered by the last case.
    """
    for consumed, total in ((1.0, 12.0), (10.0, 12.0), (40.0, 12.0)):
        row = RepoRow(
            code="ABC",
            path="/abs/path/abc",
            phase_done=0,
            phase_total=1,
            eu_consumed=consumed,
            eu_total=total,
            age="1h",
        )
        cell = _eu_cell(row, mode="braille")
        assert "$" not in cell
        Text.from_markup(cell)  # must not raise


def test_eu_cell_zero_total_is_empty_sentinel() -> None:
    """A repo with no EU estimate surfaces the empty sentinel, not a fake bar."""
    row = RepoRow(
        code="ABC",
        path="/abs/path/abc",
        phase_done=0,
        phase_total=0,
        eu_consumed=0.0,
        eu_total=0.0,
        age="—",
    )
    assert "no data" in _eu_cell(row, mode="braille")


# --------------------------------------------------------------------------
# N=1 — exactly one row, focused (not a fallback panel)
# --------------------------------------------------------------------------


def test_workspace_table_renders_one_row_at_n1() -> None:
    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            table = app.screen.query_one(WorkspaceTable)
            assert table.row_count == 1
            rows = table.rows_data()
            assert len(rows) == 1
            assert rows[0].code == "QR"

    asyncio.run(body())


def test_workspace_table_row_focused_so_enter_would_zoom() -> None:
    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            table = app.screen.query_one(WorkspaceTable)
            assert table.focused_repo() == "QR"

    asyncio.run(body())


# --------------------------------------------------------------------------
# Enter zoom → quadrant; Esc returns
# --------------------------------------------------------------------------


def test_enter_zooms_focused_repo() -> None:
    """Enter zooms the focused repo into a 2x2 quadrant; Esc returns."""

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, WorkspaceScreen)
            assert not screen.zoomed
            screen.query_one(WorkspaceTable).focus()
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.zoomed
            assert screen.query_one("#zoom-quadrant RoadmapTree", RoadmapTree)
            assert screen.query_one("#zoom-quadrant StatusPane", StatusPane)
            assert screen.query_one("#zoom-quadrant BacklogTable", BacklogTable)
            await pilot.press("escape")
            await pilot.pause()
            assert not screen.zoomed
            assert not screen.query("#zoom-quadrant")

    asyncio.run(body())


def test_z_no_longer_zooms() -> None:
    """The secondary ``z`` zoom binding is dropped — ``z`` does not zoom."""

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, WorkspaceScreen)
            screen.query_one(WorkspaceTable).focus()
            await pilot.press("z")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert not screen.zoomed
            assert not screen.query("#zoom-quadrant")

    asyncio.run(body())


def test_esc_from_table_browse_quits() -> None:
    """Esc with no quadrant mounted falls through to the app quit."""

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, WorkspaceScreen)
            assert not screen.zoomed
            await pilot.press("escape")
            await pilot.pause()
            assert app._exit is True

    asyncio.run(body())


def test_re_zoom_reloads_current_focus() -> None:
    """Re-zoom mounts the quadrant scoped to the CURRENT focus, not a cached target.

    Zoom ABC, return to the table, move focus to DEF, then zoom again: the
    quadrant must scope to DEF (the current focus), not the cached ABC.
    """

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.state = _multi_repo_state(["ABC", "DEF", "GHI"])
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, WorkspaceScreen)
            table = screen.query_one(WorkspaceTable)
            table.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert screen._zoomed_code == "ABC"
            await pilot.press("escape")
            await pilot.pause()
            table.focus()
            await pilot.press("down")
            await pilot.pause()
            assert table.focused_repo() == "DEF"
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen._zoomed_code == "DEF"

    asyncio.run(body())


def test_zoom_out_mid_probe_is_clean() -> None:
    """Esc before the zoom git probe returns unmounts cleanly (no crash)."""

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, WorkspaceScreen)
            screen.query_one(WorkspaceTable).focus()
            await pilot.press("enter")
            await pilot.pause()
            assert screen.zoomed
            await pilot.press("escape")
            await pilot.pause()
            assert not screen.zoomed
            assert not screen.query("#zoom-quadrant")
            # Draining workers now must not raise (stale result dropped).
            await app.workers.wait_for_complete()

    asyncio.run(body())


# --------------------------------------------------------------------------
# Git column — dim to "git?" on GIT_UNAVAILABLE, large-N scroll
# --------------------------------------------------------------------------


def test_git_unavailable_dims_column_without_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed git probe dims the git cell to ``git?`` while other columns render."""
    monkeypatch.setattr(
        "eawf.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(branch=DASH, dirty=DASH, ahead_behind=DASH, recent_commits=()),
    )

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.screen.query_one(WorkspaceTable)
            assert table.get_cell("QR", "git") == GIT_UNAVAILABLE_CELL
            # Other columns still render (the repo code cell is intact).
            assert table.get_cell("QR", "repo") == "QR"

    asyncio.run(body())


def test_git_clean_column_renders_status() -> None:
    """A successful probe renders the dirty/clean status in the git cell."""

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.screen.query_one(WorkspaceTable)
            assert table.get_cell("QR", "git") == "clean"

    asyncio.run(body())


def test_large_n_rows_scroll_without_breaking_widths() -> None:
    """A 30-repo registry scrolls within the table; every row + column renders."""

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.state = _multi_repo_state([f"R{n:02d}" for n in range(30)])
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.screen.query_one(WorkspaceTable)
            assert table.row_count == 30
            assert len(table.columns) == 5
            assert table.get_cell("R29", "repo") == "R29"

    asyncio.run(body())


# --------------------------------------------------------------------------
# Composition — chassis + table; first paint
# --------------------------------------------------------------------------


def test_workspace_screen_composes_chassis_and_table() -> None:
    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, WorkspaceScreen)
            assert screen.query(Header)
            assert screen.query(Footer)
            assert screen.query(Heartbeat)
            assert screen.query_one(WorkspaceTable)

    asyncio.run(body())


def test_workspace_screen_table_title_present() -> None:
    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert "WORKSPACE" in app.export_screenshot()

    asyncio.run(body())


# --------------------------------------------------------------------------
# Footer hints + config binding
# --------------------------------------------------------------------------


def test_workspace_screen_footer_hints_applied() -> None:
    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            footer = app.screen.query_one(Footer)
            assert footer.hints == WorkspaceScreen.FOOTER_HINTS
            assert "zoom" in app.export_screenshot()

    asyncio.run(body())


def test_workspace_screen_advertises_config_hint() -> None:
    assert "c config" in WorkspaceScreen.FOOTER_HINTS


def test_workspace_screen_binds_c_to_open_config() -> None:
    actions = {binding.action for binding in WorkspaceScreen.BINDINGS}
    assert "open_config" in actions
    assert "leave_zoom" in actions
    # The secondary ``z`` zoom binding is dropped — Enter is the sole entry.
    assert "zoom_focused" not in actions


def test_workspace_c_keypress_opens_config_modal() -> None:
    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigModal)

    asyncio.run(body())


def test_workspace_screen_first_paint_renders_brand() -> None:
    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            rendered = app.export_screenshot()
            assert BRAND in rendered
            assert "workspace" in rendered

    asyncio.run(body())
