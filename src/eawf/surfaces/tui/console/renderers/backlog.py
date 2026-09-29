"""backlog: two focusable groups, drafts and deferred, walked by one cursor.

Tab moves the focus between the groups, and the group without the cursor recedes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from types import MappingProxyType
from typing import Any

from eawf.kernel.projection.spine import SpineRow, SpineView
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.format import day, group
from eawf.surfaces.tui.console.frame import (
    Fixed,
    Grid,
    View,
    acting_pairs,
    build,
    g_frame,
    g_row,
    thin,
    window_rows,
)
from eawf.surfaces.tui.console.keybar import keybar, route_pairs
from eawf.surfaces.tui.console.keymap import native_keys
from eawf.surfaces.tui.console.navigation import Ctx, busy
from eawf.surfaces.tui.console.reads import reads
from eawf.surfaces.tui.console.renderers.read_model import (
    UNKNOWN_WORD,
    cursor_note,
    native_head,
    route_crumb,
)
from eawf.surfaces.tui.console.renderers.spine import held
from eawf.surfaces.tui.console.width import cell_len, clip, pad

_FIRST_INK = re.compile(r"\S")
_KEYS = route_pairs("backlog")
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


#: The groups the native backlog lists, and the status each one holds.
GROUPS: tuple[tuple[str, str], ...] = ((_DRAFTS, "DRAFT"), (_DEFERRED, "DEFERRED"))

#: What each group says when it holds no Task, so an empty group is not a dangling head.
_NONE_IN: Mapping[str, str] = MappingProxyType(
    {_DRAFTS: "∅ no draft Task", _DEFERRED: "∅ nothing deferred"}
)

#: The next move an empty backlog offers instead of a dead screen.
EMPTY_NEXT = "a drafted Task lands here · g t shows what is planned"


def _date(stamp: str | None) -> str:
    """Return the day a stored instant falls on, or the unknown token when it states none."""
    try:
        return day(datetime.fromisoformat(stamp)) if stamp else UNKNOWN_WORD
    except ValueError:
        return UNKNOWN_WORD


def _native_rows(
    view: View, rows: Sequence[SpineRow], group: str, on: bool, shown: range
) -> list[str]:
    """Return one group's Task rows in ``shown``: key, title, then the group's two columns."""
    s, w = view.session, view.w
    grid = _native_grid(w)
    out: list[str] = []
    for i in shown:
        row = rows[i]
        # a hard cut, so every clipped title ends in the same cell of its column
        title = clip(row.title or UNKNOWN_WORD, grid.cols[2] - 1)
        if group == _DRAFTS:
            due = row.facts.get("due")
            tail = [promotion_needs(row), due or "undated"]
        else:
            tail = [value_cell(row.field("reason")).full, _date(row.facts.get("updated_at"))]
        line = _caret(grid.row(["", row.key, title, *tail], False, w), on and i == s.sel)
        out.append(g_row(line, w) if on else Fixed(pad(line, w)))
    return out


# The rows the unfocused group lists before it counts the rest.
_PEEK = 2
# The cells the native grid's key, status and due columns take; the title takes the rest
# up to its cap, and a wider frame leaves the remainder empty rather than stretching it.
_KEY_W, _STATUS_W, _DUE_W, _TITLE_MAX = 11, 24, 12, 48


def promotion_needs(row: SpineRow) -> str:
    """Return what a draft still needs before it can be promoted, or that it is ready.

    A planned Task must hold criteria and a Batch, and a draft holds neither by
    construction, so the column names what promotion still has to supply rather than
    repeating the ``DRAFT`` its group already says.

    Args:
        row: A draft Task's row; its criteria count and its Batch are the promotion fields.

    Returns:
        ``needs criteria · batch`` naming each missing field, or ``ready to promote``.
    """
    held = (("criteria", row.facts.get("criteria")), ("batch", row.parent_key))
    missing = [word for word, value in held if not value]
    return f"needs {' · '.join(missing)}" if missing else "ready to promote"


def _native_grid(w: int) -> Grid:
    """Return the backlog grid at width ``w``: the title narrows so the due never clips."""
    title = min(_TITLE_MAX, max(16, w - 2 - 11 - _KEY_W - _STATUS_W - _DUE_W))
    return Grid([11, _KEY_W, title, _STATUS_W, 0], 2)


def native_backlog(view: View, spine: SpineView) -> list[str]:
    """Return the Backlog drawn from the Task register: drafts, then deferred Tasks.

    An empty backlog is its own frame: it names the revision at which nothing is queued
    and offers the next move, and a read that cannot vouch for every Task says that
    instead of calling the backlog empty.

    Args:
        view: The render being built; its session names the focused group.
        spine: The Task register at the committed cursor.

    Returns:
        The full frame, keybar last.
    """
    s, w = view.session, view.w
    grouped = {
        name: [row for row in spine.rows if row.field("status").value == status]
        for name, status in GROUPS
    }
    focus = s.bl_group if s.bl_group in grouped else _DRAFTS
    shown = grouped[focus]
    at = next((i for i, row in enumerate(shown) if row.key == s.sel_id), None)
    s.sel = at if at is not None else min(max(s.sel, 0), max(len(shown) - 1, 0))
    s.sel_id = shown[s.sel].key if shown else None
    drafts, deferred = (len(grouped[name]) for name, _status in GROUPS)
    top = native_head(
        view,
        spine,
        crumb_text=route_crumb(view, spine, "Backlog"),
        summary=f"{drafts} drafts · {deferred} deferred{cursor_note(spine)}",
    )
    body: list[str] = []
    if not drafts and not deferred:
        revision = group(int(spine.source_cursor))
        if reads(s).complete:
            body += [
                f" NOTHING QUEUED  no draft and no deferred Task at revision {revision}",
                f"   {EMPTY_NEXT}",
            ]
        else:
            body += [
                f"   no queued Task is known at revision {revision} · "
                "this read cannot vouch for every Task",
            ]
        # with no group holding a Task there is no row to walk or open and no group to swap
        s.nav_rows = 0
        idle = [pair for pair in _KEYS if pair[0] != "Tab"]
        return build(view, [*top, *body], keybar(acting_pairs(view, idle), w))
    other = next(name for name, _status in GROUPS if name != focus)
    peek = min(len(grouped[other]), _PEEK)
    # the focused group is windowed into what the other group and the heads leave
    chrome = len(top) + 2 * 2 + peek + (1 if len(grouped[other]) > peek else 0) + 2
    win = window_rows(view, total=len(shown), cursor=s.sel, chrome=chrome)
    for name, heads in ((_DRAFTS, ["STATUS", "DUE"]), (_DEFERRED, ["REASON", "SINCE"])):
        raw = _native_grid(w).row([name, "", "", *heads], False, w)
        tail = raw[raw.index(name) + cell_len(name) :].rstrip()
        body.append(Fixed(pad(f" {name}  {tail}", w)))
        rows = grouped[name]
        if not rows:
            body.append(Fixed(pad(f"   {_NONE_IN[name]}", w)))
        elif name == focus:
            body += _native_rows(view, rows, name, True, range(win.start, win.stop))
            if win.hides:
                body.append(win.line(complete=spine.complete))
        else:
            body += _native_rows(view, rows, name, False, range(peek))
            if len(rows) > peek:
                body.append(Fixed(pad(f"   … {len(rows) - peek} more · Tab walks them", w)))
        body.append(thin(w))
    s.nav_rows = len(shown)
    # a group cut to its window pages as well as steps, and its bar says so
    pairs = [entry.pair() for entry in native_keys("backlog", windowed=s.windowed)]
    return build(view, [*top, *body], keybar(acting_pairs(view, pairs), w))


def render(view: View) -> list[str]:
    """Return the Backlog frame, native when a read model is held."""
    spine = held(view)
    if spine is not None:
        return native_backlog(view, spine)
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
    native = isinstance(ctx.projection, SpineView)
    groups = (
        [name for name, _status in GROUPS] if native else list(ctx.fixture.registers.backlog_groups)
    )
    cur = s.bl_group or _DRAFTS
    at = groups.index(cur) if cur in groups else -1
    s.bl_group = groups[(at + (-1 if shift else 1)) % len(groups)]
    s.sel = 0
    ctx.log("Tab", f"group → {s.bl_group}")
    return True
