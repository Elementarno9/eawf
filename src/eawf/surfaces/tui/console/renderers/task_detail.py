"""task.detail: the fixture's own task with its criteria, candidates and one Run.

Any other task id renders its stored record through the record path or states the absence.

The native frame is the packet's labelled facts about one Task: the Runs of it first, the
caret walking them, then the Batch it is filed in, its active Run, the commit it was
integrated at, each criterion with the newest proof receipt filed for it, and the
receipts in all. A frame opened on no Task lists the register to pick one from.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from eawf.kernel.projection.compute import CRITERION_FACT
from eawf.kernel.projection.spine import SpineRow, SpineView
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.frame import (
    TABLES,
    Table,
    View,
    bar,
    build,
    header,
    route_keys_bar,
    thin,
)
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.renderers.children import (
    child_cursor,
    child_rows,
    children,
    lrow,
    status,
)
from eawf.surfaces.tui.console.renderers.detail import at, subject_of
from eawf.surfaces.tui.console.renderers.read_model import UNKNOWN_WORD, cell
from eawf.surfaces.tui.console.renderers.spine import (
    detail_head,
    detail_keys,
    finished,
    held,
    native_frame,
)

OWN = "EAWF-0001"
OWN_RUN = pt.OWN_RUN

#: What a Task that has not completed states for its integrated commit.
NOT_INTEGRATED = "∅ not integrated · only a completed Task names the commit it landed in"
_CRITERIA: tuple[tuple[str, str], ...] = (
    ("events carry a semantic kind", "receipt EVT-1188"),
    ("no wave vocabulary remains", "receipt EVT-1190"),
    ("replay digest equal to clean load", "?  nothing answered"),
)


def _subject(view: View, tid: str) -> str:
    s, fx = view.session, view.fixture
    if dv.own_body(s, OWN):
        return "Normalize tool events · READY_TO_INTEGRATE"
    name = dv.task_name_of(fx, tid)
    if name:
        row = dv.fleet_of(fx, tid)
        return f"{name} · {row.state if row else ''}"
    facts = [("NAME", "field"), ("STATE|STANDING", "first")]
    return dv.subj_facts(fx, tid, facts) or "∅ name and state unavailable"


def _run_line(run: SpineRow) -> str:
    """Return one Run of the Task as its row reads: key, status and when it started."""
    started = run.facts.get("started_at")
    return f"{run.key} · {status(run)}" + (f" · started {at(started)}" if started else "")


def _newest_proofs(
    facts: Mapping[str, str], proofs: Sequence[SpineRow]
) -> list[tuple[str, SpineRow | None]]:
    """Return each criterion the Task states, in order, beside its newest receipt, if any."""
    newest: list[tuple[str, SpineRow | None]] = []
    place = 1
    while (stated := facts.get(f"{CRITERION_FACT}{place}")) is not None:
        ident = stated.split(" · ", 1)[0]
        named = [p for p in proofs if ident in p.facts.get("criteria", "").split(",")]
        newest.append((ident, max(named, key=lambda p: p.facts.get("ended_at", ""), default=None)))
        place += 1
    return newest


def _criterion_lines(facts: Mapping[str, str], proofs: Sequence[SpineRow]) -> list[str]:
    """Return one line per criterion the Task states, each with its newest proof.

    A criterion reads ``id · kind · gates`` and then what the newest receipt naming it
    reached, or that none is filed; its text comes last, so a narrow frame clips it first.
    """
    lines: list[str] = []
    for place, (_ident, newest) in enumerate(_newest_proofs(facts, proofs), start=1):
        ident, kind, gates, text = facts[f"{CRITERION_FACT}{place}"].split(" · ", 3)
        proof = (
            f"{newest.facts.get('result', UNKNOWN_WORD)} {newest.facts.get('receipt', newest.key)}"
            if newest is not None
            else "∅ no receipt"
        )
        lines.append(" · ".join(part for part in (ident, kind, gates, proof, text) if part))
    return lines


def _proof_text(facts: Mapping[str, str], proofs: Sequence[SpineRow]) -> str:
    """Return how the Task's criteria stand on their newest receipts, and how many are filed.

    A criterion re-run until it passes leaves its earlier failures on file, so only the
    newest receipt of each criterion says whether it is proven; the count of every
    receipt follows, so the earlier ones are not hidden.
    """
    if not proofs:
        return "∅ no proof receipt is filed for this Task"
    newest = [proof for _ident, proof in _newest_proofs(facts, proofs)]
    filed = dv.plural(len(proofs), "receipt")
    if not newest:
        return f"{filed} filed · the Task states no criterion they prove"
    passed = sum(1 for p in newest if p is not None and p.facts.get("result") == "pass")
    return f"{passed} of {len(newest)} criteria pass on their newest receipt · {filed} filed"


def _due_text(view: View, key: str) -> str:
    """Return when a Task is due: its Milestone's target date, else with that Milestone.

    A Task is due with the Milestone it is scoped to, so the Milestone's own date is the
    answer, and its title names it where it states no date.
    """
    milestone = next(
        (
            row
            for row in view.rows
            if row.key == key and row.collection is Epoch2Collection.MILESTONE
        ),
        None,
    )
    if milestone is None:
        return f"with {key}"
    named = f"{key} {milestone.title}" if milestone.title else key
    target = milestone.facts.get("target_date")
    return f"{target} · with {named}" if target else f"with {named}"


def task_frame(view: View, spine: SpineView) -> list[str]:
    """Return one Task's frame: its Runs, then the facts the read model states about it.

    Args:
        view: The render being built; its session names the Task.
        spine: The Task register with the Runs of each Task.

    Returns:
        The full frame, keybar last; the register frame when the session names no Task.
    """
    session = view.session
    subject = subject_of(view, spine)
    if subject is None:
        return native_frame(view, spine)
    runs = children(spine.rows, subject.key, Epoch2Collection.RUN)
    facts = subject.facts
    proofs = [
        row
        for row in spine.rows
        if row.collection is Epoch2Collection.RECEIPT and row.facts.get("task") == subject.key
    ]
    stated = facts.get("criteria")
    criteria = f"{stated} {'criterion' if stated == '1' else 'criteria'} stated" if stated else None
    below = [
        lrow("BATCH", subject.parent_key or "∅ filed in no Batch"),
        # a finished Task keeps naming the Run it last ran, which is no longer active
        lrow(
            "LAST RUN" if finished(subject) else "ACTIVE RUN",
            facts.get("run", "∅ no Run of this Task is active"),
        ),
        lrow("INTEGRATED", facts["integrated"][:7] if "integrated" in facts else NOT_INTEGRATED),
        lrow("CRITERIA", criteria or cell(subject.field("criteria"))),
        *(lrow("", line) for line in _criterion_lines(facts, proofs)),
        lrow("PROOF", _proof_text(facts, proofs)),
        lrow("CANDIDATES", cell(subject.field("candidates"))),
        *([lrow("PRIORITY", facts["priority"])] if "priority" in facts else []),
        *([lrow("DUE", _due_text(view, facts["due"]))] if "due" in facts else []),
    ]
    top = detail_head(view, spine, subject)
    cursor = child_cursor(session, [run.key for run in runs], subject=subject.key)
    listed = child_rows(
        view,
        "RUNS",
        [_run_line(run) for run in runs],
        cursor,
        chrome=len(top) + len(below),
        empty="∅ no Run of this Task is recorded",
    )
    keys = detail_keys(view, spine)
    return build(view, [*top, *listed, *below], route_keys_bar(view, keys))


def render(view: View) -> list[str]:
    """Return the Task frame, native when a read model is held."""
    spine = held(view)
    if spine is not None:
        return task_frame(view, spine)
    s, fx, w = view.session, view.fixture, view.w
    tid = dv.subj_of(s, OWN)
    own = dv.own_body(s, OWN)
    if own:
        # one link, one nav entry: a criterion is prose, not a destination
        dv.publish_nav(s, [OWN_RUN])
        dv.sel_in(s, 1)
    parent = "BAT-0001 ▸ " if tid == OWN else ""
    rows = [
        header(view, f" Eä ▸ … ▸ {parent}{tid}"),
        f" Task {tid} {_subject(view, tid)}",
        bar(w),
        " RESULT    one candidate accepted · attempt 1 of 1",
        thin(w),
        TABLES["CR"].head(["CRITERIA", "PROOF"]),
    ]
    rows.extend(TABLES["CR"].row(list(c), False) for c in _CRITERIA)
    rows.extend(
        [
            thin(w),
            " DEPENDENCIES    none blocking",
            " CANDIDATES      1 accepted · 0 rejected · 0 waiting",
            thin(w),
            TABLES["RN"].head(["RUNS", "STATE", "PROVIDER", "ELAPSED"]),
            Table([34, 10, 12, 0], 2).row([OWN_RUN, "SUCCEEDED", "claude", "18m 04s"], s.sel == 0),
            thin(w),
            " LINEAGE   attempt 1 of 1 · no retry · no fork",
        ]
    )
    if not own:
        what = "criteria, candidates or runs"
        rows = dv.absent(s, fx, rows, entity_id=tid, what=what, w=w)
    return build(view, rows, route_keys_bar(view, ROUTE_KEYS["task.detail"]))
