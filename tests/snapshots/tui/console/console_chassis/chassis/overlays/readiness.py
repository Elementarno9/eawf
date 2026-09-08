"""The readiness matrix: every signal with its evidence, then the approval and the
publish refusal; the prototype's wrap splices the state-model rows above the keybar of
the BUILT frame, so the splice runs after build here too."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.frame import TBL, TSPEC, bar, build, header_row, keybar, thin
from ...chassis.overlays import with_state_rows

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_SIGNALS: tuple[tuple[str, str, str], ...] = (
    ("acceptance complete", "no", "1 still in review"),
    ("policy gate", "passed", "receipt EVT-3301"),
    ("artifact build", "passed", "receipt EVT-3302"),
    ("approvals", "1 of 2", "owner sign-off open"),
)


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    dv.sel_in(s, len(_SIGNALS))
    rows: list[str] = [
        header_row(s, fixture, " Eä ▸ readiness · REL-0001", w),
        " every signal, with the evidence behind it",
        bar(w),
        TBL([38, 11, 0], 2).head(["READINESS SIGNAL", "STATE", "EVIDENCE"]),
    ]
    for i, x in enumerate(_SIGNALS):
        rows.append(TSPEC["RD"].row(list(x), i == s.sel))
    rows.append(thin(w))
    rows.append(" APPROVAL  Granted at head 9d2b41c · still exact.")
    rows.append("           if that head moves this approval is invalidated and the")
    rows.append("           release returns to CANDIDATE with the cause named")
    rows.append(thin(w))
    rows.append(" NOT       Publish is unavailable — 1 member is not accepted.")
    frame = build(s, rows, keybar([("↑↓", "signal"), ("Esc", "back")], w), w, h)
    return with_state_rows("readiness", frame, s, w)
