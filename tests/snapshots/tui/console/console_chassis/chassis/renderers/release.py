"""release: the candidate's membership table over its readiness matrix; Tab moves the
cursor between the two regions and Enter on a readiness row opens the matrix overlay."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.frame import TBL, TSPEC, bar, bar_, build, header_row, thin
from ...chassis.keys import ROUTE_KEYS

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_MEMBERS: tuple[tuple[str, str, str], ...] = (
    ("MLS-0001 Replay-safe activity", "Runtime", "Jul 14"),
    ("MLS-0007 Calibration set", "Research", "Jul 15"),
    ("MLS-0004 Escape-ledger", "Trust", "not yet"),
)
_READINESS: tuple[tuple[str, str, str], ...] = (
    ("acceptance complete", "no", "1 still in review"),
    ("policy gate", "passed", "receipt EVT-3301"),
    ("artifact build", "passed", "receipt EVT-3302"),
    ("approvals", "1 of 2", "owner sign-off open"),
)


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    dv.sel_in(s, len(_MEMBERS))
    on_rd = (s.rel_reg or "MEMBERSHIP") == "READINESS"
    if on_rd:
        s.rel_sel = max(0, min(len(_READINESS) - 1, s.rel_sel or 0))
    else:
        dv.sel_in(s, len(_MEMBERS))
    rows: list[str] = [
        header_row(s, fixture, f" Eä ▸ {fixture.scope} ▸ REL-0001", w),
        " Release REL-0001 v0.7.0-rc1 · CANDIDATE",
        bar(w),
        TSPEC["MB"].head(["MEMBERSHIP", "TRACK", "ACCEPTED"]),
    ]
    for i, m in enumerate(_MEMBERS):
        rows.append(TSPEC["MB"].row(list(m), not on_rd and i == s.sel))
    rdt = TBL([34, 11, 0], 2)
    rows.append(thin(w))
    rows.append(rdt.head(["READINESS", "STATE", "EVIDENCE"]))
    for i, x in enumerate(_READINESS):
        rows.append(rdt.row(list(x), on_rd and i == (s.rel_sel or 0)))
    rows.append(thin(w))
    rows.append(" APPROVAL  Granted at head 9d2b41c · still exact.")
    rows.append(" PUBLICATION  Not started — nothing has been published.")
    return build(s, rows, bar_(s, ROUTE_KEYS["release"], w), w, h)
