"""``RepoScreen`` — repo-scope 2x2 quadrant screen.

The repo screen leads its body with the Home attention band (the
orthogonal Home-overview strip; see
:func:`~eawf.surfaces.tui.scopes.attention_band`) then composes the widget
catalog into a two-row, two-column quadrant body inside the
:class:`~eawf.surfaces.tui.scopes.ScopeScreen` shared chassis (Header + Footer +
Heartbeat reused verbatim):

* top-left  — :class:`~eawf.surfaces.tui.widgets.roadmap_tree.RoadmapTree`
* top-right — :class:`~eawf.surfaces.tui.widgets.status_pane.StatusPane`
* bottom-left  — :class:`~eawf.surfaces.tui.widgets.git_pane.GitPane`
* bottom-right — :class:`~eawf.surfaces.tui.widgets.backlog_table.BacklogTable`

Each quadrant pane is a bordered :class:`~textual.containers.Vertical`
carrying a bold-accent title cell + the composed widget; the panes flex
equally (``1fr`` by ``1fr``). The widgets self-bind to the App's reactive
``state`` (each W17 widget seeds from ``app.state`` on mount), so this
screen only declares the layout — it holds no per-widget state plumbing.

This screen overrides **only** :meth:`compose_body`; the brand,
breadcrumb, runtime cell, clock, heartbeat, and quit/help/palette
bindings are inherited from :class:`~eawf.surfaces.tui.scopes.ScopeScreen`
(zero-duplication chassis). The ``c`` binding opens the registry-driven
:class:`~eawf.surfaces.tui.screens.overlays.config_modal.ConfigModal` via the
shared ``action_open_config`` on the base chassis.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from eawf.surfaces.tui.scopes import ScopeScreen, attention_band
from eawf.surfaces.tui.widgets.backlog_table import BacklogTable
from eawf.surfaces.tui.widgets.footer import render_hint_label
from eawf.surfaces.tui.widgets.git_pane import GitPane
from eawf.surfaces.tui.widgets.roadmap_tree import RoadmapTree
from eawf.surfaces.tui.widgets.status_pane import StatusPane

#: Footer hints tuned for the repo quadrant. Every label is produced through
#: :func:`~eawf.surfaces.tui.widgets.footer.render_hint_label` so the key
#: tokens stay pinned to the canonical vocabulary.
_REPO_HINTS: tuple[str, ...] = (
    render_hint_label("↑↓", "move"),
    render_hint_label("←→", "collapse"),
    render_hint_label("Enter", "open"),
    render_hint_label("w/r/u", "scope"),
    render_hint_label("c", "config"),
    render_hint_label("/", "palette"),
    render_hint_label("?", "help"),
    render_hint_label("q", "quit"),
)


class RepoScreen(ScopeScreen):
    """Repo-scope screen: a 2x2 quadrant of the core widgets.

    Composes :class:`RoadmapTree` · :class:`StatusPane` /
    :class:`GitPane` · :class:`BacklogTable` inside the shared chassis.
    """

    #: Sub-screen bindings layered on the chassis chrome. ``c`` opens the
    #: registry-driven config window through the shared
    #: ``action_open_config`` on the base chassis. (Raw ``w`` is the
    #: app-wide workspace scope-switch, so no wave-board binding lives
    #: here.)
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("c", "open_config", "config", show=False),
    ]

    FOOTER_HINTS: ClassVar[tuple[str, ...]] = _REPO_HINTS

    def compose_body(self) -> ComposeResult:
        """Yield the Home attention band, then the 2x2 quadrant body."""
        with Vertical(id="body"):
            yield from attention_band()
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
