"""search: entity hits for one query, exact counts, event text never searched."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.frame import GTBL, g_frame, thin

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_KEYS: list[tuple[str, str]] = [
    ("↑↓", "hit"),
    ("Enter", "drill"),
    ("\\", "refine"),
    ("k", "kind"),
    ("Esc", "back"),
]


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    sr = GTBL([18, 18, 0])
    hits = [
        ["RUN-538453eb", "task title", "EAWF-0042 Bound replay"],
        ["RUN-be1e085a", "task title", "EAWF-0051 Coalesce"],
        ["EAWF-0042", "name", "Bound replay window"],
        ["BAT-0001", "name", "Semantic event slice"],
        ["MLS-0001", "name", "Replay-safe activity"],
        ["CLM-0004", "claim text", "ordering under replay"],
    ]
    dv.sel_in(s, len(hits))
    b: list[str] = [
        " QUERY        replay ▏",
        " KINDS        runs 6 · tasks 4 · batches 2 · milestones 1 · claims 1",
        thin(w),
        sr.head(["HIT", "WHAT MATCHED", "WHERE"]),
    ]
    for i, x in enumerate(hits):
        b.append(sr.row(x, i == s.sel, w))
    b.extend(
        [
            thin(w),
            " WINDOW       6 of 14 · sorted by kind, then id · every count exact",
            " SCOPE        Entities only — event text is not searched.",
        ]
    )
    return g_frame(
        s,
        fixture,
        "Eä ▸ eawf-core ▸ Search",
        "query “replay” · entities only · 14 hits, exact",
        b,
        _KEYS,
        w,
        h,
    )
