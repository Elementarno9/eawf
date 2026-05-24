"""``RepoZoomMixin`` — shared focused-repo zoom machinery for scope screens.

Both the workspace scope and the user (portfolio) scope render a per-repo
:class:`~eawf.tui.widgets.workspace_table.WorkspaceTable` and zoom the
focused row into a 2x2 quadrant (roadmap · status / git · backlog) scoped
to that repo's own ``state.json``. This mixin holds the lift-and-shift of
that zoom lifecycle so neither host duplicates it:

* ``Enter`` (the table's ``RowZoomed`` message) and ``z``
  (``action_zoom_focused``) mount a fresh quadrant scoped to the focused
  repo; ``Esc`` (``action_leave_zoom``) unmounts it and restores the
  browse pane (falling through to app-quit when not zoomed).
* The quadrant is **mounted on zoom and unmounted on exit** so a re-zoom
  always reloads against the current focus and a mid-probe Esc discards
  the stale git result cleanly.

The host parameterises only its browse-pane selector via
:attr:`ZOOM_BROWSE_PANE` (workspace ``#pane-repos``; user
``#pane-portfolio``); everything else is shared. Because
:class:`~eawf.tui.scopes.user.PortfolioTable` subclasses
:class:`WorkspaceTable`, the ``WorkspaceTable.RowZoomed`` handler and the
``query_one(WorkspaceTable)`` focus lookup resolve for both scopes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Static

from eawf.tui.state_binding import load_state
from eawf.tui.widgets.backlog_table import BacklogTable
from eawf.tui.widgets.git_pane import GitPane
from eawf.tui.widgets.roadmap_tree import RoadmapTree
from eawf.tui.widgets.status_pane import StatusPane
from eawf.tui.widgets.workspace_table import WorkspaceTable

if TYPE_CHECKING:
    from textual.screen import Screen

    from eawf.state.models import State, WorkspaceRepoRef

    _Base = Screen[None]
else:
    _Base = object

logger = logging.getLogger(__name__)


class RepoZoomMixin(_Base):
    """Focused-repo zoom lifecycle shared by the workspace + user scopes.

    Mixed into a :class:`~eawf.tui.scopes.ScopeScreen` subclass whose body
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

    @property
    def zoomed(self) -> bool:
        """``True`` while a focused-repo quadrant is mounted."""
        return self._zoomed_code is not None

    def on_workspace_table_row_zoomed(self, message: WorkspaceTable.RowZoomed) -> None:
        """Zoom the focused repo into a 2x2 quadrant (Enter handler).

        Args:
            message: The :class:`WorkspaceTable.RowZoomed` message carrying
                the repo code to scope the quadrant to.
        """
        self._enter_zoom(message.repo_code)

    def action_zoom_focused(self) -> None:
        """Zoom the focused row (the ``z`` binding).

        Re-reads the table's current focus so a ``z`` after an ``↑↓`` move
        zooms the row under the cursor, not a stale target.
        """
        table = self.query_one(WorkspaceTable)
        repo_code = table.focused_repo()
        if repo_code is not None:
            self._enter_zoom(repo_code)

    async def action_leave_zoom(self) -> None:
        """Return to the table from the zoom quadrant (the ``Esc`` binding).

        Unmounts the quadrant (discarding any in-flight git probe) and
        restores the table. When not zoomed this falls through to the
        chassis quit so ``Esc`` still exits the app from table-browse.
        """
        if not self.zoomed:
            await self.app.action_quit()
            return
        self._exit_zoom()

    def _enter_zoom(self, repo_code: str) -> None:
        """Mount a fresh quadrant scoped to *repo_code*.

        Any existing quadrant is unmounted first so a re-zoom always
        reloads against the current focus rather than reusing a cached
        mount. The focused repo's own ``state.json`` is loaded read-only
        and fed into the quadrant widgets; the git pane probes the repo's
        path directly.

        Args:
            repo_code: The repo code to scope the quadrant to.
        """
        self._clear_zoom_mount()
        ref = self._repo_ref(repo_code)
        if ref is None:
            logger.info(f"_enter_zoom unknown repo_code={repo_code!r}")
            return
        repo_path = Path(ref.path)
        repo_state = load_state(repo_path / ".ea" / "state.json")
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
        self.call_after_refresh(self._seed_quadrant, repo_state)

    def _seed_quadrant(self, repo_state: State | None) -> None:
        """Assign the focused repo's state into the mounted quadrant widgets.

        Args:
            repo_state: The loaded focused-repo state, or ``None`` when the
                repo's ``state.json`` was missing / unreadable.
        """
        for widget_type in (RoadmapTree, StatusPane, BacklogTable):
            for widget in self.query(f"#zoom-quadrant {widget_type.__name__}"):
                widget.state = repo_state  # type: ignore[attr-defined]

    def _exit_zoom(self) -> None:
        """Unmount the quadrant and restore the table-browse view."""
        self._clear_zoom_mount()
        self.query_one(self.ZOOM_BROWSE_PANE, Vertical).display = True
        self._zoomed_code = None

    def _clear_zoom_mount(self) -> None:
        """Remove any mounted quadrant (discards its in-flight git probe)."""
        for child in self.query("#zoom-quadrant"):
            child.remove()

    def _repo_ref(self, repo_code: str) -> WorkspaceRepoRef | None:
        """Resolve *repo_code* to its workspace repo ref, or ``None``.

        Args:
            repo_code: The repo code to look up in the bound workspace
                index.

        Returns:
            The :class:`~eawf.state.models.WorkspaceRepoRef`, or ``None``
            when the bound state has no such repo.
        """
        state: State | None = getattr(self.app, "state", None)
        if state is None or state.workspace is None:
            return None
        return state.workspace.repos.get(repo_code)


__all__ = ["RepoZoomMixin"]
