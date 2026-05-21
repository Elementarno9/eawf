"""``WorkspaceScreen`` — workspace-scope strip + zoom screen.

The workspace screen composes a top **strip** (one summary row per
linked repo) above a **zoom** quadrant scoped to the focused repo,
inside the :class:`~eawf.tui_v2.scopes.ScopeScreen` shared chassis
(Header + Footer + Heartbeat reused verbatim).

The dedicated strip/zoom sub-widgets the brief names
(``WorkspaceTopStrip`` — a per-repo ``DataTable`` with expansion-on-focus
— and ``RepoQuadrant`` — a reusable repo 2x2 sub-widget) land in a later
wave of this band. This wave composes the **available** W17 widget
catalog in the strip+zoom arrangement so the screen renders live today:

* top strip — :class:`~eawf.tui_v2.widgets.status_pane.StatusPane`
  (workspace lifecycle summary), the seam the per-repo strip rows hang
  off;
* zoom quadrant — :class:`~eawf.tui_v2.widgets.roadmap_tree.RoadmapTree`
  beside :class:`~eawf.tui_v2.widgets.git_pane.GitPane` +
  :class:`~eawf.tui_v2.widgets.backlog_table.BacklogTable`, scoped to the
  bound workspace state.

This screen overrides **only** :meth:`compose_body` plus its scope
bindings + footer hints; the entire chassis is inherited from
:class:`~eawf.tui_v2.scopes.ScopeScreen` (zero-duplication).
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from eawf.tui_v2.scopes import ScopeScreen
from eawf.tui_v2.widgets.backlog_table import BacklogTable
from eawf.tui_v2.widgets.git_pane import GitPane
from eawf.tui_v2.widgets.roadmap_tree import RoadmapTree
from eawf.tui_v2.widgets.status_pane import StatusPane

#: Footer hints tuned for the strip+zoom workspace screen.
_WORKSPACE_HINTS: tuple[str, ...] = (
    "↑↓ row",
    "z zoom",
    "Enter open",
    "w/r/u scope",
    "F5 refresh",
    "/ palette",
    "? help",
    "q quit",
)


class WorkspaceScreen(ScopeScreen):
    """Workspace-scope screen: top strip over an active-repo quadrant.

    Composes the workspace :class:`StatusPane` strip above a
    :class:`RoadmapTree` · :class:`GitPane` + :class:`BacklogTable` zoom
    quadrant, inside the shared chassis.
    """

    #: ``z`` zooms the focused strip row into the quadrant (the focus →
    #: reload wiring lands with the ``WorkspaceTopStrip`` sub-widget in a
    #: later wave); declared here as the navigation seam.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("z", "zoom_focused", "zoom", show=False),
    ]

    FOOTER_HINTS: ClassVar[tuple[str, ...]] = _WORKSPACE_HINTS

    def compose_body(self) -> ComposeResult:
        """Yield the strip (top) + zoom quadrant (bottom) body."""
        with Vertical(id="body"):
            with Vertical(classes="pane", id="pane-strip"):
                yield Static("WORKSPACE", classes="pane-title")
                yield StatusPane(id="strip-status")
            with Horizontal(classes="row", id="zoom"):
                with Vertical(classes="pane", id="pane-roadmap"):
                    yield Static("ROADMAP", classes="pane-title")
                    yield RoadmapTree(id="roadmap-tree")
                with Vertical(classes="pane", id="pane-git"):
                    yield Static("GIT", classes="pane-title")
                    yield GitPane(id="git-pane")
                with Vertical(classes="pane", id="pane-backlog"):
                    yield Static("BACKLOG", classes="pane-title")
                    yield BacklogTable(id="backlog-table")


__all__ = ["WorkspaceScreen"]
