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
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Static

from eawf.kernel.state.enums import AgentReportVerdict
from eawf.kernel.state.ids import natural_key
from eawf.platform.scrub import scan_text
from eawf.surfaces.tui.scopes import ScopeScreen
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE, RenderMode
from eawf.surfaces.tui.widgets.footer import render_hint_label
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph, tint
from eawf.workflow.agent_report.rollup import AgentReportRow, iter_agent_reports
from eawf.workflow.estimation.buckets import wave_estimate_eu

if TYPE_CHECKING:
    from eawf.kernel.state.models import State
    from eawf.kernel.store.kinds.evidence import EvidenceRecord
    from eawf.platform.scrub import ScrubFinding
    from eawf.workflow.verify.models import CloseReadiness, CriterionView

logger = logging.getLogger(__name__)

#: Notice rendered when no agent report exists for the active scope -- the
#: common path on a scope whose waves emitted no reports (no report store
#: on disk). Phrased honestly so the empty surface is unmistakable.
EMPTY_NOTICE: str = "no agent reports yet"


def frame_empty_notice(*, mode: RenderMode = DEFAULT_RENDER_MODE) -> str:
    """Return the no-reports sentinel framed calmly with a muted ring.

    Leads the honest :data:`EMPTY_NOTICE` with the muted PENDING sigil (the
    hollow ring) so the empty surface reads in the reskin's lifecycle
    vocabulary as a not-yet-populated pane rather than a missing one -- a
    calm frame that fabricates no rows. The notice text itself is unchanged;
    the surrounding ``#evidence-empty`` Static keeps its muted-italic CSS.

    Args:
        mode: The App's resolved render-mode label threaded into the sigil
            helper; defaults to the ASCII column for a bare standalone render.

    Returns:
        The framed sentinel string (``<ring> no agent reports yet``).
    """
    ring = glyph(Sigil.PENDING, mode=mode)
    return f"{ring} {EMPTY_NOTICE}"


#: The criterion statuses that count as "ready" toward the close-readiness
#: header tally. A criterion is ready when its gate evidence passed or an
#: operator waiver cleared it; ``fail`` / ``blocked`` / ``pending`` are not
#: ready. Sourced from the closed
#: :data:`~eawf.workflow.verify.models.CriterionStatus` literal.
_READY_STATUSES: frozenset[str] = frozenset({"pass", "waived"})

#: Header shown when the active scope carries no typed criteria -- the
#: honest-empty path for the close-readiness header (a scope whose waves
#: declare only legacy string criteria, or none at all).
NO_CRITERIA_NOTICE: str = "criteria: none"

#: Column ids for the evidence table, in display order. The table is keyed
#: by the WAVE the report advanced: ``report`` carries the wave label joined
#: to the producing role, ``verdict`` carries the tinted lifecycle-shaped
#: verdict sigil, and ``eu`` carries the wave's effort-unit estimate. The
#: per-report attempt + the follow-up titles render in the detail block
#: below the table.
_COLUMNS: tuple[str, ...] = ("report", "verdict", "eu")

#: Map each typed agent-report verdict to the lifecycle :class:`Sigil` shape
#: whose mark + Wong tint reads its meaning at a glance, so the verdict
#: column never prints a raw enum word. A clean ``pass`` (and the
#: ``pass-with-followups`` pass-with-a-tail) wears the CLOSED filled circle
#: (closed green); a ``fail`` wears the FAILED multiplication cross (failed
#: red); a ``blocked`` verdict -- a withheld, not-terminal call -- wears the
#: muted PENDING ring so it reads as held back rather than as a clean pass
#: or a hard fail. Shape comes from the single sigils home; no pane invents
#: a glyph of its own.
_VERDICT_SIGIL: dict[AgentReportVerdict, Sigil] = {
    AgentReportVerdict.PASS: Sigil.CLOSED,
    AgentReportVerdict.PASS_WITH_FOLLOWUPS: Sigil.CLOSED,
    AgentReportVerdict.FAIL: Sigil.FAILED,
    AgentReportVerdict.BLOCKED: Sigil.PENDING,
}


def verdict_sigil(verdict: str, *, mode: RenderMode = DEFAULT_RENDER_MODE) -> Text:
    """Return the tinted lifecycle-shaped sigil for an agent-report *verdict*.

    Resolves the verdict string to its lifecycle :class:`Sigil` shape via
    :data:`_VERDICT_SIGIL` and renders the shape's glyph (in render *mode*)
    tinted with the shape's own Wong hex (:func:`~eawf.surfaces.tui.widgets.sigils.tint`)
    into a :class:`~rich.text.Text` -- the cell form a
    :class:`~textual.widgets.DataTable` styles without a content-markup parse.
    A verdict string outside the closed :class:`~eawf.kernel.state.enums.AgentReportVerdict`
    set falls back to the muted PENDING ring rather than raising, so a row
    never crashes the pane on an unrecognised verdict.

    Args:
        verdict: The report verdict string (an
            :class:`~eawf.kernel.state.enums.AgentReportVerdict` value).
        mode: The App's resolved render-mode label threaded into the sigil
            helper; defaults to the ASCII column for a bare standalone render.

    Returns:
        A tinted single-cell :class:`~rich.text.Text` for the verdict sigil.
    """
    try:
        sigil = _VERDICT_SIGIL[AgentReportVerdict(verdict)]
    except ValueError:
        sigil = Sigil.PENDING
    mark = glyph(sigil, mode=mode)
    hex_tint = tint(sigil)
    return Text(mark, style=hex_tint or "")


#: Footer hints for the Evidence mode (arrows primary). The mode digits are
#: surfaced by the always-visible mode row, not duplicated in the hint strip.
#: Every label is produced through
#: :func:`~eawf.surfaces.tui.widgets.footer.render_hint_label` so the key
#: tokens stay pinned to the canonical vocabulary.
_EVIDENCE_HINTS: tuple[str, ...] = (
    render_hint_label("↑↓", "select"),
    render_hint_label("p", "peek"),
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
        eu: The effort-unit estimate of the wave the report advanced, derived
            from the wave's effort bucket; ``0.0`` when the ``base_id`` joins
            no wave in state (a phase-scoped report) or the wave has no bucket.
    """

    report_id: str
    role: str
    verdict: str
    wave_id: str
    wave_title: str | None
    attempt: int
    summary: str
    followups: tuple[str, ...]
    eu: float = 0.0

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

    @property
    def report_label(self) -> str:
        """Return the report's identity -- the wave it advanced plus the role.

        The table is keyed by wave, so the report column leads with the wave
        label (id + title) and trails the producing role, reading as
        ``<wave-id> <title> :: <role>`` so a multi-role wave's rows are
        distinguishable under the shared wave key.
        """
        return f"{self.wave_label} :: {self.role}"

    @property
    def eu_label(self) -> str:
        """Return the wave EU formatted to two decimals, or a calm dash.

        A report joining no wave (or a wave with no effort bucket) carries
        ``0.0`` EU; that renders as a ``-`` so an honest no-estimate cell is
        unmistakable rather than a misleading ``0.00``.
        """
        if self.eu <= 0.0:
            return "-"
        return f"{self.eu:.2f}"


def _wave_joins(state: State | None) -> dict[str, tuple[str, float]]:
    """Return a ``{wave_id: (title, eu)}`` join map from *state*.

    Args:
        state: The loaded state, or ``None`` (fresh / user scope) -- the
            latter yields an empty map so every row renders with no title
            join and a dashed EU rather than raising.

    Returns:
        A wave-id to ``(title, eu)`` map, where ``eu`` is the bucket-derived
        effort-unit estimate for the wave (``0.0`` when the wave has no
        effort bucket).
    """
    if state is None:
        return {}
    return {wave_id: (wave.title, wave_estimate_eu(wave)) for wave_id, wave in state.waves.items()}


def _row_from_report(
    report: AgentReportRow, wave_joins: dict[str, tuple[str, float]]
) -> EvidenceRow:
    """Join one report row to its wave and project the surfaced fields.

    Args:
        report: The loaded report row.
        wave_joins: The ``{wave_id: (title, eu)}`` join map.

    Returns:
        The projected :class:`EvidenceRow`.
    """
    header = report.payload.header
    body = report.payload.body
    title, eu = wave_joins.get(header.base_id, (None, 0.0))
    return EvidenceRow(
        report_id=header.report_id,
        role=header.role.value,
        verdict=body.verdict.value,
        wave_id=header.base_id,
        wave_title=title,
        attempt=header.attempt,
        summary=header.summary,
        followups=tuple(followup.title for followup in body.followups),
        eu=eu,
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
    wave_joins = _wave_joins(state)
    rows = tuple(_row_from_report(report, wave_joins) for report in reports)
    logger.info(f"build_evidence_rows reports={len(rows)} waves={len(wave_joins)}")
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


# --------------------------------------------------------------------------
# Close-readiness ledger -- typed-criterion view alongside the report rollup
# --------------------------------------------------------------------------


def criterion_ready_count(readiness: CloseReadiness) -> tuple[int, int]:
    """Return the ``(ready, total)`` typed-criterion tally for *readiness*.

    A criterion counts as ready when its rolled-up status is in
    :data:`_READY_STATUSES` (``pass`` / ``waived``); ``fail`` / ``blocked``
    / ``pending`` are not ready. The total is every criterion attached to
    the scope's close-readiness view.

    Args:
        readiness: The derived close-readiness view for the active scope.

    Returns:
        A ``(ready, total)`` pair; ``(0, 0)`` when the view carries no
        criteria (the honest-empty path).
    """
    total = len(readiness.criteria)
    ready = sum(1 for view in readiness.criteria if view.status in _READY_STATUSES)
    return (ready, total)


def close_readiness_header(readiness: CloseReadiness) -> str:
    """Return the one-line close-readiness header for *readiness*.

    Renders the passed-over-total typed-criterion count -- e.g.
    ``criteria: 3/4 ready`` -- so the operator reads how close the scope
    is to a clean close at a glance. A view with no typed criteria yields
    :data:`NO_CRITERIA_NOTICE` rather than a misleading ``0/0`` figure.

    Args:
        readiness: The derived close-readiness view for the active scope.

    Returns:
        The header line, or :data:`NO_CRITERIA_NOTICE` when there are no
        typed criteria.
    """
    ready, total = criterion_ready_count(readiness)
    if total == 0:
        return NO_CRITERIA_NOTICE
    return f"criteria: {ready}/{total} ready"


#: Column ids for the close-readiness ledger table, in display order. One
#: row per typed criterion: its id, the rolled-up gate status, who produced
#: the joined evidence, and the criterion's overall status.
_LEDGER_COLUMNS: tuple[str, ...] = ("criterion", "gate", "produced_by", "status")

#: Dash placeholder rendered in a ledger cell with no joined value -- a
#: criterion with no gate results (gate column) or no joined evidence row
#: (produced_by column). Kept ASCII per the source-glyph convention.
_LEDGER_DASH: str = "-"

#: Notice rendered in the ledger pane when no close-readiness view is bound
#: -- the honest-empty path (no typed criteria to roll up into rows).
LEDGER_EMPTY_NOTICE: str = "no close-readiness criteria"


def gate_status_label(view: CriterionView) -> str:
    """Return the rolled-up gate-status label for a criterion *view*.

    Rolls the criterion's per-gate :class:`~eawf.workflow.verify.models.GateResult`
    statuses into one cell: ``fail`` if any gate failed, else ``blocked`` if
    any is blocked, else ``pass`` when every gate passed. A view with no gate
    results (a legacy / floor criterion) yields :data:`_LEDGER_DASH`.

    Args:
        view: The criterion view whose gate results to roll up.

    Returns:
        The rolled-up gate status (``pass`` / ``fail`` / ``blocked``), or
        :data:`_LEDGER_DASH` when the view carries no gate results.
    """
    results = view.gate_results
    if not results:
        return _LEDGER_DASH
    statuses = {result.status for result in results}
    if "fail" in statuses:
        return "fail"
    if "blocked" in statuses:
        return "blocked"
    return "pass"


@dataclass(frozen=True)
class LedgerRow:
    """One close-readiness ledger row over a typed criterion.

    Attributes:
        criterion_id: The criterion id (e.g. ``CR-01``).
        gate_status: The rolled-up gate status for the criterion
            (:func:`gate_status_label`), or a dash when no gates exist.
        produced_by: Who produced the joined evidence row for this criterion
            (``human`` / ``agent`` / ``tool`` / ``canary``), or a dash when no
            evidence row joins the criterion.
        status: The criterion's overall rolled-up status.
    """

    criterion_id: str
    gate_status: str
    produced_by: str
    status: str


def build_evidence_ledger(
    readiness: CloseReadiness,
    records: Iterable[EvidenceRecord] = (),
) -> tuple[LedgerRow, ...]:
    """Build one ledger row per *readiness* criterion, joining evidence producers.

    Emits exactly one :class:`LedgerRow` per criterion in the close-readiness
    view, in view order. Each row carries the criterion id, the rolled-up gate
    status (:func:`gate_status_label`), the ``produced_by`` of the joined
    evidence row (matched by criterion id against *records*), and the
    criterion's overall status. A criterion with no joined evidence renders a
    dash in the ``produced_by`` column rather than dropping the row.

    Args:
        readiness: The derived close-readiness view for the active scope.
        records: The scope's evidence rows, joined to criteria by id (an
            ``EvidenceRecord`` matches a criterion when its ``refs`` or
            ``metrics["criterion_id"]`` names the criterion id). Defaults to
            no records, so every ``produced_by`` cell shows a dash.

    Returns:
        One ledger row per criterion, in close-readiness view order; empty
        when the view carries no criteria.
    """
    producers = _producer_by_criterion(readiness, records)
    rows = tuple(
        LedgerRow(
            criterion_id=view.id,
            gate_status=gate_status_label(view),
            produced_by=producers.get(view.id, _LEDGER_DASH),
            status=view.status,
        )
        for view in readiness.criteria
    )
    logger.info(f"build_evidence_ledger criteria={len(rows)}")
    return rows


def _producer_by_criterion(
    readiness: CloseReadiness,
    records: Iterable[EvidenceRecord],
) -> dict[str, str]:
    """Map each criterion id to the ``produced_by`` of its joined evidence row.

    A record joins a criterion when the criterion id appears in the record's
    ``refs`` or its ``metrics["criterion_id"]``. The last matching record
    wins, so a re-run's fresh row supersedes an earlier one. Only criteria in
    *readiness* are mapped; an evidence row that names no in-view criterion is
    ignored here (the orphan grouping is the W05 join's job).

    Args:
        readiness: The close-readiness view supplying the criterion id set.
        records: The scope's evidence rows.

    Returns:
        A ``{criterion_id: produced_by}`` map over the joined criteria.
    """
    criterion_ids = {view.id for view in readiness.criteria}
    producers: dict[str, str] = {}
    for record in records:
        for criterion_id in _record_criterion_ids(record):
            if criterion_id in criterion_ids:
                producers[criterion_id] = record.produced_by
    return producers


def _record_criterion_ids(record: EvidenceRecord) -> tuple[str, ...]:
    """Return the criterion ids an evidence *record* names, in stable order.

    A record names a criterion through its ``metrics["criterion_id"]`` entry
    (the deterministic-gate producer stamps it there) and/or its ``refs``
    list. Both sources are folded into one de-duplicated tuple so the join is
    resilient to either shape.

    Args:
        record: The evidence record to inspect.

    Returns:
        The criterion ids the record references, de-duplicated in
        ``metrics`` then ``refs`` order.
    """
    ids: list[str] = []
    metrics = record.metrics or {}
    metric_id = metrics.get("criterion_id")
    if isinstance(metric_id, str):
        ids.append(metric_id)
    ids.extend(record.refs)
    seen: set[str] = set()
    ordered: list[str] = []
    for criterion_id in ids:
        if criterion_id not in seen:
            seen.add(criterion_id)
            ordered.append(criterion_id)
    return tuple(ordered)


#: Section heading for evidence rows that join no in-scope criterion -- the
#: "orphan" bucket the evidence->criterion join groups unmatched rows under.
ORPHAN_SECTION: str = "orphan"


@dataclass(frozen=True)
class EvidenceJoin:
    """The result of joining evidence rows to typed criteria.

    Attributes:
        matched: A ``{criterion_id: (records, ...)}`` map -- the evidence rows
            that join each in-scope criterion, keyed by criterion id. A
            criterion with no joining evidence is absent from the map (callers
            that want a row per criterion read the close-readiness view, not
            this join).
        orphans: The evidence rows that join NO in-scope criterion, in input
            order. Grouped here under the :data:`ORPHAN_SECTION` heading so an
            evidence row referencing an unknown / dropped criterion stays
            visible rather than silently vanishing.
    """

    matched: dict[str, tuple[EvidenceRecord, ...]]
    orphans: tuple[EvidenceRecord, ...]


def join_evidence_to_criteria(
    criterion_ids: Iterable[str],
    records: Iterable[EvidenceRecord],
) -> EvidenceJoin:
    """Join *records* to the *criterion_ids* they reference, bucketing orphans.

    Maps each :class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord` to the
    in-scope criterion id it references (via its ``metrics["criterion_id"]`` or
    its ``refs``); a record that references at least one in-scope criterion
    lands under every such id in :attr:`EvidenceJoin.matched`, and a record
    that references none lands in :attr:`EvidenceJoin.orphans`. The orphan
    bucket is the load-bearing half: an evidence row whose criterion was
    dropped (or never existed) stays visible under the :data:`ORPHAN_SECTION`
    heading rather than silently disappearing.

    Args:
        criterion_ids: The in-scope criterion ids (typically the close-
            readiness view's criterion ids) a record may join.
        records: The scope's evidence rows.

    Returns:
        The :class:`EvidenceJoin` of matched groups + the orphan bucket.
    """
    in_scope = set(criterion_ids)
    matched: dict[str, list[EvidenceRecord]] = {}
    orphans: list[EvidenceRecord] = []
    for record in records:
        joined = [cid for cid in _record_criterion_ids(record) if cid in in_scope]
        if not joined:
            orphans.append(record)
            continue
        for criterion_id in joined:
            matched.setdefault(criterion_id, []).append(record)
    frozen = {cid: tuple(rows) for cid, rows in matched.items()}
    logger.info(f"join_evidence_to_criteria matched={len(frozen)} orphans={len(orphans)}")
    return EvidenceJoin(matched=frozen, orphans=tuple(orphans))


# --------------------------------------------------------------------------
# Scrub-gated evidence export -- a clean manifest, refusing host-path leaks
# --------------------------------------------------------------------------


class EvidenceScrubError(ValueError):
    """Raised when an evidence export payload carries unscrubbed host tokens.

    The export gate scans every text-bearing payload value with the project
    scrub scanner (:func:`eawf.platform.scrub.scan_text`); a payload carrying
    a host path (``/Users/...`` / ``/home/...``), a private IP, a local
    hostname, or a non-allowlisted email is refused rather than emitted, so a
    machine-local leak never rides an exported manifest off the operator's box.

    Attributes:
        findings: The scanner findings that tripped the refusal, in byte order.
    """

    def __init__(self, findings: list[ScrubFinding]) -> None:
        """Build the error naming each offending token kind.

        Args:
            findings: The scrub findings that tripped the refusal.
        """
        self.findings = findings
        kinds = ", ".join(sorted({finding.kind for finding in findings}))
        super().__init__(f"evidence export carries unscrubbed token(s): {kinds}")


def build_evidence_manifest(
    readiness: CloseReadiness,
    records: Iterable[EvidenceRecord] = (),
) -> dict[str, object]:
    """Build the exportable evidence manifest from a close-readiness view.

    Assembles a plain ``dict`` -- one criterion entry per ledger row (id,
    gate status, produced_by, status) plus the joined evidence rows (id,
    produced_by, status, summary) -- so the manifest round-trips through JSON
    losslessly. This is the raw payload; :func:`export_evidence_manifest`
    applies the scrub gate before it is emitted.

    Args:
        readiness: The derived close-readiness view to export.
        records: The scope's evidence rows, joined to criteria by id.

    Returns:
        The manifest dict (``{"criteria": [...], "evidence": [...]}``).
    """
    rows = list(records)
    ledger = build_evidence_ledger(readiness, rows)
    criteria = [
        {
            "id": row.criterion_id,
            "gate_status": row.gate_status,
            "produced_by": row.produced_by,
            "status": row.status,
        }
        for row in ledger
    ]
    evidence = [
        {
            "id": record.id,
            "produced_by": record.produced_by,
            "status": record.status,
            "summary": record.summary,
        }
        for record in rows
    ]
    return {"criteria": criteria, "evidence": evidence}


def export_evidence_manifest(
    readiness: CloseReadiness,
    records: Iterable[EvidenceRecord] = (),
) -> dict[str, object]:
    """Emit a scrub-clean evidence manifest, refusing a host-path leak.

    Builds the manifest (:func:`build_evidence_manifest`), scans every
    text-bearing value through the project scrub scanner
    (:func:`eawf.platform.scrub.scan_text`), and returns the manifest only when
    it is clean. A payload carrying a host path, a private IP, a local
    hostname, or a non-allowlisted email is refused -- so a machine-local leak
    never rides an exported manifest off the operator's box.

    Args:
        readiness: The derived close-readiness view to export.
        records: The scope's evidence rows, joined to criteria by id.

    Returns:
        The scrub-clean manifest dict.

    Raises:
        EvidenceScrubError: When any payload value carries an unscrubbed
            host token. The error names each offending token kind and carries
            the raw findings on :attr:`EvidenceScrubError.findings`.
    """
    manifest = build_evidence_manifest(readiness, records)
    findings = _scan_manifest(manifest)
    if findings:
        logger.info(f"export_evidence_manifest refused findings={len(findings)}")
        raise EvidenceScrubError(findings)
    logger.info("export_evidence_manifest clean")
    return manifest


def _scan_manifest(manifest: dict[str, object]) -> list[ScrubFinding]:
    """Scan every string value in *manifest* for unscrubbed host tokens.

    Walks the manifest's nested ``criteria`` / ``evidence`` lists and scans
    each string field with :func:`eawf.platform.scrub.scan_text`, accumulating
    every finding so the refusal names all offending token kinds at once.

    Args:
        manifest: The manifest dict produced by :func:`build_evidence_manifest`.

    Returns:
        The scrub findings across every scanned string value (empty when the
        manifest is clean).
    """
    findings: list[ScrubFinding] = []
    for section in manifest.values():
        if not isinstance(section, list):
            continue
        for entry in section:
            if not isinstance(entry, dict):
                continue
            for value in entry.values():
                if isinstance(value, str):
                    findings.extend(scan_text(value))
    return findings


class EvidenceModeScreen(ScopeScreen):
    """Evidence-mode base screen rendering the agent-report rollup.

    Composes the shared chassis around a bordered pane carrying the rollup
    summary line, a wave-keyed :class:`~textual.widgets.DataTable` of
    per-report rows (report identity / tinted verdict sigil / wave EU), and a
    follow-up detail block. Renders honest-empty (the calmly-framed muted
    :data:`EMPTY_NOTICE`) whenever no report exists for the active scope --
    the common path.

    The screen self-binds to the host :class:`~eawf.surfaces.tui.app.EaApp`
    reactive ``state``: it seeds from ``app.state`` + ``app._state_path`` on
    mount and rebuilds when a daemon-pushed revision lands, so a report
    written after launch surfaces without a relaunch.
    """

    FOOTER_HINTS: ClassVar[tuple[str, ...]] = _EVIDENCE_HINTS

    #: ``p`` peeks into the selected ledger criterion's evidence chain (the
    #: why-peek drill modal). Appended to the shared
    #: :class:`~eawf.surfaces.tui.scopes.ScopeScreen` chrome bindings so the
    #: advertised ``p peek`` footer key resolves to a live binding.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("p", "drill", "peek", show=False),
    ]

    DEFAULT_CSS: ClassVar[str] = """
    EvidenceModeScreen #evidence-body {
        padding: 0 1;
    }
    EvidenceModeScreen .evidence-readiness {
        height: 1;
        color: $accent;
        text-style: bold;
    }
    EvidenceModeScreen #evidence-ledger {
        height: auto;
        max-height: 10;
        overflow-x: hidden;
    }
    EvidenceModeScreen .evidence-ledger-empty {
        height: 1;
        color: $text-muted;
        text-style: italic;
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

    def __init__(self) -> None:
        """Construct the Evidence mode screen with no bound readiness view."""
        super().__init__()
        #: The close-readiness view painted into the ledger header + table.
        #: ``None`` is the honest-empty path -- the render seam never calls
        #: :func:`~eawf.workflow.verify.readiness.compute` (it spawns live
        #: gate subprocesses), so a readiness view is supplied externally
        #: (the daemon close envelope today, a fixture under test) via
        #: :meth:`set_readiness`.
        self._readiness: CloseReadiness | None = None
        #: Evidence rows joined into the ledger's ``produced_by`` column,
        #: supplied alongside the readiness view via :meth:`set_readiness`.
        self._records: tuple[EvidenceRecord, ...] = ()

    def compose_body(self) -> ComposeResult:
        """Yield the evidence pane body (readiness header + ledger + rollup)."""
        with Vertical(id="evidence-body"):
            yield Static(NO_CRITERIA_NOTICE, id="evidence-readiness", classes="evidence-readiness")
            yield Static(
                LEDGER_EMPTY_NOTICE, id="evidence-ledger-empty", classes="evidence-ledger-empty"
            )
            yield DataTable(id="evidence-ledger", cursor_type="row", zebra_stripes=True)
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
        ledger = self.query_one("#evidence-ledger", DataTable)
        for column in _LEDGER_COLUMNS:
            ledger.add_column(column, key=column)
        app_state = getattr(self.app, "state", None)
        if app_state is not None and self.state is None:
            self.state = app_state
        if hasattr(self.app, "state"):
            self.watch(self.app, "state", self._on_app_state)
        if hasattr(self.app, "render_mode"):
            self.watch(self.app, "render_mode", self._on_render_mode)
        self._rebuild()

    def _on_app_state(self, new_state: State | None) -> None:
        """Mirror an app-level state change onto this screen's reactive."""
        self.state = new_state

    def _on_render_mode(self, _mode: RenderMode) -> None:
        """Repaint the verdict-sigil column when the render mode swaps."""
        if self.is_mounted:
            self._rebuild()

    def _render_mode(self) -> RenderMode:
        """Return the host app's resolved render-mode label, else the default."""
        return getattr(self.app, "render_mode", DEFAULT_RENDER_MODE)

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
        joins each report to the wave it advanced (title + EU) via the bound
        state, painting the wave-keyed table (report identity / tinted verdict
        sigil / wave EU). Honest-empty is the common path: no report store on
        disk yields no rows, so the calmly-framed muted :data:`EMPTY_NOTICE`
        shows and the table hides.
        """
        table = self.query_one("#evidence-table", DataTable)
        if not table.columns:
            return
        rows = sort_evidence_rows(build_evidence_rows(self._state_path(), self.state))
        mode = self._render_mode()
        summary = self.query_one("#evidence-summary", Static)
        empty = self.query_one("#evidence-empty", Static)
        followups = self.query_one("#evidence-followups", Static)
        summary.update(evidence_summary_line(rows))
        followups.update(render_followups_block(rows))
        empty.update(frame_empty_notice(mode=mode))
        table.clear()
        for row in rows:
            table.add_row(
                row.report_label,
                verdict_sigil(row.verdict, mode=mode),
                row.eu_label,
                key=row.report_id,
            )
        has_rows = bool(rows)
        table.display = has_rows
        empty.display = not has_rows
        self._paint_readiness()
        logger.info(f"evidence_rebuild rows={len(rows)} has_rows={has_rows}")

    def set_readiness(
        self,
        readiness: CloseReadiness | None,
        records: Iterable[EvidenceRecord] = (),
    ) -> None:
        """Bind a close-readiness view (+ evidence rows) and repaint the ledger.

        The render seam never calls
        :func:`~eawf.workflow.verify.readiness.compute` (it spawns live gate
        subprocesses), so the readiness view + its evidence rows are supplied
        externally and pushed in here. Repaints the header + the ledger table
        immediately when the pane is mounted.

        Args:
            readiness: The derived close-readiness view to bind, or ``None``
                to clear it back to the honest-empty header + ledger.
            records: The scope's evidence rows joined into the ledger's
                ``produced_by`` column. Defaults to none.
        """
        self._readiness = readiness
        self._records = tuple(records)
        if self.is_mounted:
            self._paint_readiness()

    def _paint_readiness(self) -> None:
        """Repaint the close-readiness header + ledger from the bound view.

        Renders :func:`close_readiness_header` into the header row and one
        :class:`LedgerRow` per criterion (:func:`build_evidence_ledger`) into
        the ledger table; falls back to :data:`NO_CRITERIA_NOTICE` /
        :data:`LEDGER_EMPTY_NOTICE` when no view is bound. Guarded on mount:
        the header / table widgets only exist after :meth:`compose_body`.
        """
        headers = self.query("#evidence-readiness")
        if not headers:
            return
        header = headers.first(Static)
        ledger = self.query_one("#evidence-ledger", DataTable)
        empty = self.query_one("#evidence-ledger-empty", Static)
        if not ledger.columns:
            return
        ledger.clear()
        if self._readiness is None:
            header.update(NO_CRITERIA_NOTICE)
            ledger.display = False
            empty.display = True
            return
        header.update(close_readiness_header(self._readiness))
        ledger_rows = build_evidence_ledger(self._readiness, self._records)
        for row in ledger_rows:
            ledger.add_row(
                row.criterion_id,
                row.gate_status,
                row.produced_by,
                row.status,
                key=row.criterion_id,
            )
        has_rows = bool(ledger_rows)
        ledger.display = has_rows
        empty.display = not has_rows

    def action_drill(self) -> None:
        """Open the why-peek drill modal for the selected ledger criterion.

        Resolves the highlighted ledger row to its
        :class:`~eawf.workflow.verify.models.CriterionView`, joins the bound
        evidence rows to it (:func:`join_evidence_to_criteria`), and pushes the
        :class:`~eawf.surfaces.tui.modals.evidence_drill.EvidenceDrillModal`
        through the App's cap-aware ``push_modal`` (falling back to
        ``push_screen`` under a bare harness). A no-op when no readiness view
        is bound or the ledger has no selectable criterion -- the binding still
        resolves, so the advertised ``p peek`` affordance is never dead.
        """
        view = self._selected_criterion()
        if view is None:
            logger.info("evidence_drill skipped reason=no-selection")
            return
        records = join_evidence_to_criteria([view.id], self._records).matched.get(view.id, ())
        from eawf.surfaces.tui.modals.evidence_drill import EvidenceDrillModal

        modal = EvidenceDrillModal(view, records)
        push_modal = getattr(self.app, "push_modal", None)
        if callable(push_modal):
            push_modal(modal)
            return
        self.app.push_screen(modal)

    def _selected_criterion(self) -> CriterionView | None:
        """Resolve the highlighted ledger row to its criterion view, or ``None``.

        Reads the ledger's row cursor and maps the highlighted row key (the
        criterion id) back to the bound close-readiness view's criterion.
        Returns ``None`` when no readiness view is bound, the ledger carries no
        rows, or the cursor resolves to no in-view criterion.

        Returns:
            The selected :class:`~eawf.workflow.verify.models.CriterionView`,
            or ``None`` when no criterion is selectable.
        """
        if self._readiness is None:
            return None
        ledgers = self.query("#evidence-ledger")
        if not ledgers:
            return None
        ledger = ledgers.first(DataTable)
        if ledger.row_count == 0:
            return None
        try:
            row_key = ledger.coordinate_to_cell_key(ledger.cursor_coordinate).row_key
        except Exception:
            # The cursor may sit outside any cell on an empty / unfocused
            # table; treat that as no selection rather than propagating.
            return None
        criterion_id = row_key.value
        for view in self._readiness.criteria:
            if view.id == criterion_id:
                return view
        return None


__all__ = [
    "EMPTY_NOTICE",
    "LEDGER_EMPTY_NOTICE",
    "NO_CRITERIA_NOTICE",
    "ORPHAN_SECTION",
    "EvidenceJoin",
    "EvidenceModeScreen",
    "EvidenceRow",
    "EvidenceScrubError",
    "LedgerRow",
    "build_evidence_ledger",
    "build_evidence_manifest",
    "build_evidence_rows",
    "close_readiness_header",
    "criterion_ready_count",
    "evidence_summary_line",
    "export_evidence_manifest",
    "frame_empty_notice",
    "gate_status_label",
    "join_evidence_to_criteria",
    "render_followups_block",
    "sort_evidence_rows",
    "verdict_sigil",
]
