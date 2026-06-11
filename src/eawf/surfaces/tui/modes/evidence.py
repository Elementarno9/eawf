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

from eawf.kernel.spec.common import OracleTier, tier_label
from eawf.kernel.state.enums import AgentReportVerdict
from eawf.kernel.state.ids import natural_key
from eawf.platform.scrub import scan_text
from eawf.surfaces.tui.scopes import ScopeScreen
from eawf.surfaces.tui.widgets.empty_state import HONEST_EMPTY_CSS, render_empty_state
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE, RenderMode
from eawf.surfaces.tui.widgets.footer import render_hint_label
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph, status_sigil, tint
from eawf.workflow.agent_report.rollup import AgentReportRow, iter_agent_reports
from eawf.workflow.estimation.buckets import wave_estimate_eu

if TYPE_CHECKING:
    from eawf.kernel.state.models import State
    from eawf.kernel.store.kinds.evidence import EvidenceRecord
    from eawf.observability.eval.cross_vendor_jury import PerItemJurorBallot
    from eawf.platform.scrub import ScrubFinding
    from eawf.workflow.verify.models import CloseReadiness, CriterionView

logger = logging.getLogger(__name__)

#: Notice rendered when no agent report exists for the active scope -- the
#: common path on a scope whose waves emitted no reports (no report store
#: on disk). Phrased honestly so the empty surface is unmistakable.
EMPTY_NOTICE: str = "no agent reports yet"


def frame_empty_notice(*, mode: RenderMode = DEFAULT_RENDER_MODE) -> str:
    """Return the no-reports honest-empty hero body, centered + calm.

    Routes the honest :data:`EMPTY_NOTICE` through the shared
    :func:`~eawf.surfaces.tui.widgets.empty_state.render_empty_state` hero so
    the no-reports rollup region reads as the calm centered hero (a muted
    brand sigil over a ``$muted`` headline) -- the same hero the research
    board + sandbox timeline render -- rather than a top-left lifecycle-ring
    one-liner, fabricating no rows. Calm rather than alarmed: agent reports
    accrue as waves close, so the empty surface is a not-yet state, not a
    fault, and the headline wears ``$muted``. No action chip: a report is
    produced by an agent closing a wave, not by an operator key here. The
    centering is the surrounding ``#evidence-empty`` Static's
    :data:`~eawf.surfaces.tui.widgets.empty_state.HONEST_EMPTY_CSS` rule.

    Args:
        mode: The App's resolved render-mode label threaded into the brand
            sigil's glyph column; defaults to the ASCII column for a bare
            standalone render.

    Returns:
        The centered honest-empty hero body for the ``#evidence-empty`` Static.
    """
    return render_empty_state(EMPTY_NOTICE, mode=mode, headline_tint="$muted")


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


def verdict_sigil(verdict: str, *, mode: RenderMode = DEFAULT_RENDER_MODE) -> Text:
    """Return the tinted ratified sigil for an agent-report *verdict*.

    Resolves the verdict string through the single extended resolver
    (:func:`~eawf.surfaces.tui.widgets.sigils.status_sigil`) so the verdict
    column wears the same ratified glyph + tint (+ follow-up badge) every
    other pane renders for :class:`~eawf.kernel.state.enums.AgentReportVerdict`
    -- a ``blocked`` verdict wears the warn-tinted withheld mark, not a
    pending ring, and ``pass-with-followups`` trails its badge. The result
    is a :class:`~rich.text.Text` -- the cell form a
    :class:`~textual.widgets.DataTable` styles without a content-markup parse.
    A verdict string outside the closed enum set falls back to the muted
    PENDING ring rather than raising, so a row never crashes the pane on an
    unrecognised verdict.

    Args:
        verdict: The report verdict string (an
            :class:`~eawf.kernel.state.enums.AgentReportVerdict` value).
        mode: The App's resolved render-mode label threaded into the sigil
            helper; defaults to the ASCII column for a bare standalone render.

    Returns:
        A tinted :class:`~rich.text.Text` for the verdict sigil.
    """
    try:
        resolved = status_sigil(AgentReportVerdict(verdict))
    except ValueError:
        return Text(glyph(Sigil.PENDING, mode=mode), style=tint(Sigil.PENDING) or "")
    return Text(resolved.render(mode=mode), style=resolved.tint_hex or "")


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
# Jury-ballot oracle-tier drill (U2) -- the juror x rubric-item grid + ladder
# --------------------------------------------------------------------------

#: The per-cell ballot vote vocabulary for the juror x rubric-item grid. A
#: juror that passed the item reads ``pass``, one that vetoed it ``fail``, and
#: one that cast no vote on the item ``abstain`` (a non-vote is never a silent
#: pass). These stay the canonical data words on :class:`BallotGridRow.votes`;
#: the grid CELL renders the pinned sigil glyph (:func:`vote_sigil`) so the
#: surface wears the designer pin-strip vote vocabulary (filled circle / cross /
#: hollow ring) rather than bare words. Kept ASCII per the source-glyph
#: convention.
PASS_VOTE: str = "pass"
FAIL_VOTE: str = "fail"
ABSTAIN_VOTE: str = "abstain"

#: The vote-word -> lifecycle :class:`Sigil` pin: the designer pin-strip (U2)
#: pins each ballot vote to a sigil from the ratified alphabet -- ``pass`` to the
#: CLOSED filled circle, ``fail`` to the FAILED multiplication cross, ``abstain``
#: to the PENDING hollow ring. The grid cell renders the glyph through this map
#: so the votes read as the shared lifecycle shapes, not bare words; an unknown
#: vote falls back to the PENDING ring.
_VOTE_SIGIL: dict[str, Sigil] = {
    PASS_VOTE: Sigil.CLOSED,
    FAIL_VOTE: Sigil.FAILED,
    ABSTAIN_VOTE: Sigil.PENDING,
}

#: Row-status word when at least one juror voted ``fail`` on a rubric item:
#: one credible refutation blocks the row under the refute-first rubric
#: (minority-veto). A row with no fail (every juror passed or abstained) reads
#: ``ready``.
ROW_BLOCKED: str = "blocked"
ROW_READY: str = "ready"

#: Notice rendered in the ballot sub-pane when no jury ballots are bound -- the
#: honest-empty path (a wave the close gate never convened a jury for, or whose
#: ballots were reduced away before they could be drilled into). Pinned to the
#: designer pin-strip (U2): the jury-runs clause names WHEN a jury convenes (a
#: UI-band high-risk close), so the empty surface reads as a deliberate "no jury
#: was needed here" rather than a missing one. The em-dash is the rendered
#: U+2014 the pin-strip carries verbatim.
NO_BALLOTS_NOTICE: str = "no jury ballots yet — jury runs on UI-band high-risk closes"

#: Mark prefixing the oracle tier that scored the criterion in the ladder; a
#: non-scoring tier is prefixed by spaces of equal width so the ladder stays
#: column-aligned. ASCII per the source-glyph convention.
_TIER_MARK: str = ">"
_TIER_NOMARK: str = " "


@dataclass(frozen=True)
class BallotGridRow:
    """One rubric item's row in the juror x rubric-item ballot grid.

    Attributes:
        item_id: The rubric behaviour id (a ``B<n>`` label) this row scores.
        votes: One ``pass`` / ``fail`` / ``abstain`` vote per juror, in juror
            (column) order. A juror that cast no vote on the item votes
            ``abstain`` -- a non-vote, never a silent pass.
        status: The rolled-up row status -- :data:`ROW_BLOCKED` when any juror
            voted ``fail`` (one credible refutation blocks the row under the
            refute-first rubric), else :data:`ROW_READY`.
    """

    item_id: str
    votes: tuple[str, ...]
    status: str


def build_ballot_grid(
    ballots: tuple[PerItemJurorBallot, ...],
    rubric_item_ids: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[BallotGridRow, ...]]:
    """Build the juror x rubric-item ballot grid over *ballots*.

    Pivots the per-juror ballots into a per-item grid: one :class:`BallotGridRow`
    per id in *rubric_item_ids* (in render order), one column per juror (in the
    order the ballots were convened). Each cell is the juror's vote on that item
    -- :data:`PASS_VOTE` when the juror passed it, :data:`FAIL_VOTE` when the
    juror vetoed it, :data:`ABSTAIN_VOTE` when that juror cast no vote on the
    item (a non-vote, never a silent pass). A row with at least one
    :data:`FAIL_VOTE` rolls up to :data:`ROW_BLOCKED` (one credible refutation
    blocks the row under the refute-first rubric); a row with none reads
    :data:`ROW_READY`.

    Boundary: an empty *rubric_item_ids* yields no rows (a rubric with nothing
    to score); a juror that voted on no in-rubric item still contributes an
    ``abstain`` column so the grid never silently drops a convened juror.

    Args:
        ballots: One :class:`~eawf.observability.eval.cross_vendor_jury.PerItemJurorBallot`
            per convened juror, in convene order.
        rubric_item_ids: The rubric behaviour ids to render as rows, in order.

    Returns:
        A ``(juror_ids, rows)`` pair -- the column header juror ids in convene
        order, and one :class:`BallotGridRow` per rubric item.
    """
    jurors = tuple(ballot.juror for ballot in ballots)
    rows: list[BallotGridRow] = []
    for item_id in rubric_item_ids:
        votes = tuple(_juror_vote(ballot, item_id) for ballot in ballots)
        status = ROW_BLOCKED if FAIL_VOTE in votes else ROW_READY
        rows.append(BallotGridRow(item_id=item_id, votes=votes, status=status))
    logger.info(f"build_ballot_grid jurors={len(jurors)} items={len(rows)}")
    return jurors, tuple(rows)


def _juror_vote(ballot: PerItemJurorBallot, item_id: str) -> str:
    """Return one juror's vote on *item_id*: pass / fail / abstain.

    Reads the juror's :class:`~eawf.observability.eval.cross_vendor_jury.RubricItemVote`
    for *item_id* from its ballot. A passing vote maps to :data:`PASS_VOTE`; a
    failing vote (a veto) to :data:`FAIL_VOTE`; the absence of a vote on the
    item to :data:`ABSTAIN_VOTE`.

    Args:
        ballot: The juror's per-item ballot.
        item_id: The rubric item id to read the juror's vote for.

    Returns:
        The juror's vote word for the item.
    """
    for vote in ballot.votes:
        if vote.item_id == item_id:
            return PASS_VOTE if vote.passed else FAIL_VOTE
    return ABSTAIN_VOTE


def vote_sigil(vote: str, *, mode: RenderMode = DEFAULT_RENDER_MODE) -> str:
    """Return the pinned sigil glyph for a ballot *vote* word.

    Maps a vote word (:data:`PASS_VOTE` / :data:`FAIL_VOTE` /
    :data:`ABSTAIN_VOTE`) onto its designer-pinned lifecycle :class:`Sigil`
    glyph via :data:`_VOTE_SIGIL` -- ``pass`` to the CLOSED filled circle,
    ``fail`` to the FAILED cross, ``abstain`` to the PENDING hollow ring -- so
    the ballot grid renders the shared lifecycle vocabulary rather than a bare
    word. A vote word outside the closed set falls back to the muted PENDING
    ring rather than raising, so a cell never crashes the grid on an
    unrecognised vote.

    Args:
        vote: The ballot vote word to render as a sigil.
        mode: The App's resolved render-mode label threaded into the sigil
            helper; defaults to the ASCII column for a bare standalone render.

    Returns:
        The single-cell sigil glyph for *vote* in the resolved column.
    """
    return glyph(_VOTE_SIGIL.get(vote, Sigil.PENDING), mode=mode)


def render_ballot_grid(
    juror_ids: tuple[str, ...],
    rows: tuple[BallotGridRow, ...],
    *,
    mode: RenderMode = DEFAULT_RENDER_MODE,
) -> str:
    """Render the juror x rubric-item ballot grid as one text block.

    Lays out a header row of juror ids followed by one line per rubric item --
    the item id, each juror's vote as the pinned sigil glyph (:func:`vote_sigil`)
    in column order, and the rolled-up row verdict -- so the operator reads the
    whole grid top-to-bottom in the shared lifecycle vocabulary. The trailing
    column header is the designer-pinned ``verdict``. An empty *rows* yields the
    honest-empty :data:`NO_BALLOTS_NOTICE`.

    Args:
        juror_ids: The column-header juror ids, in convene order.
        rows: The ballot grid rows (:func:`build_ballot_grid`).
        mode: The App's resolved render-mode label threaded into the vote-sigil
            helper; defaults to the ASCII column for a bare standalone render.

    Returns:
        The newline-joined grid block (no trailing newline), or
        :data:`NO_BALLOTS_NOTICE` when there are no rows.
    """
    if not rows:
        return NO_BALLOTS_NOTICE
    header = "item  " + "  ".join(juror_ids) + "  verdict"
    lines = [header]
    for row in rows:
        cells = "  ".join(vote_sigil(vote, mode=mode) for vote in row.votes)
        lines.append(f"{row.item_id}  {cells}  {row.status}")
    return "\n".join(lines)


def render_tier_ladder(scored: OracleTier | None) -> str:
    """Render the T1_STATIC..T7_JURY oracle-tier ladder, marking *scored*.

    Walks every :class:`~eawf.kernel.spec.common.OracleTier` in ascending order
    and renders its human :func:`~eawf.kernel.spec.common.tier_label` (e.g.
    ``T1 static`` ... ``T7 jury``), prefixing the tier that scored the criterion
    with :data:`_TIER_MARK` so the operator reads which tier settled it. The
    label is sourced from :func:`tier_label`, never hardcoded, so a tier rename
    lands in one place. A ``None`` *scored* marks no tier (the honest-empty
    path: no oracle result bound).

    Args:
        scored: The :class:`~eawf.workflow.verify.oracle.OracleResult.tier` that
            scored the criterion, or ``None`` when no result is bound.

    Returns:
        The newline-joined ladder block, one ``<mark> T<n> <flavor>`` line per
        tier (no trailing newline).
    """
    lines: list[str] = []
    for tier in OracleTier:
        mark = _TIER_MARK if tier is scored else _TIER_NOMARK
        lines.append(f"{mark} {tier_label(tier)}")
    return "\n".join(lines)


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
        HONEST_EMPTY_CSS
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
    EvidenceModeScreen .evidence-ballot-title {
        height: 1;
        color: $accent;
        text-style: bold;
    }
    EvidenceModeScreen .evidence-ballot-grid {
        height: auto;
        max-height: 8;
        color: $text;
    }
    EvidenceModeScreen .evidence-ballot-ladder {
        height: auto;
        max-height: 8;
        color: $text;
    }
    """.replace("HONEST_EMPTY_CSS", HONEST_EMPTY_CSS)

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
        #: The per-juror jury ballots painted into the ballot-grid sub-pane.
        #: Empty is the honest-empty path -- like the readiness view, the
        #: render seam never convenes a live jury, so ballots are supplied
        #: externally (a close envelope, a fixture under test) via
        #: :meth:`set_ballots`.
        self._ballots: tuple[PerItemJurorBallot, ...] = ()
        #: The rubric behaviour ids the ballot grid renders as rows, in order.
        self._rubric_item_ids: tuple[str, ...] = ()
        #: The oracle tier that scored the criterion, marked in the ladder.
        #: ``None`` marks no tier (no oracle result bound).
        self._scored_tier: OracleTier | None = None

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
            yield Static("Jury ballots", classes="evidence-ballot-title")
            yield Static(
                NO_BALLOTS_NOTICE, id="evidence-ballot-grid", classes="evidence-ballot-grid"
            )
            yield Static(
                render_tier_ladder(None),
                id="evidence-ballot-ladder",
                classes="evidence-ballot-ladder",
            )

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
        self._paint_ballots()
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

    def set_ballots(
        self,
        ballots: Iterable[PerItemJurorBallot] = (),
        rubric_item_ids: Iterable[str] = (),
        scored_tier: OracleTier | None = None,
    ) -> None:
        """Bind jury ballots + the scoring tier and repaint the ballot sub-pane.

        Like :meth:`set_readiness`, the render seam never convenes a live jury
        (three cross-vendor spawns) -- the reduced ballots + the
        :class:`~eawf.workflow.verify.oracle.OracleResult.tier` that scored the
        criterion are supplied externally and pushed in here. Repaints the
        juror x rubric-item grid + the oracle-tier ladder immediately when the
        pane is mounted.

        Args:
            ballots: One
                :class:`~eawf.observability.eval.cross_vendor_jury.PerItemJurorBallot`
                per convened juror, in convene order. Defaults to none -- the
                grid then renders the honest-empty :data:`NO_BALLOTS_NOTICE`.
            rubric_item_ids: The rubric behaviour ids the grid renders as rows,
                in order. Defaults to none.
            scored_tier: The oracle tier that scored the criterion, marked in
                the ladder, or ``None`` to mark no tier.
        """
        self._ballots = tuple(ballots)
        self._rubric_item_ids = tuple(rubric_item_ids)
        self._scored_tier = scored_tier
        if self.is_mounted:
            self._paint_ballots()

    def _paint_ballots(self) -> None:
        """Repaint the jury-ballot grid + oracle-tier ladder from the bound data.

        Renders the juror x rubric-item grid (:func:`build_ballot_grid` ->
        :func:`render_ballot_grid`) into the grid Static and the
        T1_STATIC..T7_JURY ladder (:func:`render_tier_ladder`, marking the
        scoring tier) into the ladder Static. Falls back to
        :data:`NO_BALLOTS_NOTICE` when no ballots are bound. Guarded on mount:
        the sub-pane widgets only exist after :meth:`compose_body`.
        """
        grids = self.query("#evidence-ballot-grid")
        if not grids:
            return
        grid = grids.first(Static)
        ladder = self.query_one("#evidence-ballot-ladder", Static)
        juror_ids, rows = build_ballot_grid(self._ballots, self._rubric_item_ids)
        grid.update(render_ballot_grid(juror_ids, rows, mode=self._render_mode()))
        ladder.update(render_tier_ladder(self._scored_tier))

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
    "ABSTAIN_VOTE",
    "EMPTY_NOTICE",
    "FAIL_VOTE",
    "LEDGER_EMPTY_NOTICE",
    "NO_BALLOTS_NOTICE",
    "NO_CRITERIA_NOTICE",
    "ORPHAN_SECTION",
    "PASS_VOTE",
    "ROW_BLOCKED",
    "ROW_READY",
    "BallotGridRow",
    "EvidenceJoin",
    "EvidenceModeScreen",
    "EvidenceRow",
    "EvidenceScrubError",
    "LedgerRow",
    "build_ballot_grid",
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
    "render_ballot_grid",
    "render_followups_block",
    "render_tier_ladder",
    "sort_evidence_rows",
    "verdict_sigil",
    "vote_sigil",
]
