"""``RunSummaryModal`` -- the FA7 fleet run-summary terminal card.

When the daemon-owned fleet auto-drain loop
(:func:`eawf.runtime.daemon.methods.fleet.drive`) reaches a terminal stop, the
:class:`~eawf.surfaces.tui.modes.autopilot.AutopilotModeScreen` opens this card
over the cockpit: a one-screen debrief of the whole run. It names WHICH of the
three terminal reasons ended the run (``drained`` -- the frontier emptied,
``converged`` -- a convergence criterion met early, or ``budget`` -- a spend cap
fired) in its header, then lays out the run totals beneath: the ``N closed /
M failed / K blocked`` lane tally, the EU + $ spend totals, the elapsed window,
the forks-resolved count, and the per-wave outcome list. ``Enter`` (or ``Esc``)
returns to the cockpit.

Reads-not-recomputes (the load-bearing C2 honesty)
--------------------------------------------------
Every figure on the card is read STRAIGHT off the persisted
:class:`~eawf.kernel.state.models.FleetRun` the daemon stamped at the terminal
transition -- its :class:`~eawf.kernel.state.models.FleetCounters` tallies, its
``elapsed_hours`` window, its ``terminal_reason``, and the per-lane outcome the
counters describe. The card NEVER re-derives a tally in the UI (it does not
re-count lanes, re-sum spend, or re-time the run), so the debrief matches the
daemon's terminal record byte-for-byte. The autopilot pane passes the persisted
run in; this overlay only formats it.

The overlay holds NO run logic -- it is a read-only debrief. There is no
``fleet.drive`` follow-up, no re-arm: the run is over, and the card's only verb
is dismiss-back-to-cockpit.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from eawf.kernel.state.models import FleetRun
from eawf.surfaces.tui.widgets.markup import escape_markup
from eawf.surfaces.tui.widgets.sigils import chrome

logger = logging.getLogger(__name__)

#: Render-mode label threaded into the sigil helper when the host App exposes no
#: ``render_mode`` (a bare standalone harness): the unicode column is the
#: default surface, ``"ascii"`` only when the App resolves it.
_DEFAULT_RENDER_MODE: str = "unicode"

#: Human-facing header phrase per terminal reason -- names WHICH stop ended the
#: run (C1). Keyed by the :class:`~eawf.kernel.state.models.FleetTerminalReason`
#: string value so the lookup stays decoupled from importing the enum, and so a
#: run whose reason somehow drifted past the map still renders (the fallback
#: names the raw reason rather than crashing the debrief).
_TERMINAL_HEADLINE: dict[str, str] = {
    "drained": "frontier drained -- every ready wave closed or blocked",
    "converged": "converged -- clean-round criterion met before the frontier emptied",
    "budget": "budget halt -- a spend cap stopped the run",
}

#: Card title prefix -- the literal that leads the run-summary header, before
#: the terminal-reason headline.
RUN_SUMMARY_TITLE: str = "fleet run complete"

#: Id of the card box (the centred modal container).
RUN_SUMMARY_BOX_ID: str = "run-summary-box"

#: Id of the title / terminal-reason header row.
RUN_SUMMARY_HEADER_ID: str = "run-summary-header"

#: Id of the counts row (``N closed / M failed / K blocked``).
RUN_SUMMARY_COUNTS_ID: str = "run-summary-counts"

#: Id of the totals row (EU / $ / elapsed / forks-resolved).
RUN_SUMMARY_TOTALS_ID: str = "run-summary-totals"

#: Id of the per-wave outcome list container.
RUN_SUMMARY_OUTCOMES_ID: str = "run-summary-outcomes"

#: Id of the key-hint footer row.
RUN_SUMMARY_HINT_ID: str = "run-summary-hint"

#: The key-hint footer vocab, mirroring the other reskin overlays: a calm
#: bracketed chord under the card. The card's only verb is return-to-cockpit.
_KEY_HINT: str = "[ Enter / Esc return to cockpit ]"

#: Heading above the persisted outcome-class tally rows.
OUTCOMES_CAPTION: str = "outcome tallies"

#: Honest-empty line when the run recorded no finished lane outcome (a run that
#: ended before any lane closed): the list says so rather than rendering blank.
OUTCOMES_EMPTY: str = "no wave outcomes recorded"


def terminal_headline(run: FleetRun) -> str:
    """Return the human-facing header phrase naming *run*'s terminal reason (C1).

    Resolves the :attr:`~eawf.kernel.state.models.FleetRun.terminal_reason` to
    its :data:`_TERMINAL_HEADLINE` phrase so the card header NAMES which of the
    three stops (drained / converged / budget) ended the run. A run with no
    terminal reason recorded (the card opened before the daemon stamped it) or a
    reason that drifted past the map reads an honest fallback rather than a
    blank header.

    Args:
        run: The persisted, terminal fleet run.

    Returns:
        The terminal-reason headline phrase.
    """
    if run.terminal_reason is None:
        return "run ended -- terminal reason not recorded"
    reason = run.terminal_reason.value
    return _TERMINAL_HEADLINE.get(reason, f"run ended -- {reason}")


def render_counts_row(run: FleetRun) -> str:
    """Render the ``N closed / M failed / K blocked`` lane-tally row off *run*.

    Reads the three closed-lane tallies STRAIGHT off
    :class:`~eawf.kernel.state.models.FleetCounters`
    (:attr:`~eawf.kernel.state.models.FleetCounters.closed` /
    :attr:`~eawf.kernel.state.models.FleetCounters.failed` /
    :attr:`~eawf.kernel.state.models.FleetCounters.blocked`) -- never recounted
    in the UI -- so the tally matches the daemon's terminal record.

    Args:
        run: The persisted, terminal fleet run.

    Returns:
        A content-markup counts row string.
    """
    counters = run.counters
    return (
        f"[$ok]{counters.closed} closed[/] [$muted]/[/] "
        f"[$err]{counters.failed} failed[/] [$muted]/[/] "
        f"[$warn]{counters.blocked} blocked[/]"
    )


def format_elapsed(hours: float | None) -> str:
    """Render an elapsed-hours window as a compact ``[#h]##m`` string.

    A raw decimal-hours float (``0.03h``) reads poorly for the sub-hour runs a
    fleet drive usually takes, so collapse it to whole minutes, prefixing the
    hour count only once the window reaches an hour. ``None`` (no window
    recorded) reads an honest dash rather than a fabricated zero.

    Args:
        hours: The daemon-stamped elapsed window in fractional hours, or
            ``None`` when no window was recorded.

    Returns:
        ``"--"`` when *hours* is ``None``; ``"##m"`` under an hour; ``"#h##m"``
        (zero-padded minutes) at or above an hour.
    """
    if hours is None:
        return "--"
    total_minutes = round(hours * 60)
    whole_hours, minutes = divmod(total_minutes, 60)
    if whole_hours == 0:
        return f"{minutes}m"
    return f"{whole_hours}h{minutes:02d}m"


def render_totals_row(run: FleetRun) -> str:
    """Render the EU / $ / elapsed / forks-resolved totals row off *run*.

    Reads the spend totals (:attr:`FleetCounters.spent_eu` /
    :attr:`FleetCounters.spent_usd`), the daemon-stamped
    :attr:`~eawf.kernel.state.models.FleetRun.elapsed_hours` window, and the
    :attr:`FleetCounters.forks_resolved` tally STRAIGHT off the persisted run --
    the elapsed window is the figure the daemon computed once at run end, never
    re-timed in the UI. A run with no elapsed window recorded reads an honest
    dash rather than a fabricated zero.

    Args:
        run: The persisted, terminal fleet run.

    Returns:
        A content-markup totals row string.
    """
    counters = run.counters
    elapsed = format_elapsed(run.elapsed_hours)
    return (
        f"[$muted]EU[/] [$accent]{counters.spent_eu:.2f}[/]  "
        f"[$muted]$[/] [$accent]{counters.spent_usd:.2f}[/]  "
        f"[$muted]elapsed[/] [$accent]{escape_markup(elapsed)}[/]  "
        f"[$muted]forks resolved[/] [$accent]{counters.forks_resolved}[/]"
    )


def outcome_lines(run: FleetRun) -> tuple[str, ...]:
    """Project *run*'s closed-lane tallies into per-wave outcome list lines.

    The persisted :class:`FleetCounters` tallies the lane outcomes by class
    (``closed`` / ``failed`` / ``blocked`` / ``forks_resolved``) rather than
    naming each wave, so the outcome list reads back the per-class tally the
    daemon recorded -- one labelled line per non-zero outcome class, in
    severity-descending order. This stays a READ of the persisted counters (it
    re-sums nothing) so the list matches the terminal record. A run with no
    finished lane recorded yields the honest-empty marker.

    Args:
        run: The persisted, terminal fleet run.

    Returns:
        The per-wave outcome list lines (each a content-markup row); a single
        honest-empty line when no outcome was recorded.
    """
    counters = run.counters
    rows: list[str] = []
    if counters.closed:
        rows.append(f"[$ok]closed[/] [$muted]x{counters.closed}[/]")
    if counters.failed:
        rows.append(f"[$err]failed[/] [$muted]x{counters.failed}[/]")
    if counters.blocked:
        rows.append(f"[$warn]blocked[/] [$muted]x{counters.blocked}[/]")
    if counters.forks_resolved:
        rows.append(f"[$accent]fork resolved[/] [$muted]x{counters.forks_resolved}[/]")
    if not rows:
        return (f"[$muted]{OUTCOMES_EMPTY}[/]",)
    return tuple(rows)


class RunSummaryModal(ModalScreen[None]):
    """The FA7 fleet run-summary terminal card (dismisses ``None`` on return).

    A centred read-only debrief opened over the cockpit when the fleet
    auto-drain loop reaches a terminal stop. The header NAMES which terminal
    reason (``drained`` / ``converged`` / ``budget``) ended the run; the body
    lays out the ``N closed / M failed / K blocked`` lane tally, the EU + $
    spend totals, the elapsed window, the forks-resolved count, and the per-wave
    outcome list. Every figure is read STRAIGHT off the persisted
    :class:`~eawf.kernel.state.models.FleetRun`, so the card matches the daemon's
    terminal record exactly. ``Enter`` / ``Esc`` returns to the cockpit (the card
    has no other verb -- the run is over).
    """

    DEFAULT_CSS: ClassVar[str] = """
    RunSummaryModal {
        align: center middle;
    }
    RunSummaryModal > #run-summary-box {
        width: 90%;
        min-width: 36;
        max-width: 96;
        height: auto;
        max-height: 90%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    RunSummaryModal .run-summary-title {
        height: auto;
        text-style: bold;
        margin-bottom: 1;
    }
    RunSummaryModal #run-summary-counts {
        height: auto;
    }
    RunSummaryModal #run-summary-totals {
        height: auto;
        margin-bottom: 1;
    }
    RunSummaryModal .run-summary-caption {
        height: 1;
        color: $text-muted;
    }
    RunSummaryModal #run-summary-outcomes {
        height: auto;
        max-height: 12;
        margin-top: 1;
    }
    RunSummaryModal .run-summary-outcome {
        height: auto;
    }
    RunSummaryModal #run-summary-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    #: ``Enter`` / ``Esc`` both return to the cockpit -- the card's only verb.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "dismiss_card", "return", show=False),
        Binding("escape", "dismiss_card", "return", show=False),
    ]

    def __init__(self, run: FleetRun) -> None:
        """Construct the card for the persisted terminal *run*.

        Args:
            run: The persisted, terminal :class:`FleetRun` the card debriefs --
                every figure on the card is read off it (never recomputed).
        """
        super().__init__()
        self._run = run

    def compose(self) -> ComposeResult:
        """Yield the header, counts, totals, per-wave outcome list, and the hint."""
        with Vertical(id=RUN_SUMMARY_BOX_ID):
            yield Static(self._header_line(), classes="run-summary-title", id=RUN_SUMMARY_HEADER_ID)
            yield Static(render_counts_row(self._run), id=RUN_SUMMARY_COUNTS_ID)
            yield Static(render_totals_row(self._run), id=RUN_SUMMARY_TOTALS_ID)
            yield Static(f"[$muted]{OUTCOMES_CAPTION}[/]", classes="run-summary-caption")
            with VerticalScroll(id=RUN_SUMMARY_OUTCOMES_ID):
                for line in outcome_lines(self._run):
                    yield Static(line, classes="run-summary-outcome")
            yield Static(_KEY_HINT, id=RUN_SUMMARY_HINT_ID)

    def on_mount(self) -> None:
        """Watch for a render-mode flip so the header sigil repaints in its column."""
        if hasattr(self.app, "render_mode"):
            self.watch(self.app, "render_mode", self._on_render_mode)

    def _on_render_mode(self, _mode: object) -> None:
        """Repaint the header sigil when the App's render mode flips."""
        self.query_one(f"#{RUN_SUMMARY_HEADER_ID}", Static).update(self._header_line())

    def _render_mode(self) -> str:
        """Resolve the active render-mode label from the host app.

        Returns:
            The render-mode label (``"ascii"`` or a unicode label).
        """
        return getattr(self.app, "render_mode", _DEFAULT_RENDER_MODE)

    def _header_line(self) -> str:
        """Render the card header: the title sigil + title + terminal-reason headline.

        The header NAMES which terminal reason ended the run (C1) -- the title
        leads with the gate chrome sigil, then the :data:`RUN_SUMMARY_TITLE`
        literal, then the resolved :func:`terminal_headline` phrase.
        """
        sigil = chrome("gate", mode=self._render_mode())
        headline = escape_markup(terminal_headline(self._run))
        return f"[$accent]{sigil} {RUN_SUMMARY_TITLE}[/]  [$muted]{headline}[/]"

    def action_dismiss_card(self) -> None:
        """Dismiss the card and return to the cockpit (``Enter`` / ``Esc``)."""
        reason = self._run.terminal_reason.value if self._run.terminal_reason else "unknown"
        logger.info(f"run_summary_modal dismissed terminal_reason={reason!r}")
        self.dismiss(None)


__all__ = [
    "OUTCOMES_CAPTION",
    "OUTCOMES_EMPTY",
    "RUN_SUMMARY_BOX_ID",
    "RUN_SUMMARY_COUNTS_ID",
    "RUN_SUMMARY_HEADER_ID",
    "RUN_SUMMARY_HINT_ID",
    "RUN_SUMMARY_OUTCOMES_ID",
    "RUN_SUMMARY_TITLE",
    "RUN_SUMMARY_TOTALS_ID",
    "RunSummaryModal",
    "outcome_lines",
    "render_counts_row",
    "render_totals_row",
    "terminal_headline",
]
