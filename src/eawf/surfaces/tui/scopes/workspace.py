"""``WorkspaceScreen`` — workspace-scope per-repo table + zoom screen.

The workspace screen has two modes inside the shared
:class:`~eawf.surfaces.tui.scopes.ScopeScreen` chassis (Header + Footer +
Heartbeat reused verbatim):

* **table_browse** — a per-repo
  :class:`~eawf.surfaces.tui.widgets.workspace_table.WorkspaceTable` (one row per
  linked repo, **always at least one** — never a fallback panel) over a
  read-only :class:`~eawf.surfaces.tui.widgets.registry_pane.RegistryPane`
  that lists the explicit ``~/.eawf/registry.json`` entries (code · title
  · path · ``(active)`` / ``(stale)`` chips). The pane reads ONLY the
  registry file — never a filesystem scan/walk — so the dashboard honours
  the explicit-registry-only rule (the registry grows solely via
  ``eawf init`` / ``eawf repo add``). ``↑↓`` focus a repo; the git column
  refreshes on the host's refresh tick and dims to ``git?`` on a probe
  failure.
* **zoom** — the focused repo's 2x2 quadrant (roadmap · status / git ·
  backlog), reusing the repo-scope widget catalog scoped to the focused
  repo's own ``state.json``. ``Enter`` zooms; ``Esc`` returns.

The zoom lifecycle (mount on zoom, unmount on exit, re-zoom reloads the
current focus) lives on the shared
:class:`~eawf.surfaces.tui.scopes._zoom.RepoZoomMixin`, mixed into both this scope
and the user portfolio scope so neither duplicates it.

This screen overrides **only** :meth:`compose_body` plus its scope
bindings + footer hints; the entire chassis is inherited from
:class:`~eawf.surfaces.tui.scopes.ScopeScreen` (zero-duplication).
"""

from __future__ import annotations

import logging
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Vertical
from textual.widgets import Static

from eawf.surfaces.tui.scopes import ScopeScreen, attention_band
from eawf.surfaces.tui.scopes._zoom import RepoZoomMixin
from eawf.surfaces.tui.widgets.footer import render_hint_label
from eawf.surfaces.tui.widgets.registry_pane import RegistryPane
from eawf.surfaces.tui.widgets.workspace_table import WorkspaceTable

logger = logging.getLogger(__name__)

#: Footer hints for the table-browse mode (arrows primary; Enter opens the
#: focused repo). Every label is produced through
#: :func:`~eawf.surfaces.tui.widgets.footer.render_hint_label` so the key
#: tokens AND the shared-token actions stay pinned to the canonical vocabulary.
_WORKSPACE_HINTS: tuple[str, ...] = (
    render_hint_label("↑↓", "select"),
    render_hint_label("Enter", "open"),
    render_hint_label("w/r/u", "scope"),
    render_hint_label("c", "config"),
    render_hint_label("F5", "refresh"),
    render_hint_label("/", "palette"),
    render_hint_label("?", "help"),
    render_hint_label("q", "quit"),
)


class WorkspaceScreen(ScopeScreen, RepoZoomMixin):
    """Workspace-scope screen: per-repo table with zoom-on-Enter.

    Composes a :class:`WorkspaceTable` in table-browse mode and mounts a
    :class:`~eawf.surfaces.tui.widgets.roadmap_tree.RoadmapTree` ·
    :class:`~eawf.surfaces.tui.widgets.status_pane.StatusPane` /
    :class:`~eawf.surfaces.tui.widgets.git_pane.GitPane` ·
    :class:`~eawf.surfaces.tui.widgets.backlog_table.BacklogTable` quadrant scoped
    to the focused repo on zoom (the shared
    :class:`~eawf.surfaces.tui.scopes._zoom.RepoZoomMixin`).
    """

    #: The browse container the zoom mixin hides while a quadrant is
    #: mounted wraps BOTH the workspace table and the registry pane, so
    #: a zoom hides the whole dashboard together and a return restores it.
    ZOOM_BROWSE_PANE: ClassVar[str] = "#pane-repos"

    #: ``#pane-repos`` is the unbordered browse container (its two children
    #: carry the ``.pane`` border); the registry pane sits compact under
    #: the workspace table so the table keeps the bulk of the body.
    DEFAULT_CSS: ClassVar[str] = """
    WorkspaceScreen #pane-repos {
        height: 1fr;
        width: 1fr;
    }
    WorkspaceScreen #pane-registry {
        height: auto;
        max-height: 10;
    }
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
        """Yield the Home band, the per-repo table + registry pane, + zoom mount.

        The ``#pane-repos`` browse container holds the per-repo
        :class:`WorkspaceTable` (each repo's live progress) over a
        read-only :class:`RegistryPane` (the explicit registry listing).
        The container is what the zoom mixin hides on Enter and restores
        on Esc, so the table + registry pane hide and return together.
        """
        with Vertical(id="body"):
            yield from attention_band()
            with Vertical(id="pane-repos"):
                with Vertical(classes="pane", id="pane-workspace"):
                    yield Static("WORKSPACE", classes="pane-title")
                    yield WorkspaceTable(id="workspace-table")
                with Vertical(classes="pane", id="pane-registry"):
                    yield Static("REGISTRY", classes="pane-title")
                    yield RegistryPane(id="registry-pane")
            yield Container(id="zoom-mount")


__all__ = ["WorkspaceScreen"]
