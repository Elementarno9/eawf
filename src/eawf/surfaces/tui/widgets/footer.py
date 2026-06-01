"""``Footer`` + ``Heartbeat`` — shared chassis footer (widget catalog).

A single footer composite reused by every per-scope screen
(``RepoScreen`` / ``WorkspaceScreen`` / ``UserScreen``) with **no
per-scope duplication**. The footer carries:

* a context-aware key-hint strip using **full key names** only
  (``PageUp`` / ``PageDown`` / ``Enter`` / ``Esc`` — never ``PgUp``) per
  the operator keymap convention, and
* a live :class:`Heartbeat` dot — a ``•`` pulse that proves the
  TUI is alive, ``accent``-coloured by default and ``err``-coloured when
  any pane is degraded, with a 0.5 s double-pulse ack on the ``F5``
  force-refresh keypress.

Bundling the heartbeat inside the footer is the chassis trim: the
three scope screens reuse one :class:`Footer` (which mounts the shared
:class:`Heartbeat`) rather than each re-declaring the chrome — the
``~5300 → ~2500`` salvageable-LOC target. Colours resolve
against the ``theme.tcss`` palette vars (``$muted`` for the hints,
``$accent`` / ``$err`` for the heartbeat) — never hardcoded hex.

The heartbeat pulse runs off a Textual ``set_interval`` timer started on
mount; the host screen flips :attr:`Heartbeat.degraded` (wired to the
App's degraded reactive in a later wave) to swap the dot colour. The
pulse cadence + the visible/hidden toggle are pure-ish state on the
widget so a Pilot test can drive a tick and assert the dot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Static

from eawf.surfaces.tui.widgets.eu_bar import EMPTY_STATE
from eawf.surfaces.tui.widgets.heartbeat import HEARTBEAT_GLYPH, HEARTBEAT_INTERVAL_S, Heartbeat
from eawf.workflow.estimation.metrics import compute_weekly_burn

if TYPE_CHECKING:
    from datetime import datetime

    from eawf.kernel.state.models import State


#: Default footer key hints (full key names). Screens may pass a
#: scope-specific override via :meth:`Footer.set_hints`; this is the base
#: chrome shared by every scope. ``w/r/u`` scope-switch + ``F5`` refresh
#: are surfaced so the operator sees the global affordances.
DEFAULT_HINTS: tuple[str, ...] = (
    "↑↓ move",
    "Enter open",
    "w/r/u scope",
    "F5 refresh",
    "/ palette",
    "? help",
    "q quit",
)


#: Empty-state marker for the weekly-burn line. Rendered when the project
#: has no ``weekly_eu_target`` set or no actuals have rolled up yet — the
#: EU estimation surface is unpopulated scaffolding today, so a graceful
#: "surface now, data later" placeholder is shown rather than a misleading
#: ``0 / 0`` figure. Sourced from the canonical
#: :data:`~eawf.surfaces.tui.widgets.eu_bar.EMPTY_STATE` sentinel so every
#: "no data" surface stays in lockstep (kept as the footer's public name).
WEEKLY_BURN_EMPTY: str = EMPTY_STATE

#: Static label prefixing the weekly-burn line in both the populated and
#: empty-state forms.
WEEKLY_BURN_LABEL: str = "weekly burn:"

#: Label prefixing the active needs_user badge cell. Kept compact so the
#: second footer row still fits the burn cell + heartbeat dot at 80 cols;
#: it renders in the ``$warn`` attention colour when pauses are pending.
#: ASCII-only per the source-glyph convention.
NEEDS_USER_BADGE_LABEL: str = "needs_user"


def format_needs_user_badge(count: int) -> str:
    """Render the footer needs_user badge text for *count* pending pauses.

    Pure render source — unit-testable without mounting the widget. The
    badge is **quiet** (the empty string, taking no footer space) when
    *count* is ``0`` so an idle surface carries no attention noise (the
    brand-badges-quiet-when-idle convention), and shows
    ``needs_user <count>`` when at least one pause is open. A negative
    count is clamped to ``0`` so a stray decrement never renders a
    nonsensical figure.

    Args:
        count: The number of open needs_user pauses across all scopes.

    Returns:
        The empty string when *count* <= ``0``, else
        ``needs_user <count> `` (a trailing space separates it from the
        heartbeat dot that follows on the same row).
    """
    safe = max(0, count)
    if safe == 0:
        return ""
    return f"{NEEDS_USER_BADGE_LABEL} {safe} "


def format_hints(hints: tuple[str, ...]) -> str:
    """Join key hints into the footer strip with a separating bullet.

    Args:
        hints: The ordered key-hint fragments (full key names).

    Returns:
        The joined hint string, e.g. ``↑↓ move · Enter open · q quit``.
    """
    return "  ·  ".join(hints)


def build_weekly_burn_line(state: State | None, *, now: datetime | None = None) -> str:
    """Build the footer weekly-burn line from *state*.

    Pure render source — unit-testable without mounting the widget. The
    rollup comes from :func:`~eawf.workflow.estimation.metrics.compute_weekly_burn`
    (trailing-7-day actual-EU consumption versus
    ``Project.weekly_eu_target``). The graceful :data:`WEEKLY_BURN_EMPTY`
    placeholder is rendered — never a ``0 / 0`` figure — whenever the line
    has no real data to show, namely when the bound state is ``None``, the
    project has no ``weekly_eu_target`` set, or no actuals have rolled up
    yet (the EU surface is unpopulated scaffolding today).

    Args:
        state: The bound state, or ``None`` before first load.
        now: Optional clock injection threaded to
            :func:`~eawf.workflow.estimation.metrics.compute_weekly_burn` so the
            trailing-7-day window is deterministic in tests. Production
            callers leave this ``None`` to anchor on wall-clock.

    Returns:
        ``weekly burn: <consumed> / <target> EU`` when a target is set and
        actuals exist, else ``weekly burn: — no data``.
    """
    if state is None or not state.actuals:
        return f"{WEEKLY_BURN_LABEL} {WEEKLY_BURN_EMPTY}"
    metric = compute_weekly_burn(state, now=now)
    if metric.target_eu is None:
        return f"{WEEKLY_BURN_LABEL} {WEEKLY_BURN_EMPTY}"
    return f"{WEEKLY_BURN_LABEL} {metric.consumed_eu:g} / {metric.target_eu:g} EU"


class Footer(Static):
    """Shared chassis footer: key hints + weekly-burn cell + heartbeat dot.

    Reused verbatim by every per-scope screen (shared chassis). The
    footer is **two rows**: row 1 is the full-width key-hint strip
    (so the longest scope hint set never clips at 120 cols), row 2 is
    the weekly-burn cell + the :class:`Heartbeat` dot. A host screen may
    override the hints via :meth:`set_hints` without touching the chrome.
    The burn cell is driven by the host
    :class:`~eawf.surfaces.tui.app.EaApp` reactive ``state`` (seeded on mount,
    watched for revisions) and falls back to the
    :data:`WEEKLY_BURN_EMPTY` placeholder when no target / actuals exist.
    Standalone tests assign :attr:`state` directly. Standalone-testable
    via the Pilot harness.
    """

    DEFAULT_CSS: ClassVar[str] = """
    Footer {
        height: 2;
        dock: bottom;
        background: $panel;
        padding: 0 1;
    }
    Footer .footer-hints {
        width: 1fr;
        height: 1;
    }
    Footer .footer-status {
        height: 1;
    }
    Footer .footer-burn {
        width: 1fr;
        height: 1;
        color: $text-muted;
    }
    Footer .footer-needs-user {
        width: auto;
        height: 1;
        color: $text-muted;
    }
    Footer .footer-needs-user.-attention {
        color: $warn;
        text-style: bold;
    }
    """

    #: Active key hints, watched so a host override repaints the strip.
    hints: reactive[tuple[str, ...]] = reactive(DEFAULT_HINTS)

    #: Bound state, watched so a fresh revision repaints the weekly-burn
    #: cell. ``None`` until the first read-only load completes.
    state: reactive[State | None] = reactive(None)

    #: Count of open needs_user pauses across all scopes. Watched so a
    #: change repaints the badge cell + flips its attention colour. The
    #: host App (:class:`~eawf.surfaces.tui.app.EaApp`) pushes the count off
    #: the same pause source the auto-open path reads; standalone tests
    #: assign it directly. Quiet (no count) at ``0``.
    pending_pauses: reactive[int] = reactive(0)

    def compose(self) -> ComposeResult:
        """Lay out the hint strip (row 1) above the status row (row 2).

        Row 2 carries the weekly-burn cell, the needs_user attention
        badge, and the heartbeat dot.
        """
        yield Static(format_hints(self.hints), classes="footer-hints")
        with Horizontal(classes="footer-status"):
            yield Static(build_weekly_burn_line(self.state), classes="footer-burn")
            # The initial attention class is set here (not only in the
            # post-mount watcher) so a count seeded before mount paints
            # attention-coloured on first render rather than waiting for a
            # later change to fire the watcher.
            badge_classes = "footer-needs-user"
            if self.pending_pauses > 0:
                badge_classes += " -attention"
            yield Static(
                format_needs_user_badge(self.pending_pauses),
                classes=badge_classes,
            )
            yield Heartbeat(id="heartbeat")

    def on_mount(self) -> None:
        """Seed the burn line + pause badge from the app and watch them.

        Standalone tests that assign :attr:`state` / :attr:`pending_pauses`
        directly do not need the app watchers; the guards skip them when
        the host exposes no matching attribute (e.g. mounted under a bare
        harness).
        """
        app_state = getattr(self.app, "state", None)
        if app_state is not None and self.state is None:
            self.state = app_state
        if hasattr(self.app, "state"):
            self.watch(self.app, "state", self._on_app_state)
        app_pauses = getattr(self.app, "pending_pauses", None)
        if isinstance(app_pauses, int):
            self.pending_pauses = app_pauses
        if hasattr(self.app, "pending_pauses"):
            self.watch(self.app, "pending_pauses", self._on_app_pending_pauses)
        self._repaint_burn()
        self._repaint_needs_user()

    def _on_app_state(self, new_state: State | None) -> None:
        """Mirror an app-level state change onto this widget's reactive."""
        self.state = new_state

    def _on_app_pending_pauses(self, count: int) -> None:
        """Mirror an app-level pending-pause count onto this widget's reactive."""
        self.pending_pauses = count

    def set_hints(self, hints: tuple[str, ...]) -> None:
        """Replace the footer key hints (scope-specific override).

        Args:
            hints: The ordered key-hint fragments (full key names).
        """
        self.hints = hints

    def watch_hints(self, hints: tuple[str, ...]) -> None:
        """Repaint the hint strip when the hints change.

        Guarded on mount: the child ``Static`` only exists after
        :meth:`compose`, so a pre-mount reactive assignment is a no-op
        (``compose`` reads the current value).
        """
        if not self.is_mounted:
            return
        self.query_one(".footer-hints", Static).update(format_hints(hints))

    def watch_state(self) -> None:
        """Repaint the weekly-burn line when the bound state changes."""
        self._repaint_burn()

    def watch_pending_pauses(self) -> None:
        """Repaint the needs_user badge when the pending-pause count changes."""
        self._repaint_needs_user()

    def _repaint_burn(self) -> None:
        """Re-render the weekly-burn line from the current state.

        Guarded on mount: the child ``Static`` only exists after
        :meth:`compose`, so a pre-mount reactive assignment is a no-op
        (``compose`` reads the current value).
        """
        if not self.is_mounted:
            return
        self.query_one(".footer-burn", Static).update(build_weekly_burn_line(self.state))

    def _repaint_needs_user(self) -> None:
        """Re-render the needs_user badge + flip its attention colour.

        The ``-attention`` class is toggled on whenever at least one pause
        is pending, so the badge draws the eye via the ``$warn`` colour
        when it matters and stays quiet (``$text-muted``, no count) when
        idle. Queries the child defensively: it only exists after
        :meth:`compose`, so a pre-compose reactive write (or a write during
        ``on_mount`` before the widget reports mounted) is a safe no-op —
        the child's compose-time value / ``on_mount`` repaint covers it.
        """
        cells = self.query(".footer-needs-user")
        if not cells:
            return
        cell = cells.first(Static)
        cell.update(format_needs_user_badge(self.pending_pauses))
        cell.set_class(self.pending_pauses > 0, "-attention")


__all__ = [
    "DEFAULT_HINTS",
    "HEARTBEAT_GLYPH",
    "HEARTBEAT_INTERVAL_S",
    "NEEDS_USER_BADGE_LABEL",
    "WEEKLY_BURN_EMPTY",
    "WEEKLY_BURN_LABEL",
    "Footer",
    "Heartbeat",
    "build_weekly_burn_line",
    "format_hints",
    "format_needs_user_badge",
]
