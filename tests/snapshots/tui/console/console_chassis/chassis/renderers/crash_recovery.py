"""crash.recovery: the three doors back into a projection the console lost; no door discards work."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.frame import GTBL, g_frame, thin

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_KEYS: list[tuple[str, str]] = [
    ("↑↓", "door"),
    ("Enter", "choose"),
    ("i", "inspect"),
    ("Esc", "later"),
]


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    crk = GTBL([13, 21, 0])
    d = [
        ["reattach", "≈ 4s", "events after 41,208"],
        ["replay", "≈ 90s from 41,190", "nothing — exact to the point"],
        ["read-only", "≈ 1s", "no mutation until you attach"],
    ]
    dv.sel_in(s, len(d))
    b: list[str] = [
        " HAPPENED     The console lost its projection at 14:02:11.",
        "              Agents kept working · 4 runs were active then",
        thin(w),
        crk.head(["DOOR", "COSTS", "CANNOT RECOVER"]),
    ]
    for i, x in enumerate(d):
        b.append(crk.row(x, i == s.sel, w))
    cur = d[s.sel]
    b.extend(
        [
            thin(w),
            f" CHOSEN       {cur[0]} · costs {cur[1]}",
            f"              cannot recover {cur[2]}",
            thin(w),
            " NO LOSS      No door discards work; two of them defer reading it.",
        ]
    )
    return g_frame(
        s,
        fixture,
        "Eä ▸ eawf-core ▸ Recovery",
        "The console stopped at 14:02:11 · the daemon did not",
        b,
        _KEYS,
        w,
        h,
    )
