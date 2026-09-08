"""run.detail: the Run's timeline page with USAGE, CONTROLS and LINEAGE; a Run other than
the fixture's own renders its record from the register or states the absence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.frame import TBL, bar, bar_, build, header_row, thin
from ...chassis.keys import ROUTE_KEYS
from ...chassis.width import cell_len

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_OWN = "RUN-9e3779b1"
TL_PREFIX = 38


def tl_label(w: int) -> str:
    return "PRIORITY" if w >= 120 else "PRI"


def tl_text(w: int) -> int:
    return w - TL_PREFIX - 1 - cell_len(tl_label(w))


def tl_header(w: int) -> str:
    line = TBL([10, 23, tl_text(w) + 1, 0], 2).head(["TIMELINE", "EVENT", "DETAIL", tl_label(w)])
    if cell_len(line) > w:
        raise ValueError(f"timeline header overruns the frame: {cell_len(line)}/{w}")
    return line


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    P = fixture.proto
    dv.sel_in(s, len(P.timeline))
    rid = dv.subj_of(s, _OWN)
    rpar = "EAWF-0001 ▸ " if rid == _OWN else ""
    rf = dv.fleet_of(fixture, rid)
    if dv.own_body(s, _OWN):
        facts = "claude · RUNNING · started 10:00:00"
    elif rf:
        facts = f"{rf.prov} · {rf.state}" + (f" · {rf.reason}" if rf.reason else "")
    else:
        facts = (
            dv.subj_facts(fixture, rid, [("PROVIDER", "token"), ("STATE", "field")])
            or "∅ provider and state unavailable"
        )
    rows: list[str] = [
        header_row(s, fixture, f" Eä ▸ … ▸ {rpar}{rid}", w),
        f" Run {rid} · {facts} · seq {dv.num(P.revision)}",
        bar(w),
        tl_header(w),
    ]
    text_w = tl_text(w)
    T = TBL([10, 23, text_w + 1, 0], 2)
    for i, e in enumerate(P.timeline):
        if cell_len(e[2]) > text_w:
            raise ValueError(f"event text exceeds its column: {e[2]}")
        rows.append(T.row([e[0], e[1], e[2], e[3]], i == s.sel))
    rows.append(thin(w))
    rows.append(" USAGE     elapsed 18m 04s of 60m · ≈41m left · cost ~4.62 of 20.00")
    rows.append("           tokens out 128,410 · peak rss ∅ probe uncertified")
    rows.append(thin(w))
    rows.append(" CONTROLS  no control outstanding · last confirmed 10:01:12")
    rows.append(" LINEAGE   attempt 1 of 1 · no retry · no fork")
    if not dv.own_body(s, _OWN):
        rows = dv.absent(s, fixture, rows, rid, "timeline or usage", w)
    return build(s, rows, bar_(s, ROUTE_KEYS["run.detail"], w), w, h)
