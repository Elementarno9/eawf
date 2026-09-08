"""backlog: two focusable groups (DRAFTS, DEFERRED) walked by one cursor; Tab moves the
focus between them and the group without the cursor dims (proto-g's R['backlog'])."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ...chassis.frame import GTBL, Fixed, g_frame, g_row, thin
from ...chassis.keys import Ctx, busy
from ...chassis.width import cell_len, pad

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_FIRST_INK = re.compile(r"\S")
_KEYS: list[tuple[str, str]] = [
    ("↑↓", "row"),
    ("Tab", "group"),
    ("Enter", "promote"),
    ("/", "palette"),
    ("Esc", "back"),
]


def _caret(row: str, on: bool) -> str:
    """The cursor sits against the list it walks: with an 11-wide first cell a gutter caret
    stood ten columns clear of the ids and read as marking the frame, not the row."""
    if not on:
        return row
    m = _FIRST_INK.search(row)
    if m is None or m.start() < 2:
        return row
    at = m.start()
    return row[: at - 2] + "▸ " + row[at:]


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    G = fixture.g
    BL = GTBL([11, 11, 26, 23, 0], 2)
    grp = s.bl_group or "DRAFTS"
    lists = {"DRAFTS": G.BL_DRAFTS, "DEFERRED": G.BL_DEFERRED}
    n = len(lists.get(grp, ()))
    if n:
        s.sel = max(0, min(n - 1, s.sel or 0))
    b: list[str] = []

    def dim(t: str) -> Fixed:
        return Fixed(pad(t, w))

    def group(name: str, cells: list[str]) -> bool:
        # The head is composed by the same spec the rows use, with the group's own name in the
        # first cell; the title then moves out of the cursor gutter to column 1 and the two
        # columns it frees become spaces so every cell after it stays on the row grid.
        on = grp == name
        raw = BL.row([name, *cells], False, w)
        tail = raw[raw.index(name) + cell_len(name) :].rstrip()
        b.append(Fixed(pad(" " + name + "  " + tail, w)))
        return on

    on_d = group("DRAFTS", ["", "", "STATUS", "DUE"])
    for i, r in enumerate(G.BL_DRAFTS):
        row = _caret(BL.row(["", r[0], r[1], r[2], r[3]], False, w), i == (s.sel or 0) and on_d)
        b.append(g_row(row, w) if on_d else dim(row))
    b.append(thin(w))
    on_f = group("DEFERRED", ["", "", "REASON", "SINCE"])
    for i, r in enumerate(G.BL_DEFERRED):
        row = _caret(BL.row(["", r[0], r[1], r[2], r[3]], False, w), i == (s.sel or 0) and on_f)
        b.append(g_row(row, w) if on_f else dim(row))
    b.append(thin(w))
    return g_frame(
        s,
        fixture,
        f"Eä ▸ {fixture.scope} ▸ Backlog",
        f"{len(G.BL_DRAFTS)} drafts · {len(G.BL_DEFERRED)} deferred",
        b,
        _KEYS,
        w,
        h,
    )


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Tab cycles only the groups a cursor can walk; PROMOTION is a readout, not a stop."""
    s = ctx.s
    if s.route != "backlog" or busy(s) or key != "Tab":
        return False
    groups = list(ctx.fixture.g.BACKLOG_GROUPS)
    cur = s.bl_group or "DRAFTS"
    gi = groups.index(cur) if cur in groups else -1
    s.bl_group = groups[(gi + 1) % len(groups)]
    s.sel = 0
    ctx.log("Tab", f"group → {s.bl_group}")
    return True
