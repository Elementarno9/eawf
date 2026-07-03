"""``ReportDetailModal`` -- the agent-report detail overlay.

Opens one agent report from the Evidence mode's report rollup: a small
centred :class:`~textual.screen.ModalScreen` that renders the report's
**verdict**, a **wave-status** line when the joined wave terminal-failed
(so a self-claimed pass on a failed wave is self-explanatory), its
**summary**, a **research** section for a researcher report (question ->
findings -> recommendation), the **evidence refs** it cited, the
**followups** it emitted, and its **attempt provenance** (which role
produced which attempt, on which runtime, when). ``Esc`` closes.

The overlay is opened from the Evidence mode's advertised ``Enter open``
key over a highlighted agent-report row, so the operator can read the
full report an at-a-glance rollup row only summarises -- without leaving
the pane.

The card content is assembled by pure module functions
(:func:`report_ref_lines` / :func:`report_followup_lines` /
:func:`report_provenance_line` / :func:`render_report_detail`) so the
content is unit-testable without mounting Textual; the screen is a thin
scrollable view over them, mirroring the
:class:`~eawf.surfaces.tui.modals.evidence_drill.EvidenceDrillModal`
convention. The modal holds no domain logic: it presents a pre-resolved
:class:`~eawf.surfaces.tui.modes.evidence.EvidenceRow` and renders it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE, RenderMode
from eawf.surfaces.tui.widgets.markup import escape_markup
from eawf.surfaces.tui.widgets.sigils import chrome

if TYPE_CHECKING:
    from eawf.surfaces.tui.modes.evidence import EvidenceRow

logger = logging.getLogger(__name__)

#: Rendered in the evidence-refs section when the report cited none -- the
#: honest-empty refs path.
NO_EVIDENCE_REFS_NOTICE: str = "no evidence refs"

#: Rendered in the followups section when the report emitted none -- the
#: honest-empty followups path.
NO_FOLLOWUPS_NOTICE: str = "no followups"


def report_ref_lines(refs: tuple[str, ...]) -> tuple[str, ...]:
    """Return one line per evidence ref the report cited, in report order.

    Each ref is already projected to a ``<kind>: <ref>`` display string on
    the :class:`~eawf.surfaces.tui.modes.evidence.EvidenceRow`. An empty
    *refs* yields a single :data:`NO_EVIDENCE_REFS_NOTICE` line.

    Args:
        refs: The report's projected evidence-ref strings.

    Returns:
        The per-ref lines, or a one-element tuple carrying
        :data:`NO_EVIDENCE_REFS_NOTICE` when there are none.
    """
    if not refs:
        return (NO_EVIDENCE_REFS_NOTICE,)
    return refs


def report_followup_lines(followups: tuple[str, ...]) -> tuple[str, ...]:
    """Return one line per follow-up title the report emitted, in order.

    An empty *followups* yields a single :data:`NO_FOLLOWUPS_NOTICE` line.

    Args:
        followups: The report's follow-up titles.

    Returns:
        The per-follow-up lines, or a one-element tuple carrying
        :data:`NO_FOLLOWUPS_NOTICE` when there are none.
    """
    if not followups:
        return (NO_FOLLOWUPS_NOTICE,)
    return followups


def report_provenance_line(row: EvidenceRow) -> str:
    """Return the report's one-line attempt provenance.

    Names which role produced the attempt and its number, then the runtime
    that produced it and its generation timestamp when known -- e.g.
    ``executor attempt 2 via codex at 2026-06-01T12:00:00+00:00``. An unknown
    runtime / timestamp (an empty field) is dropped so the line never trails a
    blank ``via`` / ``at``.

    Args:
        row: The evidence row whose report provenance to render.

    Returns:
        The attempt-provenance line.
    """
    parts = [f"{row.role} attempt {row.attempt}"]
    if row.runtime:
        parts.append(f"via {row.runtime}")
    if row.generated_at:
        parts.append(f"at {row.generated_at}")
    return " ".join(parts)


def report_wave_status_line(row: EvidenceRow) -> str | None:
    """Return the failed-wave status line, or ``None`` when the wave did not fail.

    When the joined wave terminal-failed
    (:attr:`~eawf.surfaces.tui.modes.evidence.EvidenceRow.wave_failed`), a
    report claiming ``pass`` is only a SELF-claim -- the wave's own
    :class:`~eawf.kernel.state.enums.WaveStatus` says otherwise. This names the
    wave's real status beside the self-claimed verdict so a ``@ x`` row is
    self-explanatory in the drill. A non-failed wave (or a report joining no
    wave) yields ``None`` so the caller renders no status line.

    Args:
        row: The evidence row whose wave status to render.

    Returns:
        The failed-wave status line, or ``None`` when the wave did not fail.
    """
    if not row.wave_failed:
        return None
    status = row.wave_status.upper() if row.wave_status else "FAILED"
    return (
        f"wave status: {status} - wave terminal-failed at verify; agent self-claimed {row.verdict}"
    )


def report_researcher_lines(row: EvidenceRow) -> tuple[str, ...]:
    """Return the researcher-body detail lines for *row*, or empty when not one.

    A researcher report carries a question, findings, considered alternatives,
    and a recommendation the common header fields drop -- so a campaign
    researcher row would otherwise drill to only the repeated campaign topic.
    This projects those fields to 2-space-indented display lines
    (``question`` -> ``findings`` -> ``alternatives`` -> ``recommendation``) so
    the drill shows what the report actually found. A non-researcher row (no
    question) yields an empty tuple so the caller renders no research section.

    Args:
        row: The evidence row whose researcher body to render.

    Returns:
        The researcher detail lines, or an empty tuple for a non-researcher row.
    """
    if not row.question:
        return ()
    lines = [f"question: {row.question}"]
    if row.findings:
        lines.append("findings:")
        lines.extend(f"  - {finding}" for finding in row.findings)
    if row.alternatives:
        lines.append("alternatives:")
        lines.extend(f"  - {alternative}" for alternative in row.alternatives)
    lines.append(f"recommendation: {row.recommendation}")
    return tuple(lines)


def render_report_detail(row: EvidenceRow) -> str:
    """Render the full agent-report detail for *row* as one text block.

    Lays out the report header (``<wave-label> :: <role>``), the verdict, a
    wave-status line when the joined wave terminal-failed
    (:func:`report_wave_status_line`), the summary, a researcher section when
    the row carries a researcher body (:func:`report_researcher_lines`), the
    evidence-refs section (:func:`report_ref_lines`), the followups section
    (:func:`report_followup_lines`), and the attempt provenance
    (:func:`report_provenance_line`) so the whole report reads top-to-bottom in
    one block -- the same content the modal paints, exposed pure for tests.

    Args:
        row: The evidence row whose report to render.

    Returns:
        The newline-joined report-detail block (no trailing newline).
    """
    lines = [f"{row.wave_label} :: {row.role}", f"verdict: {row.verdict}"]
    wave_status = report_wave_status_line(row)
    if wave_status is not None:
        lines.append(wave_status)
    lines.append("summary:")
    lines.append(f"  {row.summary}")
    research = report_researcher_lines(row)
    if research:
        lines.append("research:")
        lines.extend(f"  {line}" for line in research)
    lines.append("evidence refs:")
    lines.extend(f"  {line}" for line in report_ref_lines(row.evidence_refs))
    lines.append("followups:")
    lines.extend(f"  {line}" for line in report_followup_lines(row.followups))
    lines.append("provenance:")
    lines.append(f"  {report_provenance_line(row)}")
    return "\n".join(lines)


class ReportDetailModal(ModalScreen[None]):
    """Report-open overlay rendering one agent report's fields (Esc closes).

    Renders the report's verdict, a failed-wave status line when the joined
    wave terminal-failed, summary, a researcher section (question / findings /
    recommendation) for a researcher report, cited evidence refs, emitted
    followups, and attempt provenance in a scrollable card. Built thin over
    the pure render helpers so the content is testable without Textual; the
    modal owns no mutation -- it presents a pre-resolved
    :class:`~eawf.surfaces.tui.modes.evidence.EvidenceRow` and never reaches
    back into the rollup or the report store.
    """

    #: One report overlay per report at a time -- a re-fired open over the
    #: same report already on top is a no-op (deduped by
    #: :meth:`~eawf.surfaces.tui.app.EaApp.push_modal` on the dedupe key).
    dedupe_singleton: ClassVar[bool] = False

    DEFAULT_CSS: ClassVar[str] = """
    ReportDetailModal {
        align: center middle;
    }
    ReportDetailModal > #report-box {
        width: 70%;
        max-width: 90;
        height: auto;
        max-height: 80%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    ReportDetailModal .report-title {
        text-style: bold;
        color: $accent;
        height: 1;
    }
    ReportDetailModal .report-verdict {
        color: $text;
        height: 1;
    }
    ReportDetailModal .report-wave-failed {
        color: $error;
        text-style: bold;
        height: auto;
    }
    ReportDetailModal .report-section {
        text-style: bold;
        color: $accent;
        margin-top: 1;
        height: 1;
    }
    ReportDetailModal .report-row {
        height: auto;
        color: $text;
    }
    ReportDetailModal .report-hint {
        color: $text-muted;
        margin-top: 1;
        height: 1;
    }
    """

    #: ``Esc`` closes the report overlay; the only binding it owns.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "close", show=False),
    ]

    def __init__(self, row: EvidenceRow, *, mode: RenderMode = DEFAULT_RENDER_MODE) -> None:
        """Construct the report detail over a pre-resolved *row*.

        Args:
            row: The evidence row whose report to surface full-field.
            mode: The App's resolved render-mode label, threaded into the
                title chrome mark; defaults to the ASCII column for a bare
                standalone render.
        """
        super().__init__()
        self._row = row
        self._mode = mode
        #: Dedupe key keyed on the report id so the App push chokepoint
        #: suppresses a duplicate open of the same report.
        self.dedupe_key = f"report-detail:{row.report_id}"

    def compose(self) -> ComposeResult:
        """Yield the scrollable card: title, verdict, summary, refs, followups."""
        gate = chrome("gate", mode=self._mode)
        title = escape_markup(f"{self._row.wave_label} :: {self._row.role}")
        with VerticalScroll(id="report-box"):
            yield Static(f"[$accent]{gate}[/] report: {title}", classes="report-title")
            yield Static(f"verdict: {escape_markup(self._row.verdict)}", classes="report-verdict")
            wave_status = report_wave_status_line(self._row)
            if wave_status is not None:
                yield Static(f"{escape_markup(wave_status)}", classes="report-wave-failed")
            yield Static("Summary", classes="report-section")
            yield Static(f"  {escape_markup(self._row.summary)}", classes="report-row")
            research = report_researcher_lines(self._row)
            if research:
                yield Static("Research", classes="report-section")
                for line in research:
                    yield Static(f"  {escape_markup(line)}", classes="report-row")
            yield Static("Evidence refs", classes="report-section")
            for line in report_ref_lines(self._row.evidence_refs):
                yield Static(f"  {escape_markup(line)}", classes="report-row")
            yield Static("Followups", classes="report-section")
            for line in report_followup_lines(self._row.followups):
                yield Static(f"  {escape_markup(line)}", classes="report-row")
            yield Static("Provenance", classes="report-section")
            yield Static(
                f"  {escape_markup(report_provenance_line(self._row))}", classes="report-row"
            )
            yield Static("[ Esc to close ]", classes="report-hint")

    def action_close(self) -> None:
        """Dismiss the report overlay (``Esc``)."""
        logger.info(f"report_detail_close report={self._row.report_id!r}")
        self.dismiss(None)


__all__ = [
    "NO_EVIDENCE_REFS_NOTICE",
    "NO_FOLLOWUPS_NOTICE",
    "ReportDetailModal",
    "render_report_detail",
    "report_followup_lines",
    "report_provenance_line",
    "report_ref_lines",
    "report_researcher_lines",
    "report_wave_status_line",
]
