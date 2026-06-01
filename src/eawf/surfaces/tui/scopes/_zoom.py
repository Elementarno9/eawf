"""``RepoZoomMixin`` — shared focused-repo zoom machinery for scope screens.

Both the workspace scope and the user (portfolio) scope render a per-repo
:class:`~eawf.surfaces.tui.widgets.workspace_table.WorkspaceTable` and zoom the
focused row into a 2x2 quadrant (roadmap · status / git · backlog) scoped
to that repo's own ``state.json``. This mixin holds the lift-and-shift of
that zoom lifecycle so neither host duplicates it:

* ``Enter`` (the table's ``RowZoomed`` message) mounts a fresh quadrant
  scoped to the focused repo; ``Esc`` (``action_leave_zoom``) unmounts it
  and restores the browse pane (falling through to app-quit when not
  zoomed).
* The quadrant is **mounted on zoom and unmounted on exit** so a re-zoom
  always reloads against the current focus and a mid-probe Esc discards
  the stale git result cleanly. The mount path **awaits** the prior
  unmount before re-mounting, so a tight Esc-then-Enter (or any re-zoom)
  never inserts a second ``id="zoom-quadrant"`` before the first is
  pruned (which would raise ``DuplicateIds``).
* :meth:`on_screen_suspend` tears the quadrant down whenever the screen
  is suspended, but distinguishes two suspends via the shared
  :meth:`_suspend_is_transient` guard. A **real switch-away** (the screen
  has been popped off ``app.screen_stack``) clears the zoom outright, so
  a cached scope screen (Textual reuses named ``SCREENS`` instances)
  never carries a stale quadrant back when the operator returns to it. A
  **transient suspend** (a modal pushed on top — the screen stays on the
  stack) tears the quadrant down too (so no hidden git probe keeps
  running and the cached-screen invariant still holds) **but remembers
  the focused repo**, and :meth:`on_screen_resume` rebuilds the quadrant
  to that repo when the modal dismisses. Without the guard, opening the
  ``c`` config window (or ``?`` help, ``/`` palette) over a zoomed scope
  destroyed the zoom irrecoverably.

The host parameterises only its browse-pane selector via
:attr:`ZOOM_BROWSE_PANE` (workspace ``#pane-repos``; user
``#pane-portfolio``); everything else is shared. Because
:class:`~eawf.surfaces.tui.scopes.user.PortfolioTable` subclasses
:class:`WorkspaceTable`, the ``WorkspaceTable.RowZoomed`` handler and the
``query_one(WorkspaceTable)`` focus lookup resolve for both scopes.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Static

from eawf.surfaces.tui.state_binding import load_state
from eawf.surfaces.tui.widgets.backlog_table import BacklogTable
from eawf.surfaces.tui.widgets.git_pane import GitPane
from eawf.surfaces.tui.widgets.roadmap_tree import RoadmapTree
from eawf.surfaces.tui.widgets.status_pane import StatusPane
from eawf.surfaces.tui.widgets.workspace_table import WorkspaceTable

if TYPE_CHECKING:
    from textual.screen import Screen

    from eawf.kernel.state.models import State, WorkspaceRepoRef

    _Base = Screen[None]
else:
    _Base = object

logger = logging.getLogger(__name__)


class RepoZoomMixin(_Base):
    """Focused-repo zoom lifecycle shared by the workspace + user scopes.

    Mixed into a :class:`~eawf.surfaces.tui.scopes.ScopeScreen` subclass whose body
    composes a :class:`WorkspaceTable` inside a browse pane plus a sibling
    ``#zoom-mount`` :class:`~textual.containers.Container`. The host sets
    :attr:`ZOOM_BROWSE_PANE` to the id of its browse pane so the mixin
    hides / restores the right pane on zoom / exit.
    """

    #: The browse-pane selector the host hides while a quadrant is mounted
    #: and restores on exit. Workspace keeps the default; the user scope
    #: overrides it to ``#pane-portfolio``.
    ZOOM_BROWSE_PANE: ClassVar[str] = "#pane-repos"

    #: The currently-zoomed repo code, or ``None`` in table-browse mode.
    #: A class attribute so neither host needs an ``__init__`` to seed it.
    _zoomed_code: str | None = None

    #: The repo code to rebuild the quadrant against on the next
    #: ``ScreenResume`` after a transient (modal-push) suspend, or ``None``
    #: when there is no pending rebuild. Class attribute so neither host
    #: needs an ``__init__`` to seed it.
    _resume_code: str | None = None

    @property
    def zoomed(self) -> bool:
        """``True`` while a focused-repo quadrant is mounted."""
        return self._zoomed_code is not None

    async def on_workspace_table_row_zoomed(self, message: WorkspaceTable.RowZoomed) -> None:
        """Zoom the focused repo into a 2x2 quadrant (Enter handler).

        Async so the mount path can await the prior quadrant's unmount
        before re-mounting (Textual supports async message handlers).

        Args:
            message: The :class:`WorkspaceTable.RowZoomed` message carrying
                the repo code to scope the quadrant to.
        """
        await self._enter_zoom(message.repo_code)

    async def action_leave_zoom(self) -> None:
        """Return to the table from the zoom quadrant (the ``Esc`` binding).

        Unmounts the quadrant (discarding any in-flight git probe) and
        restores the table. When not zoomed this falls through to the
        chassis quit so ``Esc`` still exits the app from table-browse.
        """
        # An explicit leave cancels any pending modal-resume rebuild.
        self._resume_code = None
        if not self.zoomed:
            await self.app.action_quit()
            return
        await self._exit_zoom()

    def _suspend_is_transient(self) -> bool:
        """Return whether the current suspend is transient, not a switch-away.

        Textual posts ``ScreenSuspend`` to this screen in three cases the
        event itself cannot tell apart:

        * ``App.push_screen`` (a modal opens on top) suspends the active
          screen while leaving it **on** its mode's stack.
        * ``App.switch_mode`` (a digit-key mode switch) suspends this
          screen but leaves it on **its own** mode's stack (only the
          *current* mode pointer moves).
        * ``App.switch_screen`` (a scope switch) pops this screen **off**
          its mode's stack first, then suspends it.

        The first two are transient (the screen is coming back -- a modal
        will dismiss, or the operator will switch the mode back), so zoom
        is preserved + rebuilt; the third is a real switch-away, so zoom
        resets. The discriminator is therefore membership across **all**
        mode stacks, not just the current mode's: a modal-push and a
        mode-switch both leave ``self`` in some mode's stack, while a
        scope ``switch_screen`` removes it entirely. (Reading only the
        current mode's ``app.screen_stack`` would misclassify a mode switch
        as a switch-away, because by the time the suspend message is pumped
        the current-mode pointer has already moved to the new mode.) The
        guard is the single place both :meth:`on_screen_suspend` and
        :meth:`on_screen_resume` consult so the transient-vs-real policy
        lives in one helper.

        Returns:
            ``True`` when a modal was pushed or the mode was switched while
            this screen stays on some mode's stack, ``False`` on a real
            scope switch-away (or when the app exposes no screen stacks,
            e.g. a bare test harness).
        """
        stacks = getattr(self.app, "_screen_stacks", None)
        if isinstance(stacks, dict):
            return any(self in stack for stack in stacks.values())
        # Bare harness without mode stacks: fall back to the single current
        # stack so standalone screen tests still resolve transient vs real.
        return self in getattr(self.app, "screen_stack", ())

    async def on_screen_suspend(self) -> None:
        """Tear the quadrant down on suspend; remember it for a modal rebuild.

        Textual reuses named ``SCREENS`` instances (a scope switch
        suspends the current screen rather than destroying it), so without
        a teardown a screen zoomed before the switch would carry its stale
        quadrant — and a hidden browse pane — back when the operator
        returns. The quadrant is therefore unmounted on every suspend.

        A **transient** suspend (a modal pushed on top, per
        :meth:`_suspend_is_transient`) stashes the focused repo code into
        :attr:`_resume_code` first, so :meth:`on_screen_resume` can rebuild
        the same quadrant when the modal dismisses. A real switch-away
        leaves :attr:`_resume_code` ``None`` so the cached screen returns
        in table-browse. A no-op when not zoomed.
        """
        if not self.zoomed:
            return
        if self._suspend_is_transient():
            self._resume_code = self._zoomed_code
        await self._exit_zoom()

    async def on_screen_resume(self) -> None:
        """Rebuild the quadrant after a transient (modal-push) suspend.

        When :meth:`on_screen_suspend` stashed a :attr:`_resume_code` (a
        modal had been pushed over a zoomed screen), the dismissing modal
        resumes this screen — rebuild the quadrant to that repo so the
        zoom survives the modal round-trip. The pending code is cleared
        first so a later switch-away does not spuriously re-zoom, and the
        rebuild routes through :meth:`_enter_zoom`, whose await-the-unmount
        path keeps the rebuild idempotent (never two ``#zoom-quadrant``).
        A no-op when there is no pending rebuild (a switch-back, or a
        modal dismissed over a non-zoomed screen).
        """
        code = self._resume_code
        self._resume_code = None
        if code is not None and not self.zoomed:
            await self._enter_zoom(code)

    async def _enter_zoom(self, repo_code: str) -> None:
        """Mount a fresh quadrant scoped to *repo_code*.

        Any existing quadrant is unmounted first — and the unmount is
        **awaited** — so a re-zoom always reloads against the current
        focus and never races a not-yet-pruned quadrant into a
        ``DuplicateIds`` on the re-mount. The focused repo's own
        ``state.json`` is loaded read-only and fed into the quadrant
        widgets; the git pane probes the repo's path directly.

        Args:
            repo_code: The repo code to scope the quadrant to.
        """
        # A fresh explicit zoom supersedes any pending modal-resume rebuild.
        self._resume_code = None
        await self._clear_zoom_mount()
        ref = self._repo_ref(repo_code)
        if ref is None:
            logger.info(f"_enter_zoom unknown repo_code={repo_code!r}")
            return
        repo_path = Path(ref.path)
        repo_state = load_state(repo_path / ".ea" / "state.json")
        cast(Any, self.app)._active_repo_path = repo_path
        mount = self.query_one("#zoom-mount", Container)
        roadmap = RoadmapTree(id="zoom-roadmap")
        status = StatusPane(id="zoom-status")
        git = GitPane(id="zoom-git", cwd=repo_path)
        backlog = BacklogTable(id="zoom-backlog")
        quadrant = Vertical(
            Static(f"REPO · {repo_code}", classes="pane-title"),
            Horizontal(
                Vertical(Static("ROADMAP", classes="pane-title"), roadmap, classes="pane"),
                Vertical(Static("STATUS", classes="pane-title"), status, classes="pane"),
                classes="row",
            ),
            Horizontal(
                Vertical(Static("GIT", classes="pane-title"), git, classes="pane"),
                Vertical(Static("BACKLOG", classes="pane-title"), backlog, classes="pane"),
                classes="row",
            ),
            id="zoom-quadrant",
        )
        mount.mount(quadrant)
        self.query_one(self.ZOOM_BROWSE_PANE, Vertical).display = False
        self._zoomed_code = repo_code
        # Feed the focused repo's own state into the quadrant widgets after
        # they mount: their seed-on-mount reads the app's workspace state,
        # not the focused repo's, so override it once the widgets exist.
        # Hiding the browse table above blurs the WorkspaceTable that had
        # focus, leaving focus unset; the same deferred pass moves focus onto
        # the quadrant's roadmap tree (its primary drill target) so Enter and
        # the arrow keys land in the zoomed view instead of nowhere.
        self.call_after_refresh(self._seed_quadrant, repo_state)
        self.call_after_refresh(self._focus_zoom_quadrant)

    def _seed_quadrant(self, repo_state: State | None) -> None:
        """Assign the focused repo's state into the mounted quadrant widgets.

        Args:
            repo_state: The loaded focused-repo state, or ``None`` when the
                repo's ``state.json`` was missing / unreadable.
        """
        for widget_type in (RoadmapTree, StatusPane, BacklogTable):
            for widget in self.query(f"#zoom-quadrant {widget_type.__name__}"):
                widget.state = repo_state  # type: ignore[attr-defined]

    def _focus_zoom_quadrant(self) -> None:
        """Move focus onto the zoomed quadrant's roadmap tree.

        Run after the quadrant mounts (the browse table that held focus is
        hidden on zoom, blurring it). The roadmap tree is the top-left drill
        target, mirroring the repo scope's natural Enter target, so the
        operator can navigate + drill the zoomed view immediately. A no-op
        when the tree is not (yet) mounted.
        """
        trees = list(self.query("#zoom-roadmap"))
        if trees:
            trees[0].focus()

    async def _exit_zoom(self) -> None:
        """Unmount the quadrant and restore the table-browse view.

        Unmounting the quadrant blurs whatever quadrant widget held focus,
        so focus is moved back onto the now-visible browse table -- otherwise
        the operator returns to the table with nothing focused and the arrow
        keys / Enter dead until a manual re-focus.
        """
        await self._clear_zoom_mount()
        self.query_one(self.ZOOM_BROWSE_PANE, Vertical).display = True
        self._zoomed_code = None
        cast(Any, self.app)._active_repo_path = None
        tables = list(self.query(WorkspaceTable))
        if tables:
            tables[0].focus()

    async def _clear_zoom_mount(self) -> None:
        """Remove any mounted quadrant, awaiting the prune to completion.

        Awaiting each ``remove()`` (a Textual ``AwaitComplete``) before the
        caller re-mounts guarantees the old ``id="zoom-quadrant"`` is gone
        from the ``#zoom-mount`` ``NodeList`` first, so the re-mount cannot
        trip ``DuplicateIds``. A no-op (empty gather) when nothing is
        mounted.
        """
        await asyncio.gather(*(child.remove() for child in self.query("#zoom-quadrant")))

    def _repo_ref(self, repo_code: str) -> WorkspaceRepoRef | None:
        """Resolve *repo_code* to its workspace repo ref, or ``None``.

        Args:
            repo_code: The repo code to look up in the bound workspace
                index.

        Returns:
            The :class:`~eawf.kernel.state.models.WorkspaceRepoRef`, or ``None``
            when the bound state has no such repo.
        """
        state: State | None = getattr(self.app, "state", None)
        if state is None or state.workspace is None:
            return None
        return state.workspace.repos.get(repo_code)


__all__ = ["RepoZoomMixin"]
