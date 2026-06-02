"""``Footer`` + ``Heartbeat`` — shared chassis footer (widget catalog).

A single footer composite reused by every per-scope screen
(``RepoScreen`` / ``WorkspaceScreen`` / ``UserScreen``) with **no
per-scope duplication**. The footer is **two rows** tall:

* **Row 1** merges the context-aware key-hint strip (left) with the
  status cells (right): the weekly-burn cell, the needs_user attention
  badge, and a live :class:`Heartbeat` dot. Hints use **full key names**
  only (``PageUp`` / ``PageDown`` / ``Enter`` / ``Esc`` — never ``PgUp``)
  per the operator keymap convention.
* **Row 2** is the always-visible **mode row**: every registered mode
  rendered as ``<digit> <title>`` (derived from
  :data:`~eawf.surfaces.tui.modes.registry.MODE_REGISTRY`), so the operator
  sees and can reach all modes at a glance. The **active** mode's token is
  highlighted (bold accent); the rest render muted.

The heartbeat is a ``•`` pulse that proves the TUI is alive,
``accent``-coloured by default and ``err``-coloured when any pane is
degraded, with a 0.5 s double-pulse ack on the ``F5`` force-refresh
keypress.

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
from eawf.surfaces.tui.widgets.markup import escape_markup
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
#: first footer row still fits the burn cell + heartbeat dot at 80 cols;
#: it renders in the ``$warn`` attention colour when pauses are pending.
#: ASCII-only per the source-glyph convention.
NEEDS_USER_BADGE_LABEL: str = "needs_user"

#: Separator between mode-row tokens. The same bullet
#: :func:`format_hints` joins key hints with, so the mode row and the hint
#: strip read as one visual family.
MODE_ROW_SEP: str = "  ·  "


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


def build_mode_row(active_mode: str | None) -> str:
    """Build the always-visible footer mode row from the mode registry.

    Pure render source — unit-testable without mounting the widget. Reads
    :data:`~eawf.surfaces.tui.modes.registry.MODE_REGISTRY` (imported lazily
    to avoid an import cycle, since the registry pulls the screen/app graph)
    and renders one ``<digit> <title>`` token per mode in registry (digit)
    order, lowercased to match the operator example
    (``1 home · 2 autopilot · ...``), joined by :data:`MODE_ROW_SEP`.

    The token whose mode name equals *active_mode* is highlighted with a
    bold accent span (the brand / heartbeat accent convention); every other
    token renders muted. When *active_mode* is ``None`` (or names no
    registered mode — e.g. a bare test harness whose Textual default mode
    is ``"_default"``) no token is highlighted, so the row stays honest
    rather than implying a mode that is not active. Titles are
    markup-escaped defensively so a bracket in a future title can never be
    parsed as a style tag.

    Args:
        active_mode: The active mode name (``app.current_mode``), or
            ``None`` when no mode is resolvable.

    Returns:
        A Textual content-markup string for the mode row.
    """
    from eawf.surfaces.tui.modes.registry import MODE_REGISTRY

    tokens: list[str] = []
    for spec in MODE_REGISTRY:
        label = f"{spec.digit} {escape_markup(spec.title.lower())}"
        if spec.name == active_mode:
            tokens.append(f"[$accent][b]{label}[/b][/]")
        else:
            tokens.append(f"[$muted]{label}[/]")
    return MODE_ROW_SEP.join(tokens)


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
    """Shared chassis footer: hints + status (row 1) + mode row (row 2).

    Reused verbatim by every per-scope screen (shared chassis). The
    footer is **two rows**: row 1 merges the key-hint strip (left) with
    the weekly-burn cell + needs_user badge + the :class:`Heartbeat` dot
    (right); row 2 is the always-visible mode row — every registered mode
    rendered ``<digit> <title>`` with the active mode highlighted. A host
    screen may override the hints via :meth:`set_hints` without touching
    the chrome. The burn cell is driven by the host
    :class:`~eawf.surfaces.tui.app.EaApp` reactive ``state`` (seeded on mount,
    watched for revisions) and falls back to the
    :data:`WEEKLY_BURN_EMPTY` placeholder when no target / actuals exist.
    The mode row's highlight is seeded from ``app.current_mode`` on mount
    (each mode owns its own scope screen, so the footer mounts fresh on
    every mode switch and reads the now-active mode); standalone tests
    assign :attr:`active_mode` directly. Standalone-testable via the Pilot
    harness.
    """

    DEFAULT_CSS: ClassVar[str] = """
    Footer {
        height: 2;
        dock: bottom;
        background: $panel;
        padding: 0 1;
    }
    Footer .footer-row1 {
        height: 1;
    }
    Footer .footer-hints {
        width: 1fr;
        height: 1;
    }
    Footer .footer-burn {
        width: auto;
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
    Footer .footer-modes {
        width: 1fr;
        height: 1;
        color: $text-muted;
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

    #: Active mode name (``app.current_mode``), watched so a change
    #: repaints the mode row's highlight. ``None`` until seeded from the
    #: app on mount; standalone tests assign it directly. A value naming
    #: no registered mode (a bare harness's Textual default) highlights
    #: nothing.
    active_mode: reactive[str | None] = reactive(None)

    def compose(self) -> ComposeResult:
        """Lay out the hints+status row (row 1) above the mode row (row 2).

        Row 1 is a Horizontal carrying the full-width hint strip (left) and
        the weekly-burn cell, the needs_user attention badge, and the
        heartbeat dot (right). Row 2 is the always-visible mode row.
        """
        with Horizontal(classes="footer-row1"):
            yield Static(format_hints(self.hints), classes="footer-hints")
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
        yield Static(build_mode_row(self.active_mode), classes="footer-modes")

    def on_mount(self) -> None:
        """Seed the burn line + pause badge + mode row from the app and watch them.

        Standalone tests that assign :attr:`state` / :attr:`pending_pauses`
        / :attr:`active_mode` directly do not need the app watchers; the
        guards skip them when the host exposes no matching attribute (e.g.
        mounted under a bare harness).
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
        # Seed the mode-row highlight from the active mode. Each mode owns
        # its own scope screen, so the footer mounts fresh on every mode
        # switch and reads the now-active ``current_mode`` here. Subscribe
        # to the app's mode-change signal too (when present) so a shared
        # footer would still repaint on a flip -- the same defensive seam
        # the Header uses.
        current_mode = getattr(self.app, "current_mode", None)
        if isinstance(current_mode, str):
            self.active_mode = current_mode
        mode_signal = getattr(self.app, "mode_change_signal", None)
        if mode_signal is not None:
            mode_signal.subscribe(self, self._on_mode_change)
        self._repaint_burn()
        self._repaint_needs_user()
        self._repaint_modes()

    def _on_app_state(self, new_state: State | None) -> None:
        """Mirror an app-level state change onto this widget's reactive."""
        self.state = new_state

    def _on_app_pending_pauses(self, count: int) -> None:
        """Mirror an app-level pending-pause count onto this widget's reactive."""
        self.pending_pauses = count

    def _on_mode_change(self, mode: str) -> None:
        """Mirror an app-level mode change onto this widget's reactive."""
        self.active_mode = mode

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

    def watch_active_mode(self) -> None:
        """Repaint the mode row's highlight when the active mode changes."""
        self._repaint_modes()

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

    def _repaint_modes(self) -> None:
        """Re-render the mode row with the active mode highlighted.

        Queries the child defensively: it only exists after :meth:`compose`,
        so a pre-compose reactive write (or a write during ``on_mount``
        before the widget reports mounted) is a safe no-op — the child's
        compose-time value / ``on_mount`` repaint covers it.
        """
        cells = self.query(".footer-modes")
        if not cells:
            return
        cells.first(Static).update(build_mode_row(self.active_mode))


__all__ = [
    "DEFAULT_HINTS",
    "HEARTBEAT_GLYPH",
    "HEARTBEAT_INTERVAL_S",
    "MODE_ROW_SEP",
    "NEEDS_USER_BADGE_LABEL",
    "WEEKLY_BURN_EMPTY",
    "WEEKLY_BURN_LABEL",
    "Footer",
    "Heartbeat",
    "build_mode_row",
    "build_weekly_burn_line",
    "format_hints",
    "format_needs_user_badge",
]
