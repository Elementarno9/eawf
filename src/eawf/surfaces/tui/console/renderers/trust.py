"""trust: the milestone's truth fields, who answered for each, and the agents' track record.

Enter opens the evidence behind the focused field, which is the route's own key rather
than a dispatcher one: the footer advertises it, so something has to claim it.

The native frame is scoped to one Milestone and draws the three truth-field groups the
packet names from a :class:`~eawf.observability.eval.trust_projection.TrustView`: the
verdict observations of the Milestone's Batches, each answered for by the producer
``(agent_role, runtime)`` that reached it, the jury-validation report with the authority
the calibration gate returned, and each producer's tally beside its own Brier score as a
juror. A subject no verdict has been recorded for reads ``? unknown · no outcome
recorded``, never a negative; an ``INSUFFICIENT`` report renders every numeric cell
unavailable, a rate over zero judged attempts renders unavailable rather than a number,
and a juror whose scored verdicts are under the cohort floor states how far short it is
rather than a Brier score.
"""

from __future__ import annotations

from eawf.kernel.projection.route_view import RouteReadModel, RouteRecord
from eawf.kernel.projection.truth import TruthState
from eawf.observability.eval.trust_projection import (
    CalibrationGroup,
    TrackRecordRow,
    TrustView,
    build_trust_view,
)
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.frame import Grid, View, chip, g_frame, thin, window_rows
from eawf.surfaces.tui.console.keybar import route_pairs
from eawf.surfaces.tui.console.navigation import Ctx, busy, go
from eawf.surfaces.tui.console.renderers.read_model import (
    UNAVAILABLE,
    UNKNOWN_WORD,
    counts,
    finish,
    label,
    more,
    native,
    native_head,
    route_crumb,
)

_KEYS = route_pairs("trust")


#: The three truth-field groups a TrustView carries, in the order the frame draws them.
GROUPS: tuple[str, ...] = ("verdicts", "calibration", "track record")

#: What a verdict cell reads for a subject no observation has been recorded for.
NO_OUTCOME = "no outcome recorded"

#: What a rate reads over zero judged attempts, which has no value.
ZERO_JUDGED = "∅ 0 judged"

_VERDICTS = Grid([20, 16, 26, 0])
_RECORD = Grid([27, 9, 9, 12, 0], 0)


def _jury(row: RouteRecord) -> str:
    """Return the JURY cell: the Batch and the criterion the verdict was reached on."""
    return f"{row.field('batch').value} {row.field('subject').value}"


def _verdict_cells(row: RouteRecord) -> list[str]:
    """Return one verdict row: the subject, its verdict, who answered and its freshness."""
    verdict = row.field("verdict")
    if verdict.state is not TruthState.KNOWN or verdict.value is None:
        # an unobserved subject is unknown, never failed: the freshness says why
        return [row.key, UNKNOWN_WORD, "? unknown", NO_OUTCOME]
    kind = {"verified_true": "ok", "verified_false": "er"}.get(verdict.value, "info")
    return [
        _jury(row),
        chip(kind, verdict.value),
        value_cell(row.field("answered_by")).slot,
        value_cell(row.field("occurred_at")).full,
    ]


def _number(value: float | None) -> str:
    """Return a report metric, or unavailable when the report states none."""
    return UNAVAILABLE if value is None else f"~{value:.2f}"


def _calibration_rows(group: CalibrationGroup) -> list[str]:
    """Return the calibration group: the status and metrics, then the cohort and authority."""
    report = group.report
    co_error = (
        "no known-bad subject"
        if report.known_bad_n == 0
        else _number(report.unanimous_pass_on_known_bad_rate)
    )
    return [
        label(
            "CALIBRATION",
            f"{report.status.value.upper()} · Brier {_number(report.brier)} · co-error {co_error}",
        ),
        more(f"n {report.n} of {group.min_scored} scored · authority {group.authority}"),
    ]


def _record_cells(row: TrackRecordRow, floor: int) -> list[str]:
    """Return one producer's tally and its Brier score, each unavailable without a basis."""
    rate = row.rate
    brier = (
        f"∅ {row.scored} of {floor} scored"
        if row.brier is None
        else f"~{row.brier:.2f} · {row.scored} scored"
    )
    return [
        f"{row.agent_role} · {row.runtime}",
        str(row.accepted),
        str(row.rejected),
        ZERO_JUDGED if rate is None else f"~{rate:.2f}",
        brier,
    ]


def _field_readout(trust: TrustView, rows: tuple[RouteRecord, ...], cursor: int) -> list[str]:
    """Return the ``FIELD`` readout: the focused field with its producer, basis and freshness."""
    if not rows:
        return [label("FIELD", "∅ no field is focused · this Milestone holds no verdict row")]
    row = rows[cursor]
    verdict = row.field("verdict")
    if verdict.state is not TruthState.KNOWN:
        basis = verdict.missing_reason or NO_OUTCOME
        return [
            label("FIELD", f"{row.key} verdict · answered by {verdict.producer}"),
            more(f"basis {basis} · freshness {verdict.freshness.value}"),
        ]
    return [
        label("FIELD", f"{_jury(row)} verdict · answered by {row.field('answered_by').value}"),
        more(
            f"basis {row.field('site').value} audit at the Batch head · freshness"
            f" {verdict.freshness.value} · authority {trust.calibration.authority}"
        ),
    ]


def native_frame(view: View, model: RouteReadModel) -> list[str]:
    """Return the Trust frame drawn from the read model the daemon served.

    Args:
        view: The render being built; its session names the Milestone the frame is scoped to.
        model: The route's read model at the committed cursor.

    Returns:
        The full frame, keybar last.
    """
    s, w = view.session, view.w
    subject = s.subj_id
    trust = build_trust_view(model, milestone=subject)
    rows = (*trust.verdicts, *trust.unobserved)
    cursor = dv.restore_by_id(s, [row.key for row in rows])
    steps = [subject, "Trust"] if subject else ["Trust"]
    scope = f"Milestone {subject}" if subject else "No Milestone named"
    top = native_head(
        view,
        model,
        crumb_text=route_crumb(view, model, *steps),
        summary=f"{scope} · {len(GROUPS)} truth-field groups · {counts(model)}",
    )
    body: list[str] = [_VERDICTS.head(["JURY", "VERDICT", "ANSWERED BY", "FRESHNESS"])]
    record = trust.track_record
    after = 11 + max(1, len(record))
    win = window_rows(view, total=len(rows), cursor=cursor, chrome=len(top) + 1 + after)
    if not rows:
        body.append(f"   {UNKNOWN_WORD} · {NO_OUTCOME} for any subject of this Milestone")
    body.extend(
        _VERDICTS.row(_verdict_cells(rows[i]), i == cursor, w) for i in range(win.start, win.stop)
    )
    body.extend([thin(w), *_calibration_rows(trust.calibration), thin(w)])
    body.append(_RECORD.head(["TRACK RECORD", "ACCEPTED", "REJECTED", "RATE", "BRIER"]))
    floor = trust.calibration.min_scored
    body.extend(_RECORD.row(_record_cells(row, floor), False, w) for row in record)
    if not record:
        body.append(f"    {UNAVAILABLE} · no producer has answered for a verdict here")
    body.append(thin(w))
    return finish(view, top, body, _KEYS, foot=_field_readout(trust, rows, cursor))


def render(view: View) -> list[str]:
    """Return the Trust frame, native when a read model is held."""
    model = native(view)
    if model is not None:
        return native_frame(view, model)
    s, w = view.session, view.w
    fields = [
        ["jury-01", chip("ok", "pass"), "conformance runner", "14:01"],
        ["jury-02", chip("ok", "pass"), "conformance runner", "13:58"],
        ["jury-03", chip("info", "? unknown"), "conformance runner", "no outcome recorded"],
    ]
    dv.sel_in(s, len(fields))
    juries = Grid([10, 12, 21, 0])
    record = Grid([16, 11, 11, 0], 3)
    # the column says who answered for the field, never the internal "producer" word
    body = [juries.head(["JURY", "VERDICT", "ANSWERED BY", "FRESHNESS"])]
    body.extend(juries.row(r, i == s.sel, w) for i, r in enumerate(fields))
    body.extend(
        [
            thin(w),
            " CALIBRATION  ~0.31 Brier over 41 resolved · metrics · 13:40",
            thin(w),
            " TRACK RECORD",
            record.head(["AGENT", "ACCEPTED", "REJECTED", "RATE"]),
            record.row(["claude", "18", "2", "~0.90"], False, w),
            record.row(["codex", "6", "0", "~1.00"], False, w),
            record.row(["local-runner", "0", "0", "∅ unavailable"], False, w),
            thin(w),
            " FIELD        A rate over zero judged attempts is ∅ unavailable, never 0.00.",
        ]
    )
    return g_frame(
        view,
        crumb=f"Eä ▸ {view.fixture.scope} ▸ MLS-0007 ▸ Trust",
        ctx="Milestone MLS-0007 · 3 truth fields · as of 14:02",
        body=body,
        keys=_KEYS,
    )


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Open the evidence behind the focused truth field on Enter."""
    s = ctx.s
    if s.route != "trust" or key != "Enter" or busy(s):
        return False
    go(ctx, "evidence", "the evidence behind this truth field")
    return True
