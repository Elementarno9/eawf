"""backlog: two focusable groups, drafts and deferred, walked by one cursor.

Tab moves the focus between the groups, and the group without the cursor recedes.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from eawf.surfaces.tui.console.frame import Fixed, Grid, View, g_frame, g_row, thin
from eawf.surfaces.tui.console.navigation import Ctx, busy
from eawf.surfaces.tui.console.width import cell_len, pad

_FIRST_INK = re.compile(r"\S")
_KEYS: tuple[tuple[str, str], ...] = (
    ("↑↓", "row"),
    ("Tab", "group"),
    ("Enter", "promote"),
    ("/", "palette"),
    ("Esc", "back"),
)
_GRID = Grid([11, 11, 26, 23, 0], 2)
_DRAFTS = "DRAFTS"
_DEFERRED = "DEFERRED"


def _caret(row: str, on: bool) -> str:
    """Set the cursor against the list it walks rather than in the wide first cell."""
    found = _FIRST_INK.search(row)
    if not on or found is None or found.start() < 2:
        return row
    at = found.start()
    return row[: at - 2] + "▸ " + row[at:]


def _group(view: View, name: str, heads: Sequence[str], rows: Sequence[Any]) -> list[str]:
    """Return one group: its head on the row grid, then its rows, receded when unfocused."""
    s, w = view.session, view.w
    on = (s.bl_group or _DRAFTS) == name
    raw = _GRID.row([name, "", "", *heads], False, w)
    tail = raw[raw.index(name) + cell_len(name) :].rstrip()
    out: list[str] = [Fixed(pad(f" {name}  {tail}", w))]
    for i, r in enumerate(rows):
        row = _caret(_GRID.row(["", r[0], r[1], r[2], r[3]], False, w), on and i == s.sel)
        out.append(g_row(row, w) if on else Fixed(pad(row, w)))
    out.append(thin(w))
    return out


def render(view: View) -> list[str]:
    """Return the Backlog frame."""
    s = view.session
    reg = view.fixture.registers
    lists = {_DRAFTS: reg.bl_drafts, _DEFERRED: reg.bl_deferred}
    n = len(lists.get(s.bl_group or _DRAFTS, ()))
    if n:
        s.sel = max(0, min(n - 1, s.sel))
    body = _group(view, _DRAFTS, ["STATUS", "DUE"], reg.bl_drafts)
    body += _group(view, _DEFERRED, ["REASON", "SINCE"], reg.bl_deferred)
    return g_frame(
        view,
        crumb=f"Eä ▸ {view.fixture.scope} ▸ Backlog",
        ctx=f"{len(reg.bl_drafts)} drafts · {len(reg.bl_deferred)} deferred",
        body=body,
        keys=_KEYS,
    )


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Cycle Tab over the groups a cursor can walk; the promotion readout is not a stop."""
    s = ctx.s
    if s.route != "backlog" or busy(s) or key != "Tab":
        return False
    groups = list(ctx.fixture.registers.backlog_groups)
    cur = s.bl_group or _DRAFTS
    at = groups.index(cur) if cur in groups else -1
    s.bl_group = groups[(at + 1) % len(groups)]
    s.sel = 0
    ctx.log("Tab", f"group → {s.bl_group}")
    return True
