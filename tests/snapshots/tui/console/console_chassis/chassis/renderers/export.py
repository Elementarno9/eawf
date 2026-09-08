"""export: the parts a plain-text report of a Run carries and what each one costs; nothing
leaves the machine."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.frame import boxed, g_pad

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_PARTS: list[list[str]] = [
    ["timeline", "yes", "41,208 events", "Every event this Run recorded, in order."],
    ["usage and cost", "yes", "~ 4.62 · derived", "Derived from the events, not from a bill."],
    ["transcript", "yes", "6,102 lines known", "What the runner said, quoted exactly."],
    ["secrets", "never", "∅ redacted by policy", "Policy redacts these; no export can carry them."],
    [
        "sandbox decisions",
        "no",
        "142 · space includes",
        "Left out — including them adds 142 lines.",
    ],
]
_KEYS: list[tuple[str, str]] = [("↑↓", "part"), ("Enter", "export"), ("Esc", "cancel")]


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    dv.sel_in(s, len(_PARTS))
    pl: list[str] = ["\x07PART                INCLUDED   SIZE\x06", ""]
    for i, p in enumerate(_PARTS):
        pl.append(("▸" if i == s.sel else " ") + g_pad(p[0], 19) + g_pad(p[1], 11) + p[2])
    pl.extend(["", "Plain text, one line per fact — nothing leaves the machine."])
    foot = f"{_PARTS[s.sel][3]}  Enter writes the report and names the path."
    return boxed(
        s,
        fixture,
        "Eä ▸ … ▸ RUN-538453eb ▸ Export",
        "Run RUN-538453eb · claude · WAIT-PERM",
        [],
        "EXPORT · report this Run",
        pl,
        foot,
        _KEYS,
        w,
        h,
    )
