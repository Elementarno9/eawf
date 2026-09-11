"""track: three groups (milestones, campaigns, the shaped queue) that Tab cycles; the focused
group carries the cursor and the others recede, so which list the arrows walk is visible
rather than remembered. The fixture describes one track, so another track's id states
plainly that nothing is held for it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.frame import TBL, Fixed, bar, bar_, build, header_row, thin
from ...chassis.keys import ROUTE_KEYS
from ...chassis.width import pad

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session


@dataclass(frozen=True)
class _Group:
    id: str
    head: str
    rows: tuple[tuple[str, ...], ...]
    widths: tuple[int, ...]


_GROUPS: tuple[_Group, ...] = (
    _Group(
        "MILESTONES",
        TBL([34, 12, 9, 0], 2).head(["MILESTONES", "STATE", "BATCHES", "DUE"]),
        (
            ("MLS-0001 Replay-safe activity", "ACTIVE", "2", "W30"),
            ("MLS-0003 Escape ledger", "PLANNED", "0", "W32"),
            ("MLS-0000 Event normalizer", "COMPLETED", "3", "—"),
        ),
        (34, 12, 9),
    ),
    _Group(
        "CAMPAIGNS",
        TBL([34, 12, 9, 0], 2).head(["CAMPAIGNS", "STATE", "FINDINGS", "CHECKPOINT"]),
        (("CAM-0001 Provider drift", "REVIEW", "4", "W30"),),
        (34, 12, 9),
    ),
    _Group(
        "SHAPED QUEUE",
        TBL([34, 19, 0], 2).head(["SHAPED QUEUE", "DEFINITION", "DUE"]),
        (
            ("EAWF-0091 Bound replay window", "criteria missing", "W30"),
            ("EAWF-0092 Retire wave shim", "criteria set", "W31"),
        ),
        (34, 19),
    ),
)


def _recede(text: str, w: int) -> Fixed:
    return Fixed(pad(text, w))


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    gi = 0
    for x, g in enumerate(_GROUPS):
        if g.id == s.track_group:
            gi = x
    s.track_group = _GROUPS[gi].id
    dv.sel_in(s, len(_GROUPS[gi].rows))
    trk = s.subj_id or "Runtime"
    rows: list[str] = [
        header_row(s, fixture, f" Eä ▸ {fixture.scope} ▸ {trk}", w),
        f" Track {trk}" + (" · ACTIVE · 2 of 3 batches in flight" if trk == "Runtime" else ""),
        bar(w),
        " POLICY    WIP 3 batches · 1 campaign · dispatch on your word only",
    ]
    for ix, g in enumerate(_GROUPS):
        on = ix == gi
        rows.append(thin(w))
        rows.append(g.head if on else _recede(g.head, w))
        for i, r in enumerate(g.rows):
            line = (
                " "
                + ("▸ " if on and i == s.sel else "  ")
                + "".join(
                    pad(c, g.widths[ci]) if ci < len(g.widths) else c for ci, c in enumerate(r)
                )
            )
            rows.append(line if on else _recede(line, w))
    rows.append(thin(w))
    rows.append(" RETIRED   nothing retired in this track")
    if trk != "Runtime":
        rows = dv.absent(s, fixture, rows, trk, "milestones, campaigns or a shaped queue", w)
    return build(s, rows, bar_(s, ROUTE_KEYS["track"], w), w, h)
