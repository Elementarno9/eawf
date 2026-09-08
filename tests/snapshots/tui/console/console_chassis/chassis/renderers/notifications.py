"""notifications: which run classes may interrupt and who decided; read only, the policy
lives in settings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.frame import boxed, g_pad

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_CLASSES: list[list[str]] = [
    ["needs permission", "yes", "policy"],
    ["needs your answer", "yes", "policy"],
    ["stopped responding", "yes", "policy"],
    ["run finished", "no", "profile"],
    ["budget passed", "no", "ratified · R23"],
]
_KEYS: list[tuple[str, str]] = [("↑↓", "class"), ("Esc", "close")]


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    dv.sel_in(s, len(_CLASSES))
    nl: list[str] = ["\x07CLASS               MAY INTERRUPT   DECIDED BY\x06", ""]
    for i, c in enumerate(_CLASSES):
        nl.append(("▸" if i == s.sel else " ") + g_pad(c[0], 19) + g_pad(c[1], 16) + c[2])
    cur = _CLASSES[s.sel]
    nl.extend(
        [
            "",
            "Read only · settings ▸ interface owns this policy.",
            "A muted class still counts in !N NEEDS YOU.",
        ]
    )
    verb = "interrupts you" if cur[1] == "yes" else "does not interrupt you"
    foot = f"A run in this class {verb}, and {cur[2]} decided that."
    return boxed(
        s,
        fixture,
        "Eä ▸ eawf-core ▸ Notifications",
        "26 runs · following",
        [],
        "NOTIFICATIONS · what may interrupt",
        nl,
        foot,
        _KEYS,
        w,
        h,
    )
