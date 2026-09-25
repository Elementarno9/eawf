"""activity: the fleet table with the bucket strip at 80 columns and a bucket rail wider.

The window follows the cursor and always says what is off screen.
"""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.fixture import FleetRow
from eawf.surfaces.tui.console.frame import (
    Fixed,
    Table,
    View,
    bar,
    build,
    header,
    route_keys_bar,
    thin,
    window_rows,
)
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.reads import prototype_attached, reads
from eawf.surfaces.tui.console.renderers.registers import native_frame
from eawf.surfaces.tui.console.width import pad

RAIL_W = 29
_AS_OF_W = 6
_FOOTER = 2
_COL_HEAD = 1


def _rows(view: View) -> list[FleetRow]:
    """Return the fleet rows the bucket and the filter leave."""
    s = view.session
    flt = dv.filter_of(s).lower()
    return [
        f
        for f in view.fixture.proto.fleet
        if not (s.bucket and f.bucket != s.bucket)
        and not (flt and flt not in f"{f.run}{f.task}{f.reason}".lower())
    ]


def _head(view: View, wide: bool) -> list[str]:
    s, fx, w = view.session, view.fixture, view.w
    proto = fx.proto
    rd = reads(s)
    rows = [
        header(view, f" Eä ▸ {proto.scope} ▸ Activity"),
        f" {len(proto.fleet)} runs" + ("" if rd.complete else f" · {rd.label}"),
        bar(w),
    ]
    if not rd.complete:
        rows.append(f" ATTACHED  {prototype_attached(rd, fx)}")
    if s.typing or dv.filter_of(s):
        rows.append(
            " FILTER    \\"
            + dv.filter_of(s)
            + ("▏" if s.typing else "")
            + "   Esc clears · Enter keeps"
        )
    if not wide:
        items = [dv.StripItem(None, "all", len(proto.fleet))] + [
            dv.StripItem(b.key, b.label, dv.fleet_bucket_count(fx, b.key)) for b in proto.buckets
        ]
        rows.append(dv.strip_row(s, items, w))
    return rows


def _with_rail(view: View, body: list[str], col: int) -> list[str]:
    """Return ``body`` beside the bucket rail; a bucket the filter excludes recedes."""
    s, fx, w = view.session, view.fixture, view.w
    buckets = fx.proto.buckets
    rail = ["BUCKETS"] + [
        ("▸" if b.key == s.bucket else " ")
        + pad(b.label, 24)
        + str(dv.fleet_bucket_count(fx, b.key))
        for b in buckets
    ]
    out: list[str] = []
    for ri in range(max(len(body), len(rail))):
        left = pad(body[ri] if ri < len(body) else "", col)
        right = pad(rail[ri] if ri < len(rail) else "", RAIL_W - 2)
        bucket = buckets[ri - 1] if 0 < ri <= len(buckets) else None
        receded = bool(bucket and s.bucket and bucket.key != s.bucket)
        raw = pad(left + "│ " + right, w)
        out.append(Fixed(raw) if receded else raw)
    return out


def render(view: View) -> list[str]:
    """Return the Activity frame."""
    if view.register is not None:
        return native_frame(view, view.register)
    s, w = view.session, view.w
    rows_all = _rows(view)
    if s.sel >= len(rows_all):
        s.sel = max(0, len(rows_all) - 1)
    wide = w >= 120
    col = w - RAIL_W - 1
    rd = reads(s)
    head_rows = _head(view, wide)
    win = window_rows(
        view, total=len(rows_all), cursor=s.sel, chrome=len(head_rows) + _FOOTER + _COL_HEAD
    )
    shown = rows_all[win.start : win.stop]
    row_w = col if wide else w
    cols = [16, 26, 12, max(14, row_w - 16 - 26 - 12 - _AS_OF_W)]
    body: list[str] = [
        Table([cols[0] - 3, cols[1], cols[2], cols[3], 0], 2).head(
            ["RUN", "TASK", "STATE", "REASON", "AS OF"]
        )
    ]
    for i, f in enumerate(shown):
        body.append(
            (" ▸ " if i + s.scroll == s.sel else "   ")
            + pad(f.run, cols[0] - 3)
            + pad(f.task, cols[1])
            + pad(f.state, cols[2])
            + pad(f.reason, cols[3])
            + f.as_of
        )
    if not rows_all:
        body.append("   nothing matches the filter")
    if wide:
        body = _with_rail(view, body, col)
    rows = head_rows + body
    rows.append(thin(w))
    rows.append(
        win.line(complete=rd.complete) + (" matching" if (s.bucket or dv.filter_of(s)) else "")
    )
    return build(view, rows, route_keys_bar(view, ROUTE_KEYS["activity"]))
