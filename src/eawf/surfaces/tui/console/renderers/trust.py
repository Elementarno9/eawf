"""trust: the milestone's truth fields, who answered for each, and the agents' track record.

Enter opens the evidence behind the focused field, which is the route's own key rather
than a dispatcher one: the footer advertises it, so something has to claim it.

The native frame is scoped to one Milestone and draws the three truth-field groups the
packet names -- the verdicts, the calibration and the track record -- each saying who
answered for it. A subject no verdict has been recorded for reads ``? unknown · no outcome
recorded``, never a negative, and a group no store feeds renders every numeric cell
unavailable rather than a number, because a rate over zero judged attempts has no value.
"""

from __future__ import annotations

from eawf.kernel.projection.route_view import RouteReadModel
from eawf.kernel.projection.truth import TruthState
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.frame import Grid, View, chip, g_frame, thin, window_rows
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
    restore,
    route_crumb,
)

_KEYS: tuple[tuple[str, str], ...] = (
    ("↑↓", "field"),
    ("Enter", "evidence"),
    (".", "actions"),
    ("i", "inspect"),
    ("Esc", "back"),
)


#: The three truth-field groups a TrustView carries, in the order the frame draws them.
GROUPS: tuple[str, ...] = ("verdicts", "calibration", "track record")

#: What a verdict cell reads for a subject no observation has been recorded for.
NO_OUTCOME = "no outcome recorded"

#: What the calibration group states while no calibration report is held: every numeric
#: cell is unavailable, the report's absence named rather than read as a zero.
NO_CALIBRATION = f"{UNAVAILABLE} · no calibration report is held"

#: What the track record states while no reputation store feeds the frame.
NO_TRACK_RECORD = f"{UNAVAILABLE} · no reputation store is held"

_VERDICTS = Grid([20, 12, 26, 0])
_RECORD = Grid([16, 11, 11, 0], 3)


def _verdict_cells(model: RouteReadModel, index: int) -> list[str]:
    """Return one verdict row: the subject, its verdict, who answered and its freshness."""
    row = model.rows[index]
    verdict = row.field("verdict")
    answered = value_cell(row.field("answered_by")).slot
    if verdict.state is not TruthState.KNOWN:
        # an unobserved subject is unknown, never failed: the freshness says why
        return [row.key, UNKNOWN_WORD, answered, NO_OUTCOME]
    return [row.key, value_cell(verdict).slot, answered, value_cell(row.field("freshness")).full]


def _field_readout(model: RouteReadModel, cursor: int) -> list[str]:
    """Return the ``FIELD`` readout: the focused field with its producer, basis and freshness."""
    if not model.rows:
        return [label("FIELD", "∅ no field is focused · this Milestone holds no verdict row")]
    row = model.rows[cursor]
    verdict = row.field("verdict")
    basis = verdict.missing_reason or "stated by its producer"
    return [
        label("FIELD", f"{row.key} verdict · answered by {verdict.producer}"),
        more(f"basis {basis} · freshness {verdict.freshness.value}"),
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
    cursor = restore(s, model)
    subject = s.subj_id
    steps = [subject, "Trust"] if subject else ["Trust"]
    scope = f"Milestone {subject}" if subject else "No Milestone named"
    top = native_head(
        view,
        model,
        crumb_text=route_crumb(model, *steps),
        summary=f"{scope} · {len(GROUPS)} truth-field groups · {counts(model)}",
    )
    body: list[str] = [_VERDICTS.head(["JURY", "VERDICT", "ANSWERED BY", "FRESHNESS"])]
    after = 13
    win = window_rows(view, total=len(model.rows), cursor=cursor, chrome=len(top) + 1 + after)
    if not model.rows:
        body.append(f"   {UNKNOWN_WORD} · {NO_OUTCOME} for any subject of this Milestone")
    body.extend(
        _VERDICTS.row(_verdict_cells(model, i), i == cursor, w) for i in range(win.start, win.stop)
    )
    body.extend(
        [
            thin(w),
            label("CALIBRATION", NO_CALIBRATION),
            more("every numeric cell reads unavailable, never a number"),
            thin(w),
            label("TRACK RECORD"),
            _RECORD.head(["AGENT", "ACCEPTED", "REJECTED", "RATE"]),
            f"    {NO_TRACK_RECORD}",
            "    a rate over zero judged attempts has no value, so none is drawn",
            thin(w),
        ]
    )
    return finish(view, top, body, _KEYS, foot=_field_readout(model, cursor))


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
