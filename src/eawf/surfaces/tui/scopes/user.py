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
from eawf.surfaces.tui.scopes import ScopeScreen, attention_band
from eawf.surfaces.tui.scopes._zoom import RepoZoomMixin
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE, RenderMode
from eawf.surfaces.tui.widgets.footer import MODE_ROW_SEP, render_hint_label
from eawf.surfaces.tui.widgets.markup import escape_markup
from eawf.surfaces.tui.widgets.registry_pane import REGISTRY_EMPTY_CELL, REGISTRY_HINT_LINE
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph
from eawf.surfaces.tui.widgets.workspace_table import WorkspaceTable, build_repo_rows

logger = logging.getLogger(__name__)

#: Synthetic owner + code for the portfolio's workspace index. The user
#: scope has no on-disk workspace ``state.json`` — its aggregate is built
#: from the global registry — so the synthesized index carries a fixed
#: code/title rather than a per-repo anchor.
_PORTFOLIO_CODE = "PORTFOLIO"
_PORTFOLIO_TITLE = "Portfolio"
USER_SCOPE_INIT_NEEDED_KEY = "init_needed"

#: The three scope-switch affordances, in the canonical left-to-right order
#: the breadcrumb axis reads (repo -> workspace -> portfolio), each as a
#: ``(label, key)`` pair. The label is the scope noun the operator sees in
#: the breadcrumb; the key is the single keypress
#: (:meth:`~eawf.surfaces.tui.app.EaApp.action_switch_scope`) that lands on
#: that scope. The user portfolio scope's switch key is ``u`` -- its strip
#: label is ``portfolio`` (the noun the breadcrumb shows), matching the
#: reskin mock ``repo r  ·  workspace w  ·  portfolio u``.
SCOPE_SWITCH_ITEMS: tuple[tuple[str, str], ...] = (
    ("repo", "r"),
    ("workspace", "w"),
    ("portfolio", "u"),
)


def build_scope_switch_strip(active_label: str, *, separator: str = MODE_ROW_SEP) -> str:
    """Build the workspace / user scope-switch mode strip as content markup.

    Pure render source -- unit-testable without mounting the widget. Renders
    each :data:`SCOPE_SWITCH_ITEMS` entry as a ``<label> <key>`` token (e.g.
    ``repo r``) joined by *separator*, so the strip reads
    ``repo r  ·  workspace w  ·  portfolio u`` -- the reskin mock. The token
    whose *label* equals *active_label* is highlighted with a bold green
    ``$accent`` span (the same brand-accent shape
    :func:`~eawf.surfaces.tui.widgets.footer.build_mode_row` draws for the
    active mode); every other token renders muted ``$muted``. A label that
    matches no scope highlights nothing, so a bare / unexpected caller never
    fabricates a false-active token. Labels + keys are markup-escaped
    defensively so a bracket can never be parsed as a style tag.

    Reusing the footer's :data:`~eawf.surfaces.tui.widgets.footer.MODE_ROW_SEP`
    bullet keeps the scope strip and the always-visible mode row reading as
    one visual family (the reskinned footer-mode strip the operator already
    knows).

    Args:
        active_label: The label of the active scope (``repo`` / ``workspace``
            / ``portfolio``); the matching token is accented.
        separator: The bullet between tokens; defaults to the footer's
            :data:`~eawf.surfaces.tui.widgets.footer.MODE_ROW_SEP` so the
            strip matches the mode row.

    Returns:
        A Textual content-markup string for the scope-switch strip.
    """
    tokens: list[str] = []
    for label, key in SCOPE_SWITCH_ITEMS:
        text = f"{escape_markup(label)} {escape_markup(key)}"
        if label == active_label:
            tokens.append(f"[$accent][b]{text}[/b][/]")
        else:
            tokens.append(f"[$muted]{text}[/]")
    return separator.join(tokens)


class ScopeSwitchStrip(Static):
    """The w/u scope-switch mode strip painted under a scope-screen body.

    A :class:`~textual.widgets.Static` that paints
    :func:`build_scope_switch_strip` for its host scope -- the workspace
    scope passes ``workspace`` and the user portfolio passes ``portfolio``,
    so each surface accents its own token in the shared
    ``repo r  ·  workspace w  ·  portfolio u`` strip. The active label is
    fixed per screen (each scope owns its own screen), so the strip is
    constructed once at compose time; there is no per-state repaint.
    """

    DEFAULT_CSS: ClassVar[str] = """
    ScopeSwitchStrip {
        width: 1fr;
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }
    """

    def __init__(self, active_label: str, **kwargs: object) -> None:
        """Construct the strip pinned to *active_label* as the active scope.

        Args:
            active_label: The label of the host scope (``workspace`` /
                ``portfolio``); the matching token paints accented.
            **kwargs: Forwarded to :class:`textual.widgets.Static`.
        """
        super().__init__(build_scope_switch_strip(active_label), **kwargs)  # type: ignore[arg-type]
        #: The active-scope label this strip accents, exposed so a host /
        #: test reads which token is highlighted without scraping markup.
        self.active_label = active_label


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


#: Headline of the honest-empty card -- the byte-for-byte no-repos line the
#: registry pane already pins
#: (:data:`~eawf.surfaces.tui.widgets.registry_pane.REGISTRY_EMPTY_CELL`),
#: reused so the empty-grid card and the registry listing speak one phrase
#: rather than inventing a second. A scope with zero registered repos shows
#: this calm directive instead of a fabricated repo or a ``0 repos`` totals
#: roll-up.
HONEST_EMPTY_HEADLINE: str = REGISTRY_EMPTY_CELL

#: Directive sub-line of the honest-empty card -- the explicit-growth hint
#: (:data:`~eawf.surfaces.tui.widgets.registry_pane.REGISTRY_HINT_LINE`) so the
#: operator reads the supported (explicit-only) way to register a repo. The
#: registry never auto-discovers, so the card names ``eawf init`` /
#: ``eawf repo add`` as the only growth path.
HONEST_EMPTY_DIRECTIVE: str = REGISTRY_HINT_LINE


def render_no_repos_card(*, mode: RenderMode = DEFAULT_RENDER_MODE) -> str:
    """Render the calm honest-empty no-repos card in the brand voice.

    Surfaced by both the user portfolio and the workspace scope when the
    bound state resolves zero repos (an empty / unavailable registry): a
    scope with no rows shows this card rather than a columns-only grid with
    no guidance, and -- because there is nothing to sum -- without a
    fabricated repo row or a ``0 repos`` totals roll-up.

    The card leads with the green ``$accent`` pending sigil (the shared
    not-yet-here SHAPE the lifecycle panes draw for a wave that has not
    started, NOT a spinner or any false-busy chrome) and the
    :data:`HONEST_EMPTY_HEADLINE` no-repos line in the same green
    ``$accent``, then the muted :data:`HONEST_EMPTY_DIRECTIVE` naming the
    explicit-only growth path. The colours resolve against the active
    theme's green accent at render time via Textual content markup, so the
    surface reads as intentionally empty -- ready for a first repo, not
    broken.

    Args:
        mode: The App's resolved render-mode label, threaded so the pending
            sigil resolves its ASCII / unicode glyph column; defaults to
            :data:`~eawf.surfaces.tui.widgets.eu_bar.DEFAULT_RENDER_MODE`.

    Returns:
        A content-markup string: the green-accent sigil + no-repos
        headline line, then the muted explicit-growth directive sub-line.
    """
    sigil = glyph(Sigil.PENDING, mode=mode)
    return "\n".join(
        [
            f"[$accent]{sigil} {HONEST_EMPTY_HEADLINE}[/]",
            f"[$muted]{HONEST_EMPTY_DIRECTIVE}[/]",
        ]
    )


def state_has_no_repos(state: State | None) -> bool:
    """Return whether *state* resolves zero repo rows (the honest-empty case).

    The single predicate both scopes toggle the honest-empty card on:
    a ``None`` / non-workspace / empty-registry state yields no
    :func:`~eawf.surfaces.tui.widgets.workspace_table.build_repo_rows`, so
    the grid is empty and the card shows instead. Reusing the same row
    builder the grid itself folds keeps the card and the grid in lockstep
    -- the card shows exactly when (and only when) the grid would render
    zero repo rows.

    Args:
        state: The bound scope state, or ``None``.

    Returns:
        ``True`` when the state resolves no repo rows; ``False`` otherwise.
    """
    return not build_repo_rows(state)


class HonestEmptyCard(Static):
    """Calm honest-empty no-repos card mounted beside an empty-grid scope.

    A :class:`~textual.widgets.Static` that paints
    :func:`render_no_repos_card` and toggles its own visibility off the
    App's bound ``state``: it shows only when the state resolves zero repo
    rows (an empty / unavailable registry) and hides the moment a repo is
    registered, so a populated scope reads as the grid alone and an empty
    scope reads as the directive card rather than a columns-only grid.

    The card carries NO totals row and fabricates NO repo -- it is the
    honest-empty surface itself, paired with a grid the host hides while
    the card is shown.
    """

    DEFAULT_CSS: ClassVar[str] = """
    HonestEmptyCard {
        width: 1fr;
        height: auto;
        color: $text-muted;
    }
    """

    def __init__(self, **kwargs: object) -> None:
        """Construct the card with an empty painted-markup cache.

        Args:
            **kwargs: Forwarded to :class:`textual.widgets.Static`.
        """
        super().__init__(**kwargs)  # type: ignore[arg-type]
        #: The last-painted content markup, exposed via :meth:`rendered_text`
        #: so a host / test reads the card body without scraping the
        #: compositor. Empty until the first :meth:`_repaint`.
        self._painted: str = ""

    def on_mount(self) -> None:
        """Paint the card, seed visibility, and watch the App's state."""
        self._repaint()
        if hasattr(self.app, "state"):
            self.watch(self.app, "state", self._on_app_state)

    def _on_app_state(self, _state: State | None) -> None:
        """Repaint + re-toggle visibility when the bound state changes."""
        self._repaint()

    def rendered_text(self) -> str:
        """Return the last-painted card markup (the no-repos card body).

        The directive the card painted, read without scraping the
        compositor. Empty until the mount-time first paint has run.

        Returns:
            The last-painted content markup string.
        """
        return self._painted

    def _render_mode(self) -> RenderMode:
        """Return the host app's live render mode, or the safe default.

        Threads :attr:`eawf.surfaces.tui.app.EaApp.render_mode` into the
        pending sigil so an ASCII / unicode flip rerenders the card with
        the matching glyph column; falls back to
        :data:`~eawf.surfaces.tui.widgets.eu_bar.DEFAULT_RENDER_MODE` under a
        bare harness whose host App carries no ``render_mode`` attribute.

        Returns:
            The active ``"unicode"`` / ``"ascii"`` mode.
        """
        return getattr(self.app, "render_mode", DEFAULT_RENDER_MODE)

    def _repaint(self) -> None:
        """Repaint the card body and show / hide it off the bound state."""
        state = getattr(self.app, "state", None)
        self.display = state_has_no_repos(state)
        self._painted = render_no_repos_card(mode=self._render_mode())
        self.update(self._painted)


#: Footer hints tuned for the user portfolio screen (arrows primary; the
#: user scope opens the focused repo on Enter, like the workspace). Every label
#: is produced through
#: :func:`~eawf.surfaces.tui.widgets.footer.render_hint_label` so the key
#: tokens AND the shared-token actions stay pinned to the canonical vocabulary.
_USER_HINTS: tuple[str, ...] = (
    render_hint_label("↑↓", "select"),
    render_hint_label("Enter", "open"),
    render_hint_label("Esc", "back"),
    render_hint_label("w/r/u", "scope"),
    render_hint_label("c", "config"),
    render_hint_label("F5", "refresh"),
    render_hint_label("/", "palette"),
    render_hint_label("?", "help"),
    render_hint_label("q", "quit"),
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
        """Yield the band, the portfolio pane (grid + card), the scope strip, + zoom.

        The portfolio pane carries BOTH the :class:`PortfolioTable` and the
        :class:`HonestEmptyCard`: the card shows (and the grid hides) only
        when the bound state resolves zero repos, so an empty / unavailable
        registry reads as the calm no-repos directive rather than a
        columns-only grid -- never a fabricated repo or a ``0 repos`` totals
        roll-up. A populated scope reads as the grid alone.

        Below the pane sits the :class:`ScopeSwitchStrip` -- the
        ``repo r  ·  workspace w  ·  portfolio u`` switch affordance with the
        ``portfolio`` token accented, so the operator sees this scope is
        active and which key reaches the others.
        """
        with Vertical(id="body"):
            yield from attention_band()
            with Vertical(classes="pane", id="pane-portfolio"):
                yield Static("PORTFOLIO", classes="pane-title")
                yield HonestEmptyCard(id="portfolio-empty")
                yield PortfolioTable(id="portfolio-table")
            yield ScopeSwitchStrip("portfolio", id="scope-switch-strip")
            yield Container(id="zoom-mount")

    def on_mount(self) -> None:
        """Seed the grid / card split, then watch the App state to re-toggle.

        Calls the base chassis ``on_mount`` (footer hints) first, then hides
        the portfolio grid whenever the honest-empty card is showing so an
        empty scope never renders a columns-only grid beneath the directive
        card.
        """
        super().on_mount()
        self._sync_empty_split()
        if hasattr(self.app, "state"):
            self.watch(self.app, "state", self._on_app_state_split)

    def _on_app_state_split(self, _state: State | None) -> None:
        """Re-toggle the grid / card split when the bound state changes."""
        self._sync_empty_split()

    def _sync_empty_split(self) -> None:
        """Hide the portfolio grid exactly when the honest-empty card shows."""
        empty = state_has_no_repos(getattr(self.app, "state", None))
        tables = self.query("#portfolio-table")
        if tables:
            tables.first().display = not empty


__all__ = [
    "HONEST_EMPTY_DIRECTIVE",
    "HONEST_EMPTY_HEADLINE",
    "SCOPE_SWITCH_ITEMS",
    "USER_SCOPE_INIT_NEEDED_KEY",
    "HonestEmptyCard",
    "PortfolioTable",
    "ScopeSwitchStrip",
    "UserScreen",
    "build_scope_switch_strip",
    "render_no_repos_card",
    "state_has_no_repos",
    "synthesize_user_state",
    "user_scope_init_needed",
]
