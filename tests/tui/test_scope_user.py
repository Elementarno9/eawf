"""Pilot tests for the C06 ``UserScreen`` portfolio table (P27-I04-W07).

Covers the full-screen per-repo :class:`PortfolioTable` (the reused W06
workspace-table family) — >=1 row even at N=1, the large-N scroll without
breaking column widths, the Enter / z zoom into a 2x2 quadrant scoped to
the focused repo (the shared zoom mixin), the Esc return, the ``↑↓``
focus movement, the D3 zero-duplication invariant, the scope-specific
footer hints, the empty registry boundary, and the ``c`` config binding.

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
from eawf.tui.scopes.user import PortfolioTable, synthesize_user_state
from eawf.tui.screens.overlays.config_modal import ConfigModal
from eawf.tui.screens.overlays.detail import DetailModal
from eawf.tui.widgets.footer import Footer, Heartbeat
from eawf.tui.widgets.git_pane import GitFields
from eawf.tui.widgets.header import BRAND, DEFAULT_PROJECT_CODE, Header
from eawf.tui.widgets.workspace_table import WorkspaceTable, build_repo_rows

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_WORKSPACE = _FIXTURES / "05-workspace-state.json"


def _write_registry(home: Path, repos: dict[str, dict[str, str]]) -> Path:
    """Write a ``registry.json`` under *home*/.eawf and return its path.

    Args:
        home: A ``tmp_path``-rooted fake home directory.
        repos: Mapping of repo code → entry payload (``code`` / ``path`` /
            optional ``title``).

    Returns:
        The written registry path under ``<home>/.eawf/registry.json``.
    """
    ea_dir = home / ".eawf"
    ea_dir.mkdir(parents=True, exist_ok=True)
    path = ea_dir / "registry.json"
    payload = {"version": "1", "repos": repos}
    path.write_bytes(orjson.dumps(payload))
    return path


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home.

    Any test that launches the user scope with ``state_path=None`` triggers
    :func:`synthesize_user_state`, which reads ``~/.eawf/registry.json``.
    Redirecting ``Path.home`` to an empty ``tmp_path`` keeps those launches
    deterministic and ensures no test ever reads the operator's real
    registry (which would leak machine paths into a rendered screenshot).
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


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
# Enter / z zoom → quadrant; Esc returns; ↑↓ focus
# --------------------------------------------------------------------------


def test_user_enter_zooms_focused_repo() -> None:
    """Enter on a repo row mounts the 2x2 zoom quadrant — no detail overlay.

    The repo refs in the abstract fixtures point at ``/abs/path/...`` with
    no on-disk ``.ea/state.json``, so the quadrant mounts with empty
    widget state — the mount is what this asserts, not the seeded content.
    """

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
            await app.workers.wait_for_complete()
            # The user scope zooms into a quadrant, never a detail overlay.
            assert isinstance(app.screen, UserScreen)
            assert not isinstance(app.screen, DetailModal)
            assert app.screen.query("#zoom-quadrant")

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


def test_user_z_zooms_focused_repo() -> None:
    """``z`` zooms the focused repo into the 2x2 quadrant, like Enter."""

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
            await app.workers.wait_for_complete()
            # Still on the UserScreen with the quadrant mounted.
            assert isinstance(app.screen, UserScreen)
            assert app.screen.query("#zoom-quadrant")

    asyncio.run(body())


def test_user_esc_while_zoomed_returns_to_table() -> None:
    """Esc unmounts the quadrant and returns to the portfolio table."""

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
            await app.workers.wait_for_complete()
            assert screen.query("#zoom-quadrant")
            await pilot.press("escape")
            await pilot.pause()
            # The quadrant unmounts and we are back on the table.
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


# --------------------------------------------------------------------------
# synthesize_user_state — pure helper over the global registry (P27-I04-W15)
# --------------------------------------------------------------------------


def test_synthesize_user_state_maps_registry_entries(tmp_path: Path) -> None:
    """N registry entries → N workspace repos with the right code + path."""
    _write_registry(
        tmp_path,
        {
            "ABC": {"code": "ABC", "path": "/abs/path/abc", "title": "Abc repo"},
            "DEF": {"code": "DEF", "path": "/abs/path/def"},
        },
    )
    state = synthesize_user_state(home=tmp_path)
    assert state.workspace is not None
    repos = state.workspace.repos
    assert set(repos) == {"ABC", "DEF"}
    assert repos["ABC"].code == "ABC"
    assert repos["ABC"].path == "/abs/path/abc"
    assert repos["ABC"].title == "Abc repo"
    # Missing title falls back to the code.
    assert repos["DEF"].title == "DEF"
    assert repos["DEF"].path == "/abs/path/def"


def test_synthesize_user_state_via_explicit_registry_path(tmp_path: Path) -> None:
    """The explicit ``registry_path`` seam bypasses the home resolver."""
    path = _write_registry(tmp_path, {"GHI": {"code": "GHI", "path": "/abs/path/ghi"}})
    state = synthesize_user_state(registry_path=path)
    assert state.workspace is not None
    assert set(state.workspace.repos) == {"GHI"}


def test_synthesize_user_state_build_repo_rows_yields_one_row_per_entry(
    tmp_path: Path,
) -> None:
    """``build_repo_rows`` over the synthesized state yields N rows in order."""
    _write_registry(
        tmp_path,
        {
            "ABC": {"code": "ABC", "path": "/abs/path/abc"},
            "DEF": {"code": "DEF", "path": "/abs/path/def"},
            "GHI": {"code": "GHI", "path": "/abs/path/ghi"},
        },
    )
    state = synthesize_user_state(home=tmp_path)
    rows = build_repo_rows(state)
    assert [row.code for row in rows] == ["ABC", "DEF", "GHI"]
    assert [row.path for row in rows] == [
        "/abs/path/abc",
        "/abs/path/def",
        "/abs/path/ghi",
    ]


def test_synthesize_user_state_empty_registry_yields_zero_rows(tmp_path: Path) -> None:
    """An empty registry (no repos) → empty workspace → zero rows, no crash."""
    _write_registry(tmp_path, {})
    state = synthesize_user_state(home=tmp_path)
    assert state.workspace is not None
    assert state.workspace.repos == {}
    assert build_repo_rows(state) == []


def test_synthesize_user_state_missing_registry_yields_empty_repos(
    tmp_path: Path,
) -> None:
    """A missing ``~/.eawf/registry.json`` → empty repos, no exception."""
    # No registry file written under tmp_path's home.
    state = synthesize_user_state(home=tmp_path)
    assert state.workspace is not None
    assert state.workspace.repos == {}
    assert build_repo_rows(state) == []


def test_synthesize_user_state_repo_ref_urn_is_valid(tmp_path: Path) -> None:
    """Each synthesized repo ref carries a valid ``urn:eawf:v1:repo`` URN."""
    _write_registry(tmp_path, {"ABC": {"code": "ABC", "path": "/abs/path/abc"}})
    state = synthesize_user_state(home=tmp_path)
    assert state.workspace is not None
    ref = state.workspace.repos["ABC"]
    assert ref.state_urn == "urn:eawf:v1:repo:ABC"
    assert ref.status.value == "active"
    assert ref.project_code == "ABC"


def test_synthesize_user_state_root_shape(tmp_path: Path) -> None:
    """The synthesized state root carries the workspace scope + empty maps."""
    _write_registry(tmp_path, {"ABC": {"code": "ABC", "path": "/abs/path/abc"}})
    state = synthesize_user_state(home=tmp_path)
    assert state.scope_kind.value == "workspace"
    assert state.project is None
    assert state.urn == "urn:eawf:v1:workspace:PORTFOLIO"
    assert state.phases == {}
    assert state.waves == {}


def test_synthesize_user_state_preserves_active_code_as_current(
    tmp_path: Path,
) -> None:
    """The registry's ``active_code`` carries onto ``current_repo_code``."""
    ea_dir = tmp_path / ".eawf"
    ea_dir.mkdir(parents=True, exist_ok=True)
    (ea_dir / "registry.json").write_bytes(
        orjson.dumps(
            {
                "version": "1",
                "active_code": "ABC",
                "repos": {"ABC": {"code": "ABC", "path": "/abs/path/abc"}},
            }
        )
    )
    state = synthesize_user_state(home=tmp_path)
    assert state.workspace is not None
    assert state.workspace.current_repo_code == "ABC"


def test_user_scope_none_state_synthesizes_from_registry(tmp_path: Path) -> None:
    """Launching the user scope with no state binds the synthesized registry state."""
    _write_registry(
        tmp_path,
        {
            "ABC": {"code": "ABC", "path": "/abs/path/abc"},
            "DEF": {"code": "DEF", "path": "/abs/path/def"},
        },
    )

    async def body() -> None:
        # state_path=None drives the on_mount synthesis path; the autouse
        # _isolate_registry fixture has already redirected Path.home to
        # tmp_path so the real registry is never read.
        app = EaApp(scope="user", state_path=None)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            table = app.screen.query_one(PortfolioTable)
            assert table.row_count == 2
            assert {row.code for row in table.rows_data()} == {"ABC", "DEF"}

    asyncio.run(body())
