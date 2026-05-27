"""``UserScreen`` — user-scope portfolio DataTable.

The user screen is the cross-repo portfolio view: a full-screen per-repo
:class:`~eawf.surfaces.tui.widgets.workspace_table.WorkspaceTable` (one row per
registered repo, **always at least one** — never a fallback panel) inside
the shared :class:`~eawf.surfaces.tui.scopes.ScopeScreen` chassis (Header + Footer
+ Heartbeat reused verbatim).

It reuses the W06 workspace-table widget family rather than forking a
second grid — the same status-tinted completion + EU-burn bars, the same
live git column, the same large-N scroll behaviour. Row activation also
matches the workspace scope: ``Enter`` zooms the focused repo
into a 2x2 quadrant (roadmap · status / git · backlog) scoped to that
repo's own ``state.json``, and ``Esc`` returns. The zoom lifecycle is the
shared :class:`~eawf.surfaces.tui.scopes._zoom.RepoZoomMixin`, and
:class:`PortfolioTable` subclasses the workspace table unchanged — it
inherits the ``RowZoomed`` Enter message, so both scopes drive the
identical zoom path.

This screen overrides **only** :meth:`compose_body` + its scope bindings
+ footer hints; the entire chassis is inherited from
:class:`~eawf.surfaces.tui.scopes.ScopeScreen` (zero-duplication).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Vertical
from textual.widgets import Static

from eawf.kernel.state.enums import ProjectStatus, ScopeKind
from eawf.kernel.state.models import (
    CurrentPointers,
    State,
    WorkspaceIndex,
    WorkspaceRepoRef,
)
from eawf.kernel.state.urn import build as build_urn
from eawf.platform.registry.models import Registry, RegistryReadError, read_registry
from eawf.surfaces.tui.scopes import ScopeScreen
from eawf.surfaces.tui.scopes._zoom import RepoZoomMixin
from eawf.surfaces.tui.widgets.workspace_table import WorkspaceTable

logger = logging.getLogger(__name__)

#: Synthetic owner + code for the portfolio's workspace index. The user
#: scope has no on-disk workspace ``state.json`` — its aggregate is built
#: from the global registry — so the synthesized index carries a fixed
#: code/title rather than a per-repo anchor.
_PORTFOLIO_CODE = "PORTFOLIO"
_PORTFOLIO_TITLE = "Portfolio"
USER_SCOPE_INIT_NEEDED_KEY = "init_needed"


def synthesize_user_state(*, registry_path: Path | None = None, home: Path | None = None) -> State:
    """Synthesize a workspace-shaped state for the user portfolio scope.

    The user scope has no on-disk ``state.json`` (it aggregates across
    repos rather than anchoring on one), so the portfolio table's bound
    state is built from the global registry ``~/.eawf/registry.json``:
    each :class:`~eawf.platform.registry.models.RegistryRepoEntry` becomes a
    :class:`~eawf.kernel.state.models.WorkspaceRepoRef` under a synthetic
    :class:`~eawf.kernel.state.models.WorkspaceIndex`, so
    :func:`~eawf.surfaces.tui.widgets.workspace_table.build_repo_rows` emits one
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
        A :class:`~eawf.kernel.state.models.State` whose ``workspace.repos``
        mirrors the registry (possibly empty).
    """
    registry_unavailable = False
    try:
        registry = read_registry(registry_path, home=home)
    except RegistryReadError as exc:
        logger.info(f"synthesize_user_state registry_unavailable cause={exc!r}")
        registry_unavailable = True
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
    init_needed = registry_unavailable or not repos
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
        indexes={USER_SCOPE_INIT_NEEDED_KEY: init_needed},
    )


def user_scope_init_needed(state: State | None) -> bool:
    """Return whether the synthesized user scope should prompt for init.

    The user portfolio carries this as an in-memory ``indexes`` flag so no
    schema change or persisted ``state.json`` write is needed. Missing or
    empty registries set the flag; populated registries clear it.
    """
    if state is None:
        return False
    return bool(state.indexes.get(USER_SCOPE_INIT_NEEDED_KEY))


#: Footer hints tuned for the user portfolio screen (arrows primary; the
#: user scope zooms the focused repo on Enter, like the workspace).
_USER_HINTS: tuple[str, ...] = (
    "↑↓ row",
    "Enter zoom",
    "Esc back",
    "w/r/u scope",
    "c config",
    "F5 refresh",
    "/ palette",
    "? help",
    "q quit",
)


class PortfolioTable(WorkspaceTable):
    """User-scope portfolio grid — the workspace table, reused verbatim.

    Reuses every column, bar, git-probe, scroll, and row-activation
    behaviour of :class:`~eawf.surfaces.tui.widgets.workspace_table.WorkspaceTable`
    with no overrides: an Enter selection posts ``RowZoomed``, so the host
    :class:`~eawf.surfaces.tui.scopes._zoom.RepoZoomMixin` mounts the focused
    repo's 2x2 quadrant exactly as the workspace scope does. The subclass
    exists only to give the user scope a distinct widget type for
    ``query_one(PortfolioTable)`` lookups.
    """


class UserScreen(ScopeScreen, RepoZoomMixin):
    """User-scope screen: full-screen per-repo portfolio table with zoom.

    Composes a :class:`PortfolioTable` (the reused workspace-table family)
    spanning the body plus an (initially empty) zoom mount. ``↑↓`` focus a
    repo; ``Enter`` zooms the focused repo into a 2x2 quadrant
    scoped to that repo's own ``state.json`` (the shared
    :class:`~eawf.surfaces.tui.scopes._zoom.RepoZoomMixin`); ``Esc`` returns. The
    git column refreshes on the host's refresh tick.
    """

    #: The user scope's browse pane is ``#pane-portfolio``; the zoom mixin
    #: hides / restores it on zoom / exit.
    ZOOM_BROWSE_PANE: ClassVar[str] = "#pane-portfolio"

    #: ``Enter`` zooms the focused row (via the table's ``RowZoomed``
    #: message); ``Esc`` returns from the zoom quadrant to the table; ``c``
    #: opens the registry-driven config window via the shared
    #: ``action_open_config`` on the base chassis. Config is scope-agnostic
    #: — the user scope has no repo anchor, so the modal opens on the
    #: global layer only.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "leave_zoom", "back", show=False),
        Binding("c", "open_config", "config", show=False),
    ]

    FOOTER_HINTS: ClassVar[tuple[str, ...]] = _USER_HINTS

    def compose_body(self) -> ComposeResult:
        """Yield the portfolio table body + an (initially empty) zoom mount."""
        with Vertical(id="body"):
            with Vertical(classes="pane", id="pane-portfolio"):
                yield Static("PORTFOLIO", classes="pane-title")
                yield PortfolioTable(id="portfolio-table")
            yield Container(id="zoom-mount")


__all__ = [
    "USER_SCOPE_INIT_NEEDED_KEY",
    "PortfolioTable",
    "UserScreen",
    "synthesize_user_state",
    "user_scope_init_needed",
]
