"""``UserScreen`` — user-scope portfolio DataTable.

The user screen is the cross-repo portfolio view: a full-screen per-repo
:class:`~eawf.tui.widgets.workspace_table.WorkspaceTable` (one row per
registered repo, **always at least one** — never a fallback panel) inside
the shared :class:`~eawf.tui.scopes.ScopeScreen` chassis (Header + Footer
+ Heartbeat reused verbatim).

It reuses the W06 workspace-table widget family rather than forking a
second grid — the same status-tinted completion + EU-burn bars, the same
live git column, the same large-N scroll behaviour. The **only** scope
difference is the row-activation semantics: the workspace scope zooms the
focused repo into a 2x2 quadrant, while the user scope has **no zoom
quadrant** — ``Enter`` opens the focused repo's detail overlay and ``z``
is a no-op. :class:`PortfolioTable` subclasses the workspace table to
swap the Enter message (``RepoSelected`` instead of ``RowZoomed``) and
suppress the ``z`` zoom action; everything else (columns, bars, git
probe, scroll) is inherited unchanged.

This screen overrides **only** :meth:`compose_body` + its footer hints;
the entire chassis is inherited from
:class:`~eawf.tui.scopes.ScopeScreen` (zero-duplication).
"""

from __future__ import annotations

import logging
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import DataTable, Static

from eawf.tui.scopes import ScopeScreen
from eawf.tui.widgets.workspace_table import WorkspaceTable

logger = logging.getLogger(__name__)

#: Footer hints tuned for the user portfolio screen (arrows primary; the
#: user scope opens repo detail on Enter and has no zoom affordance).
_USER_HINTS: tuple[str, ...] = (
    "↑↓ row",
    "Enter detail",
    "w/r/u scope",
    "c config",
    "F5 refresh",
    "/ palette",
    "? help",
    "q quit",
)


class PortfolioTable(WorkspaceTable):
    """User-scope portfolio grid — the workspace table without zoom.

    Reuses every column, bar, git-probe, and scroll behaviour of
    :class:`~eawf.tui.widgets.workspace_table.WorkspaceTable`; the only
    override is the row-activation semantics. The user scope has no zoom
    quadrant, so an Enter selection posts :class:`RepoSelected` (the host
    opens the repo's detail overlay) and the ``z`` zoom action is a no-op.
    """

    class RepoSelected(Message):
        """Posted when the operator opens a repo row (Enter).

        The host :class:`UserScreen` opens the focused repo's detail
        overlay in response — there is no zoom quadrant in the user scope.

        Attributes:
            repo_code: The selected repo's project code (the row key).
        """

        def __init__(self, repo_code: str) -> None:
            self.repo_code = repo_code
            super().__init__()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Post :class:`RepoSelected` for the Enter-selected row.

        Overrides the workspace table's zoom message so the user scope
        opens repo detail instead of mounting a quadrant.

        Args:
            event: The Textual row-selected event; ``row_key.value`` is the
                repo code used as the row key.
        """
        repo_code = event.row_key.value
        if repo_code is not None:
            self.post_message(self.RepoSelected(repo_code))

    def action_zoom_row(self) -> None:
        """No-op: the user scope has no zoom quadrant (``z`` is inert)."""
        logger.info("action_zoom_row suppressed scope=user")


class UserScreen(ScopeScreen):
    """User-scope screen: full-screen per-repo portfolio table.

    Composes a :class:`PortfolioTable` (the reused workspace-table family)
    spanning the body. ``↑↓`` focus a repo; ``Enter`` opens the focused
    repo's detail overlay; ``z`` is a no-op (no zoom quadrant in this
    scope). The git column refreshes on the host's refresh tick.
    """

    #: ``c`` opens the registry-driven config window via the shared
    #: ``action_open_config`` on the base chassis. Config is scope-agnostic
    #: — the user scope has no repo anchor, so the modal opens on the
    #: global layer only (``open_config`` resolves the available writable
    #: layers per scope).
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("c", "open_config", "config", show=False),
    ]

    FOOTER_HINTS: ClassVar[tuple[str, ...]] = _USER_HINTS

    def compose_body(self) -> ComposeResult:
        """Yield the full-screen portfolio table body."""
        with Vertical(id="body"), Vertical(classes="pane", id="pane-portfolio"):
            yield Static("PORTFOLIO", classes="pane-title")
            yield PortfolioTable(id="portfolio-table")

    def on_portfolio_table_repo_selected(self, message: PortfolioTable.RepoSelected) -> None:
        """Open the focused repo's detail overlay (the Enter handler).

        Routes the repo code through the shared
        :meth:`~eawf.tui.scopes.ScopeScreen._open_detail` seam so the
        drill-in path matches every other row activation — no zoom
        quadrant is mounted.

        Args:
            message: The :class:`PortfolioTable.RepoSelected` message
                carrying the repo code to open detail for.
        """
        self._open_detail(message.repo_code)


__all__ = ["PortfolioTable", "UserScreen"]
