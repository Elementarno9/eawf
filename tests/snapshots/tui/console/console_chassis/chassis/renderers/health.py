"""health: every check drawn with its own result, the focused check's repair docked to the foot."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.frame import GTBL, chip, g_frame, g_pad, thin

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_KEYS: list[tuple[str, str]] = [
    ("↑↓", "check"),
    ("Enter", "detail"),
    ("\\", "filter"),
    ("Esc", "back"),
]


def _checks() -> list[tuple[str, str, str, str | None]]:
    return [
        (
            "Policy revision reachable",
            chip("er", "FAILED"),
            "cannot read revision",
            "Re-point the sandbox policy at a readable revision.",
        ),
        ("Heartbeat probe certified", chip("ok", "pass"), "14:00", None),
        ("Digest store writable", chip("ok", "pass"), "14:00", None),
        (
            "Clock skew within 250 ms",
            chip("info", "? unknown"),
            "probe never ran",
            "Run the clock probe on the host that never answered.",
        ),
        ("Event schema accepted", chip("ok", "pass"), "14:01", None),
        ("Snapshot restore verified", chip("ok", "pass"), "13:58", None),
        (
            "Provider rate cards current",
            chip("info", "? unknown"),
            "no card for the local runner",
            "Attach a rate card for the local runner, or accept it unmetered.",
        ),
        ("Replay digests reproducible", chip("ok", "pass"), "13:31", None),
        ("Retention sweep on schedule", chip("ok", "pass"), "09:12", None),
    ]


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    # two columns of air between the longest check name and its result cell
    hc = GTBL([30, 11, 0])
    c = _checks()
    dv.sel_in(s, len(c))
    b: list[str] = [
        f" CHECKS       {len(c)} checks · 1 failed · 2 unknown · 6 pass · as of 14:02",
        hc.head(["CHECK", "RESULT", "REASON"]),
    ]
    for i, x in enumerate(c):
        b.append(hc.row([x[0], x[1], x[2]], i == s.sel, w))
    # the repair follows the cursor at the foot, so it never floats with the list's height
    rep = c[s.sel][3]
    foot = [
        thin(w),
        " REPAIR       " + (rep or "∅ Nothing to repair · this check passes."),
        "              "
        + (
            "Naming it happens here; running it lives in settings."
            if rep
            else "Its last result stands until the next sweep."
        ),
    ]
    while len(b) + len(foot) < h - 4:
        b.append(g_pad("", w))
    b.extend(foot)
    return g_frame(
        s,
        fixture,
        "Eä ▸ eawf-core ▸ Health",
        "Fleet health · conformance runner · as of 14:02",
        b,
        _KEYS,
        w,
        h,
    )
