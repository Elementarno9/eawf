"""task.detail: the fixture's own task with its criteria, candidates and one Run.

Any other task id renders its stored record through the record path or states the absence.
"""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
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
from eawf.surfaces.tui.console.renderers.spine import held, native_frame

OWN = "EAWF-0001"
OWN_RUN = "RUN-9e3779b1"
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


def render(view: View) -> list[str]:
    """Return the Task frame, native when a read model is held."""
    spine = held(view)
    if spine is not None:
        return native_frame(view, spine)
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
