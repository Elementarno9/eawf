"""cost.ceiling: spend against the day's ceiling; observe only, the ceiling moves in settings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.frame import GTBL, g_frame, thin

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_KEYS: list[tuple[str, str]] = [("↑↓", "row"), ("Enter", "run"), ("Esc", "back")]


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    cc = GTBL([17, 8, 0])
    st = [
        ["RUN-1c93af08", "13:41", "hard limit reached"],
        ["RUN-4e2b6c77", "12:08", "hard limit reached"],
    ]
    dv.sel_in(s, len(st))
    b: list[str] = [
        " CEILING      ~ 41.20 of 60.00 today · ≈ 18.80 left",
        " OWNED BY     settings ▸ execution ▸ concurrency · policy",
        thin(w),
        " STOPPED",
        cc.head(["RUN", "AT", "REASON"]),
    ]
    for i, x in enumerate(st):
        b.append(cc.row(x, i == s.sel, w))
    b.extend(
        [
            thin(w),
            " SPEND        claude  ~ 31.40 · 8 runs",
            "              codex   ~ 9.80 · 6 runs",
            "              local   ∅ unmetered — no rate card",
            thin(w),
            " AUTHORITY    This surface observes — the ceiling moves in settings.",
        ]
    )
    return g_frame(
        s,
        fixture,
        "Eä ▸ eawf-core ▸ Cost ceiling",
        "spend against ceiling · observe only",
        b,
        _KEYS,
        w,
        h,
    )
