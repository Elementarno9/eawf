"""``EvidenceModeScreen`` -- the Evidence mode pane over the agent reports.

The Evidence mode (digit ``4``) renders the typed agent-report rollup for
the active scope: one row per report joining its role, verdict, the wave
it advanced (``base_id``), and the count of follow-ups it emitted, plus a
detail block listing every surfaced follow-up. The rollup is the
:func:`~eawf.workflow.agent_report.rollup.iter_agent_reports` reader over
the scope's role-report stores (``<state_dir>/store/<role>_report.jsonl``).

Honest-empty is the COMMON path, not an edge case: a scope that has
produced no agent reports yet (the live ``state.json`` is exactly this)
has no report stores on disk, so :func:`build_evidence_rows` returns an
empty tuple and the pane renders a muted ``no agent reports yet`` notice.
The pane is built for that case first and populates cleanly once reports
land.

The screen subclasses :class:`~eawf.surfaces.tui.scopes.ScopeScreen` so it
inherits the shared chassis (Header brand + breadcrumb + Footer) verbatim;
only :meth:`compose_body` differs. The join + format logic lives in pure
module functions (:func:`build_evidence_rows`, :func:`evidence_summary_line`,
:func:`render_followups_block`) so the rollup view is unit-testable without
mounting Textual, mirroring the widget-catalog convention.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Static

from eawf.kernel.state.ids import natural_key
from eawf.surfaces.tui.scopes import ScopeScreen
from eawf.surfaces.tui.widgets.footer import render_hint_label
from eawf.workflow.agent_report.rollup import AgentReportRow, iter_agent_reports

if TYPE_CHECKING:
    from eawf.kernel.state.models import State

logger = logging.getLogger(__name__)

#: Notice rendered when no agent report exists for the active scope -- the
#: common path on a scope whose waves emitted no reports (no report store
#: on disk). Phrased honestly so the empty surface is unmistakable.
EMPTY_NOTICE: str = "no agent reports yet"

#: Column ids for the evidence table, in display order. ``followups`` is the
#: trailing count column; the per-follow-up titles render in the detail
#: block below the table.
_COLUMNS: tuple[str, ...] = ("role", "verdict", "wave", "attempt", "followups")

#: Footer hints for the Evidence mode (arrows primary). The mode digits are
#: surfaced by the always-visible mode row, not duplicated in the hint strip.
#: Every label is produced through
#: :func:`~eawf.surfaces.tui.widgets.footer.render_hint_label` so the key
#: tokens stay pinned to the canonical vocabulary.
_EVIDENCE_HINTS: tuple[str, ...] = (
    render_hint_label("↑↓", "move"),
    render_hint_label("w/r/u", "scope"),
    render_hint_label("/", "palette"),
    render_hint_label("?", "help"),
    render_hint_label("q", "quit"),
)


@dataclass(frozen=True)
class EvidenceRow:
    """One agent report joined to the wave it advanced.

    Attributes:
        report_id: The report record id (e.g. ``AR-executor-P29-I02-W22-01``).
        role: The agent role value (e.g. ``executor``).
        verdict: The report verdict value (e.g. ``pass``).
        wave_id: The ``base_id`` the report advanced -- a wave id when the
            report joins a planned wave, else the raw base scope.
        wave_title: The joined wave title when the ``base_id`` resolves to a
            wave in state, else ``None`` (no join -- e.g. a phase-scoped
            report or a wave absent from the loaded state).
        attempt: The report attempt number (``>= 1``).
        summary: The report's one-line header summary.
        followups: The follow-up titles the report emitted, in order.
    """

    report_id: str
    role: str
    verdict: str
    wave_id: str
    wave_title: str | None
    attempt: int
    summary: str
    followups: tuple[str, ...]

    @property
    def followup_count(self) -> int:
        """Return the number of follow-ups this report emitted."""
        return len(self.followups)

    @property
    def wave_label(self) -> str:
        """Return the wave id plus its joined title when one resolved."""
        if self.wave_title is None:
            return self.wave_id
        return f"{self.wave_id} {self.wave_title}"


def _wave_titles(state: State | None) -> dict[str, str]:
    """Return a ``{wave_id: title}`` map from *state*, empty when unbound.

    Args:
        state: The loaded state, or ``None`` (fresh / user scope) -- the
            latter yields an empty map so every row renders with no title
            join rather than raising.

    Returns:
        A wave-id to title map.
    """
    if state is None:
        return {}
    return {wave_id: wave.title for wave_id, wave in state.waves.items()}


def _row_from_report(report: AgentReportRow, wave_titles: dict[str, str]) -> EvidenceRow:
    """Join one report row to its wave title and project the surfaced fields.

    Args:
        report: The loaded report row.
        wave_titles: The ``{wave_id: title}`` join map.

    Returns:
        The projected :class:`EvidenceRow`.
    """
    header = report.payload.header
    body = report.payload.body
    return EvidenceRow(
        report_id=header.report_id,
        role=header.role.value,
        verdict=body.verdict.value,
        wave_id=header.base_id,
        wave_title=wave_titles.get(header.base_id),
        attempt=header.attempt,
        summary=header.summary,
        followups=tuple(followup.title for followup in body.followups),
    )


def build_evidence_rows(state_path: Path | None, state: State | None) -> tuple[EvidenceRow, ...]:
    """Build the evidence rows for the active scope's agent reports.

    Reads every role-report store under *state_path* via
    :func:`~eawf.workflow.agent_report.rollup.iter_agent_reports` (already
    sorted by ``(created_at, id)``) and joins each report to the wave it
    advanced (``base_id``) using the titles in *state*. Returns an empty
    tuple -- the COMMON path -- when *state_path* is ``None`` or no report
    store exists on disk, so the pane renders honest-empty rather than
    crashing on a scope that has produced no reports.

    Args:
        state_path: Path to the scope's ``state.json``; the role-report
            stores resolve under its sibling ``store/`` directory. ``None``
            (user scope / no resolved state) yields no rows.
        state: The loaded state supplying the wave-title join, or ``None``
            (rows then render with no title join).

    Returns:
        The evidence rows in report (chronological) order; empty when no
        report exists for the scope.
    """
    if state_path is None:
        return ()
    reports = iter_agent_reports(state_path)
    wave_titles = _wave_titles(state)
    rows = tuple(_row_from_report(report, wave_titles) for report in reports)
    logger.info(f"build_evidence_rows reports={len(rows)} waves={len(wave_titles)}")
    return rows


def evidence_summary_line(rows: tuple[EvidenceRow, ...]) -> str:
    """Return a one-line rollup summary over *rows*.

    Reports the report count and the follow-up total so the operator reads
    the scope's evidence shape at a glance. An empty *rows* yields the
    honest-empty :data:`EMPTY_NOTICE`.

    Args:
        rows: The evidence rows for the active scope.

    Returns:
        The summary line, or :data:`EMPTY_NOTICE` when there are no rows.
    """
    if not rows:
        return EMPTY_NOTICE
    followups = sum(row.followup_count for row in rows)
    report_word = "report" if len(rows) == 1 else "reports"
    return f"{len(rows)} {report_word} - {followups} followup(s)"


def render_followups_block(rows: tuple[EvidenceRow, ...]) -> str:
    """Render the per-report follow-up detail block.

    Lists each report that emitted at least one follow-up under a ``wave ::
    role`` heading, one ``- <title>`` line per follow-up, so the operator
    sees the actionable tail the verdict column only counts. Reports with no
    follow-up are omitted. Returns ``no followups`` when no report surfaced
    any (including the honest-empty no-reports case).

    Args:
        rows: The evidence rows for the active scope.

    Returns:
        The follow-up detail block, or ``no followups`` when none exist.
    """
    lines: list[str] = []
    for row in rows:
        if not row.followups:
            continue
        lines.append(f"{row.wave_id} :: {row.role}")
        lines.extend(f"  - {title}" for title in row.followups)
    if not lines:
        return "no followups"
    return "\n".join(lines)


def sort_evidence_rows(rows: tuple[EvidenceRow, ...]) -> tuple[EvidenceRow, ...]:
    """Return *rows* ordered by wave id (natural), then role, then attempt.

    The reader hands rows back in report (chronological) order; the pane
    groups them by the wave they advanced so a multi-role wave reads as a
    block. The natural wave-id key sorts ``W2`` before ``W10``.

    Args:
        rows: The evidence rows to order.

    Returns:
        A new tuple in display order (the input is not mutated).
    """
    return tuple(sorted(rows, key=lambda row: (natural_key(row.wave_id), row.role, row.attempt)))


class EvidenceModeScreen(ScopeScreen):
    """Evidence-mode base screen rendering the agent-report rollup.

    Composes the shared chassis around a bordered pane carrying the rollup
    summary line, a :class:`~textual.widgets.DataTable` of per-report rows
    (role / verdict / wave / attempt / follow-up count), and a follow-up
    detail block. Renders honest-empty (the muted :data:`EMPTY_NOTICE`)
    whenever no report exists for the active scope -- the common path.

    The screen self-binds to the host :class:`~eawf.surfaces.tui.app.EaApp`
    reactive ``state``: it seeds from ``app.state`` + ``app._state_path`` on
    mount and rebuilds when a daemon-pushed revision lands, so a report
    written after launch surfaces without a relaunch.
    """

    FOOTER_HINTS: ClassVar[tuple[str, ...]] = _EVIDENCE_HINTS

    DEFAULT_CSS: ClassVar[str] = """
    EvidenceModeScreen #evidence-body {
        padding: 0 1;
    }
    EvidenceModeScreen .evidence-summary {
        height: 1;
        color: $accent;
        text-style: bold;
    }
    EvidenceModeScreen #evidence-table {
        height: 1fr;
        overflow-x: hidden;
    }
    EvidenceModeScreen .evidence-empty {
        height: 1fr;
        color: $text-muted;
        text-style: italic;
    }
    EvidenceModeScreen .evidence-followups-title {
        height: 1;
        color: $accent;
        text-style: bold;
    }
    EvidenceModeScreen .evidence-followups {
        height: auto;
        max-height: 8;
        color: $text;
    }
    """

    #: Bound state, watched so a fresh revision rebuilds the rollup rows.
    state: reactive[State | None] = reactive(None)

    def compose_body(self) -> ComposeResult:
        """Yield the evidence pane body (summary + table + follow-up block)."""
        with Vertical(id="evidence-body"):
            yield Static(EMPTY_NOTICE, id="evidence-summary", classes="evidence-summary")
            yield Static(EMPTY_NOTICE, id="evidence-empty", classes="evidence-empty")
            yield DataTable(id="evidence-table", cursor_type="row", zebra_stripes=True)
            yield Static("Followups", classes="evidence-followups-title")
            yield Static("no followups", id="evidence-followups", classes="evidence-followups")

    def on_mount(self) -> None:
        """Add columns, seed from app state, and watch for revisions."""
        super().on_mount()
        table = self.query_one("#evidence-table", DataTable)
        for column in _COLUMNS:
            table.add_column(column, key=column)
        app_state = getattr(self.app, "state", None)
        if app_state is not None and self.state is None:
            self.state = app_state
        if hasattr(self.app, "state"):
            self.watch(self.app, "state", self._on_app_state)
        self._rebuild()

    def _on_app_state(self, new_state: State | None) -> None:
        """Mirror an app-level state change onto this screen's reactive."""
        self.state = new_state

    def watch_state(self) -> None:
        """Rebuild the rollup rows when the bound state changes."""
        if self.is_mounted:
            self._rebuild()

    def _state_path(self) -> Path | None:
        """Return the host app's ``state.json`` path, or ``None``."""
        path = getattr(self.app, "_state_path", None)
        return path if isinstance(path, Path) else None

    def _rebuild(self) -> None:
        """Repopulate the summary, table, and follow-up block from state.

        Reads the role-report stores under the app's ``state.json`` path and
        joins each report to its wave title via the bound state. Honest-empty
        is the common path: no report store on disk yields no rows, so the
        muted :data:`EMPTY_NOTICE` shows and the table hides.
        """
        table = self.query_one("#evidence-table", DataTable)
        if not table.columns:
            return
        rows = sort_evidence_rows(build_evidence_rows(self._state_path(), self.state))
        summary = self.query_one("#evidence-summary", Static)
        empty = self.query_one("#evidence-empty", Static)
        followups = self.query_one("#evidence-followups", Static)
        summary.update(evidence_summary_line(rows))
        followups.update(render_followups_block(rows))
        table.clear()
        for row in rows:
            table.add_row(
                row.role,
                row.verdict,
                row.wave_label,
                str(row.attempt),
                str(row.followup_count),
                key=row.report_id,
            )
        has_rows = bool(rows)
        table.display = has_rows
        empty.display = not has_rows
        logger.info(f"evidence_rebuild rows={len(rows)} has_rows={has_rows}")


__all__ = [
    "EMPTY_NOTICE",
    "EvidenceModeScreen",
    "EvidenceRow",
    "build_evidence_rows",
    "evidence_summary_line",
    "render_followups_block",
    "sort_evidence_rows",
]
