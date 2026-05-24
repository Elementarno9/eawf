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
  repo's own ``state.json``. ``Enter`` zooms; ``Esc`` returns.

The zoom lifecycle (mount on zoom, unmount on exit, re-zoom reloads the
current focus) lives on the shared
:class:`~eawf.tui.scopes._zoom.RepoZoomMixin`, mixed into both this scope
and the user portfolio scope so neither duplicates it.

This screen overrides **only** :meth:`compose_body` plus its scope
bindings + footer hints; the entire chassis is inherited from
:class:`~eawf.tui.scopes.ScopeScreen` (zero-duplication).
"""

from __future__ import annotations

import logging
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Vertical
from textual.widgets import Static

from eawf.tui.scopes import ScopeScreen
from eawf.tui.scopes._zoom import RepoZoomMixin
from eawf.tui.widgets.workspace_table import WorkspaceTable

logger = logging.getLogger(__name__)

#: Footer hints for the table-browse mode (arrows primary; Enter zooms).
_WORKSPACE_HINTS: tuple[str, ...] = (
    "↑↓ row",
    "Enter zoom",
    "w/r/u scope",
    "c config",
    "F5 refresh",
    "/ palette",
    "? help",
    "q quit",
)


class WorkspaceScreen(ScopeScreen, RepoZoomMixin):
    """Workspace-scope screen: per-repo table with zoom-on-Enter.

    Composes a :class:`WorkspaceTable` in table-browse mode and mounts a
    :class:`~eawf.tui.widgets.roadmap_tree.RoadmapTree` ·
    :class:`~eawf.tui.widgets.status_pane.StatusPane` /
    :class:`~eawf.tui.widgets.git_pane.GitPane` ·
    :class:`~eawf.tui.widgets.backlog_table.BacklogTable` quadrant scoped
    to the focused repo on zoom (the shared
    :class:`~eawf.tui.scopes._zoom.RepoZoomMixin`).
    """

    #: ``Enter`` zooms the focused row (via the table's ``RowZoomed``
    #: message); ``Esc`` returns from the zoom quadrant to the table; ``c``
    #: opens the registry-driven config window via the shared
    #: ``action_open_config`` on the base chassis. The chassis already
    #: binds ``Esc`` to quit, so the zoom-aware ``escape`` here takes
    #: precedence on this screen and only quits when not zoomed.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "leave_zoom", "back", show=False),
        Binding("c", "open_config", "config", show=False),
    ]

    FOOTER_HINTS: ClassVar[tuple[str, ...]] = _WORKSPACE_HINTS

    def compose_body(self) -> ComposeResult:
        """Yield the table-browse body + an (initially empty) zoom mount."""
        with Vertical(id="body"):
            with Vertical(classes="pane", id="pane-repos"):
                yield Static("WORKSPACE", classes="pane-title")
                yield WorkspaceTable(id="workspace-table")
            yield Container(id="zoom-mount")


__all__ = ["WorkspaceScreen"]
