"""Pilot regression test for the live ``u``-key scope switch (P27-I04-W19).

The user-scope portfolio rendered EMPTY when reached via the live ``u``
scope switch: :meth:`~eawf.surfaces.tui.app.EaApp.action_switch_scope` swapped to
the user screen but never re-synthesized the portfolio state, leaving the
table bound to the stale repo/workspace state (whose ``workspace`` is
``None``), so :func:`~eawf.surfaces.tui.widgets.workspace_table.build_repo_rows`
emitted zero rows. The ``on_mount`` launch path already synthesizes; this
test pins the live-switch path to the same behaviour.

The app launches into the workspace scope (a seeded repo workspace state),
then presses ``u`` (and, in a second test, calls
:meth:`~eawf.surfaces.tui.app.EaApp.action_switch_scope` directly). After the
switch the bound state must be the synthesized portfolio and the
:class:`~eawf.surfaces.tui.scopes.user.PortfolioTable` must carry one row per
seeded registry repo.

Determinism: each test awaits ``app.workers.wait_for_complete()`` after
the switch (per the project Pilot-worker rule — ``pilot.pause()`` is
CPU-idle-based, not worker-aware) so the git-probe's deferred repaint
lands before the assertion. Repo codes are abstract placeholders
(ABC / DEF / ...), never real-looking project names; the autouse
``_isolate_registry`` fixture redirects ``Path.home`` to ``tmp_path`` so
no test ever reads the operator's real ``~/.eawf/registry.json``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson
import pytest

from eawf.kernel.state.enums import ScopeKind
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.scopes import RepoScreen, UserScreen, WorkspaceScreen
from eawf.surfaces.tui.scopes.user import PortfolioTable
from eawf.surfaces.tui.widgets.git_pane import GitFields
from eawf.surfaces.tui.widgets.header import build_breadcrumb
from eawf.surfaces.tui.widgets.roadmap_tree import RoadmapTree
from eawf.surfaces.tui.widgets.workspace_table import WorkspaceTable, build_repo_rows

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_WORKSPACE = _FIXTURES / "05-workspace-state.json"
_REPO = _FIXTURES / "03-phase-iter-wave-active.json"


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
    """Point registry resolution at a ``tmp_path`` home for every test.

    The live ``u`` switch calls
    :func:`~eawf.surfaces.tui.scopes.user.synthesize_user_state`, which reads
    ``~/.eawf/registry.json``. Redirecting ``Path.home`` to ``tmp_path``
    keeps the switch deterministic and ensures no test reads (or leaks the
    machine paths from) the operator's real registry.
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
        "eawf.surfaces.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(
            branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=()
        ),
    )


def test_u_keypress_switch_synthesizes_portfolio(tmp_path: Path) -> None:
    """Pressing ``u`` from the workspace scope rebinds the synthesized portfolio."""
    _write_registry(
        tmp_path,
        {
            "ABC": {"code": "ABC", "path": "/abs/path/abc"},
            "DEF": {"code": "DEF", "path": "/abs/path/def"},
        },
    )

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("u")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, UserScreen)
            # The bound state is the synthesized portfolio, not the stale
            # repo/workspace state (which has the seeded fixture repos).
            assert app.state is not None
            assert app.state.workspace is not None
            rows = build_repo_rows(app.state)
            assert {row.code for row in rows} == {"ABC", "DEF"}
            table = app.screen.query_one(PortfolioTable)
            assert table.row_count == 2
            assert {row.code for row in table.rows_data()} == {"ABC", "DEF"}

    asyncio.run(body())


def test_action_switch_scope_user_rebinds_state(tmp_path: Path) -> None:
    """Calling ``action_switch_scope('user')`` rebinds state to the portfolio."""
    _write_registry(tmp_path, {"ABC": {"code": "ABC", "path": "/abs/path/abc"}})

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            app.action_switch_scope("user")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, UserScreen)
            assert app.state is not None
            assert app.state.workspace is not None
            table = app.screen.query_one(PortfolioTable)
            assert table.row_count == 1
            assert table.rows_data()[0].code == "ABC"

    asyncio.run(body())


def test_r_keypress_after_u_rebinds_repo_state(tmp_path: Path) -> None:
    """Launch repo, press ``u`` then ``r``: the repo state + roadmap restore.

    Regression for the inverse of the W19 ``→user`` fix: switching back to
    ``repo`` left the stale synthesized portfolio bound (``workspace`` set,
    no ``phases``), so the repo roadmap rendered empty. The switch now
    re-reads the launch ``state.json`` for ``repo`` / ``workspace``.
    """
    _write_registry(tmp_path, {"ABC": {"code": "ABC", "path": "/abs/path/abc"}})

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.state is not None
            assert app.state.scope_kind is ScopeKind.REPO
            assert len(app.state.phases) > 0
            await pilot.press("u")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, UserScreen)
            # The user screen breadcrumb reads "user", not the synth "workspace".
            assert build_breadcrumb(app.state, app._scope).startswith("user")
            await pilot.press("r")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, RepoScreen)
            # The repo state is rebound: scope_kind=repo, phases>0.
            assert app.state is not None
            assert app.state.scope_kind is ScopeKind.REPO
            assert len(app.state.phases) > 0
            tree = app.screen.query_one("#roadmap-tree", RoadmapTree)
            assert len(tree.root.children) > 0

    asyncio.run(body())


def test_w_keypress_after_u_rebinds_workspace_state(tmp_path: Path) -> None:
    """Launch workspace, press ``u`` then ``w``: the workspace state restores."""
    _write_registry(tmp_path, {"ABC": {"code": "ABC", "path": "/abs/path/abc"}})

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("u")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, UserScreen)
            await pilot.press("w")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, WorkspaceScreen)
            assert app.state is not None
            assert app.state.scope_kind is ScopeKind.WORKSPACE
            assert app.state.workspace is not None
            rows = build_repo_rows(app.state)
            # The launch workspace repos are restored, not the synth portfolio
            # (which would carry the registry's "ABC").
            assert len(rows) > 0
            assert "ABC" not in {row.code for row in rows}
            table = app.screen.query_one(WorkspaceTable)
            assert table.row_count == len(rows)

    asyncio.run(body())


def test_u_switch_empty_registry_renders_no_rows(tmp_path: Path) -> None:
    """An empty registry on switch → zero rows, no crash (columns persist)."""
    _write_registry(tmp_path, {})

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("u")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, UserScreen)
            assert build_repo_rows(app.state) == []
            table = app.screen.query_one(PortfolioTable)
            assert table.row_count == 0
            # The five columns persist even with no rows (not a fallback panel).
            assert len(table.columns) == 5

    asyncio.run(body())
