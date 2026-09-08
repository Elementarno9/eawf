"""trust: the milestone's truth fields, who answered for each, and the agents' track record."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.frame import GTBL, chip, g_frame, thin

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_KEYS: list[tuple[str, str]] = [
    ("↑↓", "field"),
    ("Enter", "evidence"),
    (".", "actions"),
    ("i", "inspect"),
    ("Esc", "back"),
]


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    f = [
        ["jury-01", chip("ok", "pass"), "conformance runner", "14:01"],
        ["jury-02", chip("ok", "pass"), "conformance runner", "13:58"],
        ["jury-03", chip("info", "? unknown"), "conformance runner", "no outcome recorded"],
    ]
    dv.sel_in(s, len(f))
    t = GTBL([10, 12, 21, 0])
    tr = GTBL([16, 11, 11, 0], 3)
    # the column says who answered for the field, never the internal "producer" word
    b: list[str] = [t.head(["JURY", "VERDICT", "ANSWERED BY", "FRESHNESS"])]
    for i, r in enumerate(f):
        b.append(t.row(r, i == s.sel, w))
    b.extend(
        [
            thin(w),
            " CALIBRATION  ~ 0.31 Brier over 41 resolved · metrics · 13:40",
            thin(w),
            " TRACK RECORD",
            tr.head(["AGENT", "ACCEPTED", "REJECTED", "RATE"]),
            tr.row(["claude", "18", "2", "~ 0.90"], False, w),
            tr.row(["codex", "6", "0", "~ 1.00"], False, w),
            tr.row(["local-runner", "0", "0", "∅ unavailable"], False, w),
            thin(w),
            " FIELD        A rate over zero judged attempts is ∅ unavailable, never 0.00.",
        ]
    )
    return g_frame(
        s,
        fixture,
        "Eä ▸ eawf-core ▸ MLS-0007 ▸ Trust",
        "Milestone MLS-0007 · 3 truth fields · as of 14:02",
        b,
        _KEYS,
        w,
        h,
    )
