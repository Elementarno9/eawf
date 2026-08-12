"""Regression tests for the focused-repo zoom lifecycle.

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
  zoom on :meth:`~eawf.surfaces.tui.scopes._zoom.RepoZoomMixin.on_screen_suspend`;
  :func:`test_zoom_does_not_leak_across_scope_switch` zooms the user scope,
  switches to repo and back, and pins the returned screen un-zoomed.

A third report against the same path:

* **Modal push destroyed the zoom.** Textual posts ``ScreenSuspend`` to
  the active scope screen when a modal is pushed on top (``c`` config,
  ``?`` help, ``/`` palette), exactly as it does on a switch-away — the
  event cannot tell the two apart. The old ``on_screen_suspend`` tore the
  quadrant down on every suspend, so opening a modal over a zoomed scope
  destroyed the zoom irrecoverably; on dismiss the operator was dumped
  back to table-browse. The fix adds the shared
  :meth:`~eawf.surfaces.tui.scopes._zoom.RepoZoomMixin._suspend_is_transient`
  guard (a modal push leaves the screen **on** ``app.screen_stack``; a
  switch-away has popped it **off**), tears down on both but remembers the
  focused repo on a transient suspend, and rebuilds the quadrant on the
  following ``ScreenResume``. The ``_modal_*`` tests below drive
  zoom→push-modal→dismiss and assert the zoom is rebuilt to the same repo
  with exactly one quadrant, while a switch-away still tears down with no
  rebuild and the guard stays idempotent under a tight round-trip.

A fourth report against the same path:

* **Zoom dropped keyboard focus.** Zooming hides the browse
  ``WorkspaceTable`` (``display = False``), which blurs the table that held
  focus and left focus unset -- so Enter and the arrow keys landed nowhere
  in the zoomed quadrant. Exiting zoom (or a real switch-away) symmetrically
  left the restored browse table unfocused. The fix moves focus onto the
  quadrant's ``#zoom-roadmap`` tree on zoom (its primary drill target,
  mirroring the repo scope's Enter target) and back onto the browse
  ``WorkspaceTable`` on exit. The ``_focus_*`` tests below pin both
  transitions.

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
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.scopes import UserScreen, WorkspaceScreen
from eawf.surfaces.tui.scopes._zoom import RepoZoomMixin
from eawf.surfaces.tui.scopes.user import PortfolioTable
from eawf.surfaces.tui.widgets.git_pane import GitFields
from eawf.surfaces.tui.widgets.roadmap_tree import RoadmapTree
from eawf.surfaces.tui.widgets.workspace_table import (
    WorkspaceTable,
    _sigil_hex,
    repo_row_from_path,
    repo_row_sigil,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_WORKSPACE = _FIXTURES / "05-workspace-state.json"
#: The single repo code seeded in the workspace fixture's repo index.
_FIXTURE_REPO = "QR"


class _ProbeModal(ModalScreen[None]):
    """A minimal modal for exercising the transient-suspend lifecycle.

    Pushing any :class:`~textual.screen.ModalScreen` posts ``ScreenSuspend``
    to the scope screen underneath and ``ScreenResume`` when it dismisses,
    which is the exact Textual path the zoom guard must survive. This stub
    stands in for the real config / help / palette overlays so the test
    isolates the zoom-rebuild behaviour from any overlay's own internals.
    """

    def compose(self) -> ComposeResult:
        yield Static("probe-modal")


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home.

    The ``u`` scope switch calls
    :func:`~eawf.surfaces.tui.scopes.user.synthesize_user_state`, which reads
    ``~/.eawf/registry.json``. Redirecting ``Path.home`` keeps the switch
    deterministic and ensures no test reads the operator's real registry.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the git probe to a deterministic clean tree."""
    monkeypatch.setattr(
        "eawf.surfaces.tui.widgets.workspace_table.gather_git_fields",
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
            assert app.screen.__class__.__name__ == "InitWizardModal"
            await pilot.press("escape")
            await pilot.pause()
            back = app.screen
            assert isinstance(back, UserScreen)
            assert back is user_screen  # the cached instance is reused
            # The cached user screen is no longer zoomed: browse pane visible,
            # zero quadrants mounted.
            assert not back.zoomed
            assert len(back.query("#zoom-quadrant")) == 0
            assert back.query_one("#pane-portfolio", Vertical).display is True

    asyncio.run(body())


def test_suspend_is_transient_distinguishes_modal_from_switch_away() -> None:
    """The shared guard reads stack membership: modal-on-top vs popped-off.

    A pushed modal leaves the scope screen **on** ``app.screen_stack`` so
    the guard reports a transient suspend; popping back to the bare screen
    leaves it the top of the stack (still a member), and the guard only
    reports a real switch-away once the screen has been popped off — which
    :func:`test_zoom_torn_down_on_real_switch_away` exercises end to end.
    Here the unit check pins the membership predicate directly.
    """

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, WorkspaceScreen)
            # Top of the stack -> a member -> transient (no switch-away).
            assert screen._suspend_is_transient() is True
            app.push_screen(_ProbeModal())
            await pilot.pause()
            # A modal sits on top, the scope screen is still stacked beneath.
            assert screen._suspend_is_transient() is True
            app.pop_screen()
            await pilot.pause()
            assert screen._suspend_is_transient() is True

    asyncio.run(body())


def test_zoom_survives_modal_push_dismiss_resume_rebuild() -> None:
    """Zoom -> push modal (transient suspend) -> dismiss -> zoom rebuilt.

    Reproduces the W15 report: pushing a modal over a zoomed scope posts
    ``ScreenSuspend`` to the scope screen exactly as a switch-away does, so
    the old unconditional teardown destroyed the zoom and the dismiss
    dumped the operator to table-browse. The guard tears the quadrant down
    on the transient suspend (no hidden git probe survives) but remembers
    the focused repo, and ``on_screen_resume`` rebuilds it on dismiss.
    Asserts the rebuilt quadrant is scoped to the same repo, the browse
    pane is hidden again, and exactly one ``#zoom-quadrant`` exists.
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
            assert screen._zoomed_code == _FIXTURE_REPO
            assert len(screen.query("#zoom-quadrant")) == 1
            # Push a modal: the scope screen is suspended while it stays on
            # the stack. The quadrant is torn down but the repo remembered.
            app.push_screen(_ProbeModal())
            await pilot.pause()
            assert app.screen.__class__.__name__ == "_ProbeModal"
            assert not screen.zoomed
            assert len(screen.query("#zoom-quadrant")) == 0
            assert screen._resume_code == _FIXTURE_REPO
            # Dismiss the modal: ScreenResume rebuilds the quadrant.
            app.pop_screen()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.screen is screen
            assert screen.zoomed
            assert screen._zoomed_code == _FIXTURE_REPO
            assert screen._resume_code is None
            assert len(screen.query("#zoom-quadrant")) == 1
            assert screen.query_one("#pane-repos", Vertical).display is False

    asyncio.run(body())


def test_zoom_modal_keypath_config_window_round_trip() -> None:
    """The real ``c`` config window over a zoom is a transient suspend.

    Drives the operator key path end to end: zoom the focused repo, press
    ``c`` to open the registry-driven config modal (a real
    :class:`~textual.screen.ModalScreen` pushed via the app's modal-cap
    helper), then ``Esc`` to dismiss it. The zoom must come back rebuilt to
    the same repo with a single quadrant — proving the guard handles the
    production modal, not only the test stub.
    """

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
            await pilot.press("c")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.screen.__class__.__name__ == "ConfigModal"
            assert not screen.zoomed
            assert screen._resume_code == _FIXTURE_REPO
            await pilot.press("escape")
            await pilot.pause()
            await app.workers.wait_for_complete()
            back = app.screen
            assert isinstance(back, WorkspaceScreen)
            assert back is screen
            assert back.zoomed
            assert back._zoomed_code == _FIXTURE_REPO
            assert back._resume_code is None
            assert len(back.query("#zoom-quadrant")) == 1

    asyncio.run(body())


def test_zoom_torn_down_on_real_switch_away_no_rebuild() -> None:
    """A real switch-away tears the zoom down and arms no resume rebuild.

    The companion to the modal-push case: when the scope screen is
    suspended because it was switched away from (popped off the stack, not
    overlaid), the guard reports a non-transient suspend, so the quadrant is
    torn down and ``_resume_code`` stays ``None``. Switching back must
    therefore return the cached screen in table-browse, never auto-re-zoom.
    """

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            ws_screen = app.screen
            assert isinstance(ws_screen, WorkspaceScreen)
            await ws_screen.on_workspace_table_row_zoomed(WorkspaceTable.RowZoomed(_FIXTURE_REPO))
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert ws_screen.zoomed
            # Switch away to the repo scope: a real suspend (screen popped).
            await pilot.press("r")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert not ws_screen.zoomed
            assert ws_screen._resume_code is None
            assert len(ws_screen.query("#zoom-quadrant")) == 0
            # Switch back: the cached workspace screen returns un-zoomed.
            await pilot.press("w")
            await pilot.pause()
            await app.workers.wait_for_complete()
            back = app.screen
            assert isinstance(back, WorkspaceScreen)
            assert back is ws_screen
            assert not back.zoomed
            assert len(back.query("#zoom-quadrant")) == 0
            assert back.query_one("#pane-repos", Vertical).display is True

    asyncio.run(body())


def test_modal_resume_rebuild_is_idempotent_single_quadrant() -> None:
    """Tight zoom -> modal -> dismiss -> re-zoom never double-mounts.

    Stresses the rebuild path's idempotency: after a modal round-trip
    rebuilds the quadrant, an immediate explicit re-zoom (Enter) must still
    leave exactly one ``#zoom-quadrant`` — the ``_enter_zoom`` await-the-
    unmount invariant holds through the resume-driven rebuild, and the
    explicit re-zoom clears any lingering ``_resume_code`` so no stale
    rebuild can fire a second mount.
    """

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
            # Modal round-trip rebuilds the quadrant.
            app.push_screen(_ProbeModal())
            await pilot.pause()
            app.pop_screen()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.zoomed
            assert len(screen.query("#zoom-quadrant")) == 1
            # Immediate explicit re-zoom on the rebuilt quadrant.
            screen.query_one(WorkspaceTable).focus()
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.zoomed
            assert screen._resume_code is None
            assert len(screen.query("#zoom-quadrant")) == 1

    asyncio.run(body())


def test_zoom_focuses_quadrant_roadmap_so_enter_lands() -> None:
    """Zooming moves focus onto the quadrant's roadmap tree.

    Reproduces the W26 report: hiding the browse table on zoom blurs the
    focused ``WorkspaceTable`` and leaves focus unset, so Enter / arrows hit
    nothing in the zoomed quadrant. The fix focuses ``#zoom-roadmap`` after
    the quadrant mounts; this pins the focused widget is the zoom roadmap
    (and is the right instance -- the mounted quadrant tree, not the hidden
    browse table).
    """

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
            await pilot.pause()
            assert screen.zoomed
            focused = app.focused
            assert focused is not None
            assert focused.id == "zoom-roadmap"
            assert isinstance(focused, RoadmapTree)

    asyncio.run(body())


def test_exit_zoom_restores_focus_to_browse_table() -> None:
    """Leaving zoom moves focus back onto the visible browse table.

    The symmetric half of the W26 focus fix: unmounting the quadrant blurs
    whatever quadrant widget held focus, so without the restore the operator
    returns to table-browse with nothing focused and the arrow keys / Enter
    dead. ``_exit_zoom`` focuses the ``WorkspaceTable``; this drives
    zoom→Esc and pins the focused widget is the browse table again.
    """

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
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not screen.zoomed
            focused = app.focused
            assert focused is not None
            assert isinstance(focused, WorkspaceTable)

    asyncio.run(body())


# --------------------------------------------------------------------------
# P30-I08-W04: the zoom quadrant in the reskin (green/sigil title + shared
# accent-dim focus tint across both scopes)
# --------------------------------------------------------------------------


def _expected_title(repo_code: str, repo_path: str, *, mode: str) -> str:
    """Return the green/sigil quadrant title the shared grid helpers produce.

    Built off the same
    :func:`~eawf.surfaces.tui.widgets.workspace_table.repo_row_sigil` +
    :func:`~eawf.surfaces.tui.widgets.workspace_table._sigil_hex` pipeline the
    parent grid row uses, so the test pins parity with the grid rather than
    hard-coding a glyph -- a retune of the sigil / tint lands in one place and
    both the title and this expectation track it.

    Args:
        repo_code: The focused repo's project code.
        repo_path: The focused repo's on-disk path (the sigil's state source).
        mode: The render-mode label selecting the glyph column.

    Returns:
        The expected ``<tinted-sigil> REPO . <code>`` markup.
    """
    row = repo_row_from_path(repo_code, repo_path)
    from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_BAND_PALETTE

    sigil = _sigil_hex(repo_row_sigil(row), mode=mode, palette=DEFAULT_BAND_PALETTE)
    return f"{sigil} REPO · {repo_code}"


def test_quadrant_title_renders_green_sigil_language() -> None:
    """The quadrant header leads with the parent grid's tinted lifecycle sigil.

    The reskin replaces the bare ``REPO . <code>`` header with the green/sigil
    language: the title is built through the SAME ``repo_row_sigil`` /
    ``_sigil_hex`` helpers the parent grid row wears, so a stale fixture repo's
    header opens with the muted ABANDONED circled-slash span exactly as its grid
    row does. Asserting against the shared-helper expectation (not a literal
    glyph) pins the parity invariant: the title and the row read the same sigil.
    """

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, WorkspaceScreen)
            assert app.state is not None
            assert app.state.workspace is not None
            repo_path = app.state.workspace.repos[_FIXTURE_REPO].path
            title = screen._quadrant_title(_FIXTURE_REPO, repo_path)
            assert title == _expected_title(_FIXTURE_REPO, repo_path, mode=app.render_mode)
            # The green/sigil span leads; the plain label trails it.
            assert title.endswith(f" REPO · {_FIXTURE_REPO}")
            assert title.startswith("[")

    asyncio.run(body())


def test_quadrant_title_tracks_render_mode_ascii() -> None:
    """The quadrant title resolves the ASCII glyph column under ASCII mode.

    The title threads the App's resolved ``render_mode`` into the sigil column
    just as every other reskin surface does, so an ASCII-mode operator sees the
    deconflicted ASCII sigil (never a unicode box). Boundary check on the
    mode-selection branch: the ascii title differs from the unicode one and
    still matches the shared-helper ascii expectation.
    """

    async def body() -> None:
        app = EaApp(scope="workspace", state_path=_WORKSPACE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, WorkspaceScreen)
            assert app.state is not None
            assert app.state.workspace is not None
            repo_path = app.state.workspace.repos[_FIXTURE_REPO].path
            app.render_mode = "ascii"
            await pilot.pause()
            ascii_title = screen._quadrant_title(_FIXTURE_REPO, repo_path)
            assert ascii_title == _expected_title(_FIXTURE_REPO, repo_path, mode="ascii")
            app.render_mode = "unicode"
            await pilot.pause()
            unicode_title = screen._quadrant_title(_FIXTURE_REPO, repo_path)
            assert ascii_title != unicode_title

    asyncio.run(body())


def test_zoom_render_mode_falls_back_under_bare_harness() -> None:
    """``_zoom_render_mode`` defaults to the unicode mode off a host App.

    Error-path / boundary cover for the mode resolver: a bare object with no
    ``render_mode`` attribute (the off-app harness) must resolve the shared
    default rather than raise ``AttributeError``, so the title helper still
    renders a glyph column when no App is mounted.
    """
    from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE

    mixin = RepoZoomMixin()
    mixin.app = object()  # type: ignore[assignment]
    assert mixin._zoom_render_mode() == DEFAULT_RENDER_MODE


def test_zoom_quadrant_shares_one_accent_dim_focus_tint_workspace() -> None:
    """Exactly one workspace quadrant pane wears the ``-focused`` accent-dim tint.

    The criterion's focus half: the quadrant panes share ONE accent-dim focus
    tint with the parent table -- the global ``.pane.-focused`` rule lifts the
    pane owning keyboard focus to the bright ``$primary`` border while the rest
    stay the dim ``$accent`` border. On zoom the roadmap pane holds focus, so it
    (and only it) carries ``-focused``; the four panes are the same ``.pane``
    class the parent ``#pane-workspace`` table wears, so the tint is shared.
    """

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
            await pilot.pause()
            assert screen.zoomed
            panes = list(screen.query("#zoom-quadrant .pane"))
            assert len(panes) == 4
            focused_panes = [p for p in panes if p.has_class("-focused")]
            assert len(focused_panes) == 1
            # The lit pane is the one that owns the focused widget.
            assert app.focused is not None
            assert app.focused in focused_panes[0].walk_children(with_self=True)
            # The parent table pane wears the same .pane class the tint keys on.
            assert screen.query_one("#pane-workspace").has_class("pane")

    asyncio.run(body())


def test_zoom_quadrant_shares_one_accent_dim_focus_tint_portfolio() -> None:
    """The portfolio (user) scope shares the identical one-pane focus tint.

    The criterion's "from either scope" half: zooming a repo from the user
    portfolio table mounts the same quadrant and lights exactly one pane via the
    same shared ``.pane.-focused`` accent-dim rule, proving the tint is one
    vocabulary across both hosts (the user scope reuses the mixin verbatim).
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
            await pilot.pause()
            assert screen.zoomed
            panes = list(screen.query("#zoom-quadrant .pane"))
            assert len(panes) == 4
            focused_panes = [p for p in panes if p.has_class("-focused")]
            assert len(focused_panes) == 1
            assert app.focused is not None
            assert app.focused in focused_panes[0].walk_children(with_self=True)
            assert screen.query_one("#pane-portfolio").has_class("pane")

    asyncio.run(body())


def test_zoom_focus_tint_follows_focus_movement() -> None:
    """Moving focus across quadrant panes moves the single accent-dim tint.

    The focus tint is live, not a one-shot mount paint: when the operator tabs
    focus from the roadmap pane to another quadrant widget, the ``-focused``
    border follows so still exactly one pane is lit -- the
    ``on_descendant_focus`` repaint keeps the shared tint in lockstep with the
    real focus, matching the parent table's moving row highlight.
    """

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
            await pilot.pause()
            assert screen.zoomed
            roadmap_pane = next(
                p
                for p in screen.query("#zoom-quadrant .pane")
                if screen.query_one("#zoom-roadmap") in p.walk_children(with_self=True)
            )
            assert roadmap_pane.has_class("-focused")
            # Move focus onto the backlog table (a different quadrant pane).
            screen.query_one("#zoom-backlog").focus()
            await pilot.pause()
            backlog_pane = next(
                p
                for p in screen.query("#zoom-quadrant .pane")
                if screen.query_one("#zoom-backlog") in p.walk_children(with_self=True)
            )
            lit = [p for p in screen.query("#zoom-quadrant .pane") if p.has_class("-focused")]
            assert lit == [backlog_pane]
            assert not roadmap_pane.has_class("-focused")

    asyncio.run(body())
