"""``RepoScreen`` — repo-scope 2x2 quadrant screen.

The repo screen composes the widget catalog into a two-row, two-column
quadrant body inside the :class:`~eawf.tui_v2.scopes.ScopeScreen` shared
chassis (Header + Footer + Heartbeat reused verbatim):

* top-left  — :class:`~eawf.tui_v2.widgets.roadmap_tree.RoadmapTree`
* top-right — :class:`~eawf.tui_v2.widgets.status_pane.StatusPane`
* bottom-left  — :class:`~eawf.tui_v2.widgets.git_pane.GitPane`
* bottom-right — :class:`~eawf.tui_v2.widgets.backlog_table.BacklogTable`

Each quadrant pane is a bordered :class:`~textual.containers.Vertical`
carrying a bold-accent title cell + the composed widget; the panes flex
equally (``1fr`` by ``1fr``). The widgets self-bind to the App's reactive
``state`` (each W17 widget seeds from ``app.state`` on mount), so this
screen only declares the layout — it holds no per-widget state plumbing.

This screen overrides **only** :meth:`compose_body`; the brand,
breadcrumb, runtime cell, clock, heartbeat, and quit/help/palette
bindings are inherited from :class:`~eawf.tui_v2.scopes.ScopeScreen`
(zero-duplication chassis). The ``w`` wave-board / ``c`` config bindings
the brief lists for this screen target screens that land in later waves;
they are added here as the navigation seam those waves wire up.
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

#: Footer hints tuned for the repo quadrant (full key names).
_REPO_HINTS: tuple[str, ...] = (
    "↑↓ move",
    "←→ collapse",
    "Enter open",
    "/ palette",
    "? help",
    "q quit",
)


class RepoScreen(ScopeScreen):
    """Repo-scope screen: a 2x2 quadrant of the core widgets.

    Composes :class:`RoadmapTree` · :class:`StatusPane` /
    :class:`GitPane` · :class:`BacklogTable` inside the shared chassis.
    """

    #: Scope-switch + sub-screen bindings layered on the chassis chrome.
    #: ``w`` / ``c`` target the wave-board / config screens that land in
    #: later waves; declared here as the navigation seam.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("w", "open_wave_board", "wave board", show=False),
        Binding("c", "open_config", "config", show=False),
    ]

    FOOTER_HINTS: ClassVar[tuple[str, ...]] = _REPO_HINTS

    def compose_body(self) -> ComposeResult:
        """Yield the 2x2 quadrant body (two rows of two bordered panes)."""
        with Vertical(id="body"):
            with Horizontal(classes="row"):
                with Vertical(classes="pane", id="pane-roadmap"):
                    yield Static("ROADMAP", classes="pane-title")
                    yield RoadmapTree(id="roadmap-tree")
                with Vertical(classes="pane", id="pane-status"):
                    yield Static("STATUS", classes="pane-title")
                    yield StatusPane(id="status-pane")
            with Horizontal(classes="row"):
                with Vertical(classes="pane", id="pane-git"):
                    yield Static("GIT", classes="pane-title")
                    yield GitPane(id="git-pane")
                with Vertical(classes="pane", id="pane-backlog"):
                    yield Static("BACKLOG", classes="pane-title")
                    yield BacklogTable(id="backlog-table")


__all__ = ["RepoScreen"]
