"""Per-scope screens for the Eä Textual TUI (tui).

The three scope screens (``RepoScreen`` / ``WorkspaceScreen`` /
``UserScreen``) compose the widget catalog
(:mod:`eawf.surfaces.tui.widgets`) inside a **shared chassis** — one
:class:`~eawf.surfaces.tui.widgets.header.Header` + one
:class:`~eawf.surfaces.tui.widgets.footer.Footer` (which owns the
:class:`~eawf.surfaces.tui.widgets.footer.Heartbeat`) reused verbatim by every
screen with **no per-scope duplication**.

The shared chassis lives on :class:`ScopeScreen`: it yields the Header,
then a per-scope body produced by the subclass's :meth:`ScopeScreen.compose_body`
hook, then the Footer. Each concrete screen overrides **only** the body
hook (and its scope-specific footer hints) — the brand, breadcrumb,
runtime cell, clock, heartbeat, and quit/help/palette key bindings are
declared once on the base. That is the chassis trim: the salvageable
chassis LOC drops from a ``~5300`` duplicate-per-scope baseline to
``~2500`` shared.

Per-scope body layouts:

* ``RepoScreen`` — 2x2 quadrant (roadmap · status / git · backlog).
* ``WorkspaceScreen`` — top strip + active-repo quadrant (the
  ``WorkspaceTopStrip`` / ``RepoQuadrant`` sub-widgets land in a later
  wave; this screen composes the available widget catalog in the
  strip+zoom arrangement).
* ``UserScreen`` — three weighted sections (attention · effort ·
  portfolio); the ``AttentionList`` / ``EffortBars`` / ``PortfolioTable``
  sub-widgets land in a later wave.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Static

from eawf.surfaces.tui.palette.command_palette import open_palette
from eawf.surfaces.tui.screens.help import open_help
from eawf.surfaces.tui.screens.overlays.detail import DetailModal, resolve_detail
from eawf.surfaces.tui.widgets.attention_feed import AttentionFeed
from eawf.surfaces.tui.widgets.backlog_table import BacklogTable
from eawf.surfaces.tui.widgets.footer import DEFAULT_HINTS, Footer
from eawf.surfaces.tui.widgets.header import Header
from eawf.surfaces.tui.widgets.roadmap_tree import RoadmapTree

logger = logging.getLogger(__name__)

#: Pane id of the Home attention band so a host (or zoom mixin) can address
#: it without touching the scope-body panes. The band is the orthogonal
#: Home-overview strip -- zoom hides only its own browse pane, never this.
ATTENTION_BAND_PANE: str = "attention-band"


def attention_band() -> ComposeResult:
    """Yield the Home overview band (an :class:`AttentionFeed` in a titled pane).

    The single source of the Home attention strip every scope screen leads
    its body with, so the band's title + id live in one place (DRY). The
    band renders the ranked attention feed above the scope body and stays
    reachable across the orthogonal scope axis (``w`` / ``r`` / ``u``): it is
    a sibling of the scope panes, not part of them, so a scope switch swaps
    the panes beneath an always-present band.

    Returns:
        The composed band widgets (a pane wrapper + the feed).
    """
    with Vertical(classes="pane", id=f"pane-{ATTENTION_BAND_PANE}"):
        yield Static("ATTENTION", classes="pane-title")
        yield AttentionFeed(id=ATTENTION_BAND_PANE)


class ScopeScreen(Screen[None]):
    """Shared-chassis base for every per-scope screen.

    Owns the Header + Footer (+ Heartbeat) chrome and the chrome key
    bindings; subclasses override **only** :meth:`compose_body` (and
    optionally :attr:`FOOTER_HINTS`). This is the single source of the
    chassis composition — no scope re-declares the header or footer.
    """

    #: Chrome key bindings shared by every scope screen (full key names;
    #: arrows are primary, vim aliases live app-wide on
    #: :class:`~eawf.surfaces.tui.app.EaApp`). Scope-specific bindings (e.g.
    #: workspace ``z`` zoom) are appended by the subclass.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("slash", "open_palette", "palette", show=False),
        Binding("question_mark", "open_help", "help", show=False),
        Binding("f5", "force_refresh", "refresh", show=False),
        Binding("q", "quit", "quit"),
        Binding("escape", "quit", "quit", show=False),
    ]

    #: Footer key hints for this scope (full key names). Overridable per
    #: subclass; defaults to the shared base hints.
    FOOTER_HINTS: ClassVar[tuple[str, ...]] = DEFAULT_HINTS

    def compose(self) -> ComposeResult:
        """Yield the shared chassis around the per-scope body.

        Header (top) → subclass body → Footer (bottom). The body is the
        only part a concrete screen customises; everything else is the
        shared chassis.
        """
        yield Header()
        yield from self.compose_body()
        yield Footer()

    def compose_body(self) -> ComposeResult:
        """Yield the per-scope body widgets.

        Overridden by every concrete scope screen; the base raises so a
        screen that forgets to compose a body fails fast rather than
        rendering empty chrome.

        Raises:
            NotImplementedError: Always — concrete screens must override.
        """
        raise NotImplementedError("scope screens must override compose_body")
        yield  # pragma: no cover — unreachable; keeps the generator typed

    def on_mount(self) -> None:
        """Apply the scope-specific footer hints once the chassis mounts."""
        if self.FOOTER_HINTS != DEFAULT_HINTS:
            self.query_one(Footer).set_hints(self.FOOTER_HINTS)

    def action_open_palette(self) -> None:
        """Open the ``/`` command palette overlay (cap-checked)."""
        open_palette(self.app)

    def action_open_help(self) -> None:
        """Open the ``?`` help overlay (cap-checked, single-instance)."""
        action = getattr(self.app, "action_open_help", None)
        if callable(action):
            action()
            return
        open_help(self.app)

    def action_open_config(self) -> None:
        """Open the ``c`` registry-driven config window (cap-checked).

        Delegates to the App's ``action_open_config`` so the keypress and
        the ``/config`` palette verb share one cap-checked path; falls
        back to the module-level
        :func:`~eawf.surfaces.tui.screens.overlays.config_modal.open_config`
        under a bare harness.
        """
        action = getattr(self.app, "action_open_config", None)
        if callable(action):
            action()
            return
        from eawf.surfaces.tui.screens.overlays.config_modal import open_config

        open_config(self.app)

    def action_force_refresh(self) -> None:
        """Acknowledge the ``F5`` force-refresh (heartbeat ack).

        Refresh moved off raw ``r`` (now the repo scope-switch) onto
        ``F5`` so the scope-switch and refresh affordances no longer
        collide. The full force-tick + cache-invalidate path lands with
        the daemon push wiring; this pulses the footer heartbeat so the
        operator sees the keypress is live.
        """
        from eawf.surfaces.tui.widgets.footer import Heartbeat

        heartbeats = self.query(Heartbeat)
        if heartbeats:
            heartbeats.first().ack()

    def _open_detail(self, selection_id: str) -> None:
        """Resolve *selection_id* against app state and push a DetailModal.

        The single drill-in path shared by both selection messages: it
        resolves the entity to a :class:`~eawf.surfaces.tui.screens.overlays.detail.DetailCard`
        from the App's reactive ``state`` and pushes the overlay through
        the modal-cap-aware helper so the stack-depth limit is honoured.

        Re-activating the row whose card is already on the modal TOP is a
        benign no-op: the duplicate push is suppressed (logged, no Toast) so a
        double-Enter or a re-selection of the same row cannot stack a second
        identical card. A drill into a *different* entity still stacks.

        Args:
            selection_id: The id carried by the selection message.
        """
        if self._detail_already_on_top(selection_id):
            logger.info(f"_open_detail dedup_skipped entity={selection_id!r}")
            return
        state = getattr(self.app, "state", None)
        card = resolve_detail(state, selection_id)
        push_modal = getattr(self.app, "push_modal", None)
        if callable(push_modal):
            push_modal(DetailModal(card, state=state, entity_id=selection_id))
            return
        self.app.push_screen(DetailModal(card, state=state, entity_id=selection_id))

    def _detail_already_on_top(self, selection_id: str) -> bool:
        """Return whether *selection_id*'s card is the top-of-stack modal.

        Reads the App's top-most overlay (via its ``_top_modal`` accessor) and
        reports ``True`` only when it is a
        :class:`~eawf.surfaces.tui.screens.overlays.detail.DetailModal` drilled
        from the SAME entity id, so the caller can suppress a duplicate push. A
        bare harness without the accessor, or any non-matching top, yields
        ``False`` (the push proceeds).

        Args:
            selection_id: The entity id the new drill targets.

        Returns:
            ``True`` when the same entity's card is already on top.
        """
        top_modal = getattr(self.app, "_top_modal", None)
        if not callable(top_modal):
            return False
        top = top_modal()
        return isinstance(top, DetailModal) and top.entity_id == selection_id

    def on_backlog_table_row_activated(self, message: BacklogTable.RowActivated) -> None:
        """Route a backlog Enter-selection to the DetailModal.

        Args:
            message: The W17 :class:`BacklogTable.RowActivated` message
                carrying the activated item id.
        """
        self._open_detail(message.item_id)

    def on_roadmap_tree_wave_selected(self, message: RoadmapTree.WaveSelected) -> None:
        """Route a roadmap wave Enter-selection to the DetailModal.

        Args:
            message: The W17 :class:`RoadmapTree.WaveSelected` message
                carrying the selected wave id.
        """
        self._open_detail(message.wave_id)

    def on_attention_feed_pause_selected(self, message: AttentionFeed.PauseSelected) -> None:
        """Route an attention-band pause activation to its needs_user modal.

        The Home overview band posts this when the operator activates a
        needs_user row; route it through the App's shared
        ``open_needs_user_pause`` so the modal opens on the same cap-checked
        + resume path the auto-open and the global inbox use. A no-op under a
        bare harness that lacks the hook.

        Args:
            message: The :class:`AttentionFeed.PauseSelected` carrying the
                pause urn + question to open.
        """
        open_pause = getattr(self.app, "open_needs_user_pause", None)
        if callable(open_pause):
            open_pause(message.pause_urn, message.question)


from eawf.surfaces.tui.scopes.repo import RepoScreen  # noqa: E402  (after base def)
from eawf.surfaces.tui.scopes.user import UserScreen  # noqa: E402
from eawf.surfaces.tui.scopes.workspace import WorkspaceScreen  # noqa: E402

__all__ = [
    "ATTENTION_BAND_PANE",
    "RepoScreen",
    "ScopeScreen",
    "UserScreen",
    "WorkspaceScreen",
    "attention_band",
]
