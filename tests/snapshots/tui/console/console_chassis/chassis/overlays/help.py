"""The help overlay: the keymap for THIS route, derived from the same key table the
keybar renders, plus the EVERYWHERE block. The `w` row is a simulator row."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis.frame import bar, build, header_row, keybar, thin
from ...chassis.keys import ENTRY_ALLOW, GLOBAL_HELP, route_keys
from ...chassis.width import pad

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    P = fixture.proto
    rows: list[str] = [
        header_row(session, fixture, f" Eä ▸ help · {session.route}", w),
        " the keymap for THIS route, not a global cheat sheet",
        bar(w),
        " THIS ROUTE",
    ]
    rk = route_keys(session, fixture)
    for e in rk:
        rows.append("   " + pad(e.token, 11) + ("null" if e.label is None else e.label))
    if not rk:
        rows.append("   no route-local key — only the global set below applies")
    pre = session.route == "entry"
    e0 = P.entry[session.entry_sel] if session.entry_sel < len(P.entry) else P.entry[0]
    allow_pre = [p[0] for p in e0.keys] + list(ENTRY_ALLOW)
    rows.append(thin(w))
    rows.append(" EVERYWHERE")
    for token, text, key in GLOBAL_HELP:
        if key == "w" and not session.simulator:
            continue
        if pre and key not in allow_pre:
            continue
        rows.append("   " + pad(token, 11) + text)
    if pre:
        rows.append("   before a session exists, only the keys above are bound")
    rows.append(thin(w))
    rows.append(" Esc Esc quits only at scope home, with nothing open and no")
    rows.append(" outstanding control — and only if the presses are 80ms to 1.5s apart.")
    return build(session, rows, keybar([("Esc", "close")], w), w, h)
