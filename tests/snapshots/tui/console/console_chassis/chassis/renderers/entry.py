"""Layer 0: the eight pre-session states. The header slot carries the process state
because no projection exists yet; the keybar carries the state's own keys plus the
simulator pair ``[ ] state`` and ``/ attach later``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ...chassis.frame import bar, build, keybar, thin
from ...chassis.keys import entry_keys
from ...chassis.width import cell_len, pad

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_TRAIL = re.compile(r"\s+$")


def _entry_header(session: Session, fixture: Fixture, w: int) -> str:
    P = fixture.proto
    e = P.entry[session.entry_sel] if session.entry_sel < len(P.entry) else P.entry[0]
    right = f"{e.glyph} {e.state}"
    return pad(" Eä", w - cell_len(right)) + right


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    P = fixture.proto
    E = P.entry
    e = E[session.entry_sel] if session.entry_sel < len(E) else E[0]
    rows: list[str] = [
        _entry_header(session, fixture, w),
        f" {e.title} · state {session.entry_sel + 1} of {len(E)}",
        bar(w),
    ]
    if e.panes:
        for label, text in e.panes:
            rows.append(" " + pad(label, 10) + text)
    if e.cols:
        rows.append(_TRAIL.sub("", " " + "".join(pad(c[0], c[1]) for c in e.cols)))
        for i, r in enumerate(e.rows or ()):
            cells = "".join(
                pad(str(r[j]), c[1] - (2 if j == 0 else 0)) for j, c in enumerate(e.cols)
            )
            rows.append((" ▸ " if i == session.path_sel else "   ") + _TRAIL.sub("", cells))
    if e.paths:
        pl = e.pathsLabel or "PATHS"
        pn = e.pathsNote or "In the order they are meant to be run."
        if cell_len(pl) > 18:
            raise ValueError(f"paths label too long: {pl}")
        rows.append(thin(w))
        rows.append(" " + pad(pl, max(10, cell_len(pl) + 1)) + pn)
        for i, (name, text) in enumerate(e.paths):
            lead = name if e.pathsOrdered is False else f"{i + 1}  {name}"
            rows.append("   " + ("▸ " if i == session.path_sel else "  ") + pad(lead, 14) + text)
    rows.append(thin(w))
    for t in e.tail:
        rows.append(f"   {t}" if t else "")
    pairs = [k.pair() for k in entry_keys(session, fixture)]
    return build(session, rows, keybar(pairs, w), w, h)
