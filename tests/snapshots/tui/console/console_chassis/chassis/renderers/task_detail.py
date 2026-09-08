"""task.detail: the fixture's own task carries criteria, candidates and one run; any other
task id renders its stored record through the record path or states the absence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.frame import TBL, TSPEC, bar, bar_, build, header_row, thin
from ...chassis.keys import ROUTE_KEYS

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_OWN = "EAWF-0001"

_CRITERIA: tuple[tuple[str, str], ...] = (
    ("events carry a semantic kind", "receipt EVT-1188"),
    ("no wave vocabulary remains", "receipt EVT-1190"),
    ("replay digest equal to clean load", "?  nothing answered"),
)


def _subject(session: Session, fixture: Fixture, tid: str) -> str:
    if dv.own_body(session, _OWN):
        return "Normalize tool events · READY_TO_INTEGRATE"
    name = dv.task_name_of(fixture, tid)
    if name:
        f = dv.fleet_of(fixture, tid)
        return f"{name} · {f.state if f else ''}"
    return (
        dv.subj_facts(fixture, tid, [("NAME", "field"), ("STATE|STANDING", "first")])
        or "∅ name and state unavailable"
    )


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    tid = dv.subj_of(s, _OWN)
    own = dv.own_body(s, _OWN)
    if own:
        # one link, one nav entry: a criterion is prose, not a destination
        dv.publish_nav(s, ["RUN-9e3779b1"])
        dv.sel_in(s, 1)
    tpar = "BAT-0001 ▸ " if tid == _OWN else ""
    rows: list[str] = [
        header_row(s, fixture, f" Eä ▸ … ▸ {tpar}{tid}", w),
        f" Task {tid} {_subject(s, fixture, tid)}",
        bar(w),
        " RESULT    one candidate accepted · attempt 1 of 1",
        thin(w),
        TSPEC["CR"].head(["CRITERIA", "PROOF"]),
    ]
    for c in _CRITERIA:
        rows.append(TSPEC["CR"].row(list(c), False))
    rows.extend(
        [
            thin(w),
            " DEPENDENCIES    none blocking",
            " CANDIDATES      1 accepted · 0 rejected · 0 waiting",
            thin(w),
            TSPEC["RN"].head(["RUNS", "STATE", "PROVIDER", "ELAPSED"]),
            TBL([34, 10, 12, 0], 2).row(
                ["RUN-9e3779b1", "SUCCEEDED", "claude", "18m 04s"], s.sel == 0
            ),
            thin(w),
            " LINEAGE   attempt 1 of 1 · no retry · no fork",
        ]
    )
    if not own:
        rows = dv.absent(s, fixture, rows, tid, "criteria, candidates or runs", w)
    return build(s, rows, bar_(s, ROUTE_KEYS["task.detail"], w), w, h)
