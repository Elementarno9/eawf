"""``WorkspaceScreen`` — workspace-scope per-repo table + zoom screen.

The workspace screen has two modes inside the shared
:class:`~eawf.tui.scopes.ScopeScreen` chassis (Header + Footer +
Heartbeat reused verbatim):

* **table_browse** — a full-screen per-repo
  :class:`~eawf.tui.widgets.workspace_table.WorkspaceTable` (one row per
  linked repo, **always at least one** — never a fallback panel). ``↑↓``
  focus a repo; the git column refreshes on the host's refresh tick and
  dims to ``git?`` on a probe failure.
* **zoom** — the focused repo's 2x2 quadrant (roadmap · status / git ·
  backlog), reusing the repo-scope widget catalog scoped to the focused
  repo's own ``state.json``. ``Enter`` / ``z`` zooms; ``Esc`` returns.

The quadrant is **mounted on zoom and unmounted on exit** so a re-zoom
always reloads against the current focused row (not a cached target) and
a mid-probe Esc discards the stale result cleanly (the unmounted git
pane's worker result is dropped).

This screen overrides **only** :meth:`compose_body` plus its scope
bindings + footer hints; the entire chassis is inherited from
:class:`~eawf.tui.scopes.ScopeScreen` (zero-duplication).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Static

from eawf.tui.scopes import ScopeScreen
from eawf.tui.state_binding import load_state
from eawf.tui.widgets.backlog_table import BacklogTable
from eawf.tui.widgets.git_pane import GitPane
from eawf.tui.widgets.roadmap_tree import RoadmapTree
from eawf.tui.widgets.status_pane import StatusPane
from eawf.tui.widgets.workspace_table import WorkspaceTable

if TYPE_CHECKING:
    from eawf.state.models import State, WorkspaceRepoRef

logger = logging.getLogger(__name__)

#: Footer hints for the table-browse mode (arrows primary; z is an alias).
_WORKSPACE_HINTS: tuple[str, ...] = (
    "↑↓ row",
    "Enter zoom",
    "z zoom",
    "w/r/u scope",
    "c config",
    "F5 refresh",
    "/ palette",
    "? help",
    "q quit",
)


class WorkspaceScreen(ScopeScreen):
    """Workspace-scope screen: per-repo table with zoom-on-Enter.

    Composes a :class:`WorkspaceTable` in table-browse mode and mounts a
    :class:`RoadmapTree` · :class:`StatusPane` / :class:`GitPane` ·
    :class:`BacklogTable` quadrant scoped to the focused repo on zoom.
    """

    #: ``z`` zooms the focused row (the Enter alias); ``Esc`` returns from
    #: the zoom quadrant to the table; ``c`` opens the registry-driven
    #: config window via the shared ``action_open_config`` on the base
    #: chassis. The chassis already binds ``Esc`` to quit, so the
    #: zoom-aware ``escape`` here takes precedence on this screen and only
    #: quits when not zoomed.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("z", "zoom_focused", "zoom", show=False),
        Binding("escape", "leave_zoom", "back", show=False),
        Binding("c", "open_config", "config", show=False),
    ]

    FOOTER_HINTS: ClassVar[tuple[str, ...]] = _WORKSPACE_HINTS

    def __init__(self, **kwargs: object) -> None:
        """Construct the screen in table-browse mode (no quadrant mounted)."""
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._zoomed_code: str | None = None

    def compose_body(self) -> ComposeResult:
        """Yield the table-browse body + an (initially empty) zoom mount."""
        with Vertical(id="body"):
            with Vertical(classes="pane", id="pane-repos"):
                yield Static("WORKSPACE", classes="pane-title")
                yield WorkspaceTable(id="workspace-table")
            yield Container(id="zoom-mount")

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
        self.query_one("#pane-repos", Vertical).display = False
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
        self.query_one("#pane-repos", Vertical).display = True
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


__all__ = ["WorkspaceScreen"]
