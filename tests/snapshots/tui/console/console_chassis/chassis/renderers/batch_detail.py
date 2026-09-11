"""batch.detail: the batch's own children found by their back-reference, so this frame
cannot show a different set from the milestone that summarises it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.frame import TBL, bar, bar_, build, header_row, thin
from ...chassis.keys import ROUTE_KEYS

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_OWN = "BAT-0001"


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    bid = dv.subj_of(s, _OWN)
    tasks = [
        (
            f"{x} {dv.field_of(fixture, x, 'NAME') or ''}",
            str(dv.ref_state(fixture, x) or "∅"),
            str(
                len(dv.children_of(fixture, x, "RUN-", "SCOPE"))
                or ("1" if dv.field_of(fixture, x, "RUNS") else "0")
            ),
        )
        for x in dv.children_of(fixture, bid, "EAWF-", "BATCH")
    ]
    if dv.own_body(s, _OWN):
        dv.sel_in(s, len(tasks))
    bmil = str(dv.rec_field(fixture, bid, "MILESTONE") or "").split(" ")[0]
    bpar = f"{bmil} ▸ " if bmil else ""
    subject = (
        dv.subj_facts(fixture, bid, [("NAME", "field"), ("STATE", "field"), ("CHECKS", "worst")])
        or "∅ name and state unavailable"
    )
    T = TBL([34, 19, 0], 2)
    rows: list[str] = [
        header_row(s, fixture, f" Eä ▸ … ▸ {bpar}{bid}", w),
        f" Batch {bid} {subject}",
        bar(w),
        " HEAD            Integration base a1f4c9e matches the shared branch.",
        "                 authorized at      a1f4c9e   still exact",
        thin(w),
        T.head(["TASK FRONTIER", "STATE", "RUNS"]),
    ]
    dv.publish_nav(s, [t[0].split(" ")[0] for t in tasks])
    for i, t in enumerate(tasks):
        rows.append(T.row([t[0], t[1], t[2]], i == s.sel))
    rows.append(thin(w))
    rows.append(" VERIFICATION CYCLE   checking → audit review → changes → repair")
    rows.append("   checking          3 of 4 checks passed          ≈6m left")
    if not dv.own_body(s, _OWN):
        rows = dv.absent(s, fixture, rows, bid, "tasks or integration detail", w)
    return build(s, rows, bar_(s, ROUTE_KEYS["batch.detail"], w), w, h)
