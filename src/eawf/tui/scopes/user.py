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
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import DataTable, Static

from eawf.registry.models import Registry, RegistryReadError, read_registry
from eawf.state.enums import ProjectStatus, ScopeKind
from eawf.state.models import (
    CurrentPointers,
    State,
    WorkspaceIndex,
    WorkspaceRepoRef,
)
from eawf.state.urn import build as build_urn
from eawf.tui.scopes import ScopeScreen
from eawf.tui.widgets.workspace_table import WorkspaceTable

logger = logging.getLogger(__name__)

#: Synthetic owner + code for the portfolio's workspace index. The user
#: scope has no on-disk workspace ``state.json`` — its aggregate is built
#: from the global registry — so the synthesized index carries a fixed
#: code/title rather than a per-repo anchor.
_PORTFOLIO_CODE = "PORTFOLIO"
_PORTFOLIO_TITLE = "Portfolio"


def synthesize_user_state(*, registry_path: Path | None = None, home: Path | None = None) -> State:
    """Synthesize a workspace-shaped state for the user portfolio scope.

    The user scope has no on-disk ``state.json`` (it aggregates across
    repos rather than anchoring on one), so the portfolio table's bound
    state is built from the global registry ``~/.eawf/registry.json``:
    each :class:`~eawf.registry.models.RegistryRepoEntry` becomes a
    :class:`~eawf.state.models.WorkspaceRepoRef` under a synthetic
    :class:`~eawf.state.models.WorkspaceIndex`, so
    :func:`~eawf.tui.widgets.workspace_table.build_repo_rows` emits one
    portfolio row per registered repo. Strictly read-only — never grows
    the registry (per the explicit-registry-only rule).

    A missing or corrupt registry yields a state with an empty
    ``workspace.repos`` rather than raising, so the table renders
    columns-only instead of crashing.

    Args:
        registry_path: Explicit registry path. When ``None``, falls back
            to ``~/.eawf/registry.json`` (resolved via *home*).
        home: Test seam for the default-path branch. Pass a ``tmp_path``
            root so tests never touch the operator's real registry.
            Ignored when *registry_path* is supplied directly.

    Returns:
        A :class:`~eawf.state.models.State` whose ``workspace.repos``
        mirrors the registry (possibly empty).
    """
    try:
        registry = read_registry(registry_path, home=home)
    except RegistryReadError as exc:
        logger.info(f"synthesize_user_state registry_unavailable cause={exc!r}")
        registry = Registry()
    repos: dict[str, WorkspaceRepoRef] = {}
    for code, entry in registry.repos.items():
        repos[code] = WorkspaceRepoRef(
            code=entry.code,
            path=entry.path,
            state_urn=build_urn("repo", owner=entry.code),
            project_code=entry.code,
            title=entry.title or entry.code,
            status=ProjectStatus.ACTIVE,
        )
    workspace = WorkspaceIndex(
        code=_PORTFOLIO_CODE,
        title=_PORTFOLIO_TITLE,
        repos=repos,
        current_repo_code=registry.active_code,
    )
    return State(
        schema_version="1.1",
        scope_kind=ScopeKind.WORKSPACE,
        urn=build_urn("workspace", owner=_PORTFOLIO_CODE),
        updated_at=datetime.now(UTC),
        project=None,
        current=CurrentPointers(),
        workspace=workspace,
        phases={},
        iters={},
        waves={},
        artifacts={},
        agent_sessions={},
        plugins={},
        indexes={},
    )


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


__all__ = ["PortfolioTable", "UserScreen", "synthesize_user_state"]
