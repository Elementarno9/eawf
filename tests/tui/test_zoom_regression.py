"""Regression tests for the focused-repo zoom lifecycle (P27-I04-W28).

Two in-session reports against the P27-I04 zoom path:

* **Re-zoom DuplicateIds.** ``_clear_zoom_mount`` scheduled an async
  ``remove()`` but ``_enter_zoom`` mounted the new quadrant synchronously
  in the same frame, so a re-zoom inserted a second ``id="zoom-quadrant"``
  before the first was pruned → ``DuplicateIds``. The fix makes the mount
  path **await** the unmount; :func:`test_rezoom_awaits_unmount_no_duplicate`
  drives two back-to-back zooms through the real async handler and pins a
  single mounted quadrant.
* **Cached-screen zoom leak.** Textual reuses named ``SCREENS`` instances,
  so a screen zoomed before a scope switch carried its stale quadrant — and
  a hidden browse pane — back when the operator returned. The fix exits the
  zoom on :meth:`~eawf.tui.scopes._zoom.RepoZoomMixin.on_screen_suspend`;
  :func:`test_zoom_does_not_leak_across_scope_switch` zooms the user scope,
  switches to repo and back, and pins the returned screen un-zoomed.

Determinism: each test awaits ``app.workers.wait_for_complete()`` after a
zoom (per the project Pilot-worker rule — ``pilot.pause()`` is
CPU-idle-based, not worker-aware) so a git probe's deferred repaint lands
before the assertion. Repo codes are abstract placeholders (ABC / ...),
never real-looking names; the autouse ``_isolate_registry`` fixture
redirects ``Path.home`` to ``tmp_path`` so the ``u`` switch never reads the
operator's real ``~/.eawf/registry.json``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.containers import Vertical

from eawf.tui.app import EaApp
from eawf.tui.scopes import UserScreen, WorkspaceScreen
from eawf.tui.scopes.user import PortfolioTable
from eawf.tui.widgets.git_pane import GitFields
from eawf.tui.widgets.workspace_table import WorkspaceTable

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_WORKSPACE = _FIXTURES / "05-workspace-state.json"
#: The single repo code seeded in the workspace fixture's repo index.
_FIXTURE_REPO = "QR"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home.

    The ``u`` scope switch calls
    :func:`~eawf.tui.scopes.user.synthesize_user_state`, which reads
    ``~/.eawf/registry.json``. Redirecting ``Path.home`` keeps the switch
    deterministic and ensures no test reads the operator's real registry.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the git probe to a deterministic clean tree."""
    monkeypatch.setattr(
        "eawf.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(
            branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=()
        ),
    )


def test_rezoom_awaits_unmount_no_duplicate() -> None:
    """A re-zoom awaits the prior unmount — exactly one quadrant, no DuplicateIds.

    Drives two back-to-back zooms through the real ``RowZoomed`` handler.
    Before the fix the second ``_enter_zoom`` mounted synchronously while
    the first quadrant's async ``remove()`` was still pending, inserting a
    second ``id="zoom-quadrant"`` → ``DuplicateIds``. Awaiting the unmount
    first keeps a single mounted quadrant.
    """

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, WorkspaceScreen)
            await screen.on_workspace_table_row_zoomed(WorkspaceTable.RowZoomed(_FIXTURE_REPO))
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.zoomed
            assert len(screen.query("#zoom-quadrant")) == 1
            # Re-zoom in tight succession: the mount must await the unmount.
            await screen.on_workspace_table_row_zoomed(WorkspaceTable.RowZoomed(_FIXTURE_REPO))
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.zoomed
            assert len(screen.query("#zoom-quadrant")) == 1

    asyncio.run(body())


def test_esc_then_enter_keypath_no_duplicate() -> None:
    """The Esc→Enter re-zoom key path leaves exactly one quadrant, no crash."""

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
            await app.workers.wait_for_complete()
            assert screen.zoomed
            await pilot.press("escape")
            await pilot.pause()
            assert not screen.zoomed
            assert not screen.query("#zoom-quadrant")
            screen.query_one(WorkspaceTable).focus()
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.zoomed
            assert len(screen.query("#zoom-quadrant")) == 1

    asyncio.run(body())


def test_zoom_does_not_leak_across_scope_switch() -> None:
    """A zoom in the user scope does not survive a switch away and back.

    Textual caches named ``SCREENS`` instances, so without the
    suspend-time reset the user screen would return still zoomed, hiding
    its browse pane behind a stale quadrant. ``on_screen_suspend`` exits
    the zoom when the screen is suspended on the ``r`` switch.
    """

    async def body() -> None:
        app = EaApp(scope="user", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            user_screen = app.screen
            assert isinstance(user_screen, UserScreen)
            user_screen.query_one(PortfolioTable).focus()
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert user_screen.zoomed
            # Switch away to the repo scope, then back to the (cached) user scope.
            await pilot.press("r")
            await pilot.pause()
            await pilot.press("u")
            await pilot.pause()
            await app.workers.wait_for_complete()
            back = app.screen
            assert isinstance(back, UserScreen)
            assert back is user_screen  # the cached instance is reused
            # The cached user screen is no longer zoomed: browse pane visible,
            # zero quadrants mounted.
            assert not back.zoomed
            assert len(back.query("#zoom-quadrant")) == 0
            assert back.query_one("#pane-portfolio", Vertical).display is True

    asyncio.run(body())
