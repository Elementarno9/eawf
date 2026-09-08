"""activity: the fleet table with the bucket strip at 80 and the bucket rail at 120/160;
the window follows the cursor and always says what is off screen."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.frame import TBL, Fixed, bar, bar_, build, header_row, thin
from ...chassis.keys import ROUTE_KEYS
from ...chassis.seam import reads
from ...chassis.width import pad

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    P = fixture.proto
    flt = dv.filter_of(s).lower()
    rows_all = [
        f
        for f in P.fleet
        if not (s.bucket and f.bucket != s.bucket)
        and not (flt and flt not in f"{f.run}{f.task}{f.reason}".lower())
    ]
    if s.sel >= len(rows_all):
        s.sel = max(0, len(rows_all) - 1)
    wide = w >= 120
    RW = 29
    col = w - RW - 1
    rd = reads(s)
    head_rows: list[str] = [
        header_row(s, fixture, f" Eä ▸ {P.scope} ▸ Activity", w),
        f" {len(P.fleet)} runs" + ("" if rd.complete else f" · {rd.label}"),
        bar(w),
    ]
    if not rd.complete:
        head_rows.append(f" ATTACHED  {rd.age}")
    if s.typing or dv.filter_of(s):
        head_rows.append(
            " FILTER    \\"
            + dv.filter_of(s)
            + ("▏" if s.typing else "")
            + "   Esc clears · Enter keeps"
        )
    if not wide:
        items = [{"key": None, "label": "all", "n": len(P.fleet)}] + [
            {"key": b.key, "label": b.label, "n": dv.bucket_count(fixture, b.key)}
            for b in P.buckets
        ]
        head_rows.append(dv.strip_row(s, items, w))
    footer = 2
    col_head = 1
    visible = max(1, h - 1 - len(head_rows) - footer - col_head - (s.reserved or 0))
    if s.sel >= len(rows_all):
        s.sel = max(0, len(rows_all) - 1)
    if s.sel < s.scroll:
        s.scroll = s.sel
    if s.sel >= s.scroll + visible:
        s.scroll = s.sel - visible + 1
    s.scroll = max(0, min(s.scroll, max(0, len(rows_all) - visible)))
    s.visible = visible
    shown = rows_all[s.scroll : s.scroll + visible]
    row_w = col if wide else w
    as_of_w = 6
    reason_w = max(14, row_w - 16 - 26 - 12 - as_of_w)
    COL = [16, 26, 12, reason_w]
    body: list[str] = [
        TBL([COL[0] - 3, COL[1], COL[2], COL[3], 0], 2).head(
            ["RUN", "TASK", "STATE", "REASON", "AS OF"]
        )
    ]
    for i, f in enumerate(shown):
        body.append(
            (" ▸ " if i + s.scroll == s.sel else "   ")
            + pad(f.run, COL[0] - 3)
            + pad(f.task, COL[1])
            + pad(f.state, COL[2])
            + pad(f.reason, COL[3])
            + f.as_
        )
    if not rows_all:
        body.append("   nothing matches the filter")
    if wide:
        rail = ["BUCKETS"] + [
            ("▸" if b.key == s.bucket else " ")
            + pad(b.label, 24)
            + str(dv.bucket_count(fixture, b.key))
            for b in P.buckets
        ]
        n = max(len(body), len(rail))
        out2: list[str] = []
        for ri in range(n):
            left = pad(body[ri] if ri < len(body) else "", col)
            rr = pad(rail[ri] if ri < len(rail) else "", RW - 2)
            bkt = P.buckets[ri - 1] if 0 < ri <= len(P.buckets) else None
            off = bool(bkt and s.bucket and bkt.key != s.bucket)
            raw = pad(left + "│ " + rr, w)
            out2.append(Fixed(raw) if off else raw)
        body = out2
    rows = head_rows + body
    rows.append(thin(w))
    window = f"{s.scroll + 1}–{s.scroll + len(shown)}" if shown else "0"
    rows.append(
        f" WINDOW    {window} of {len(rows_all)}"
        + ("" if rd.complete else " known")
        + (" matching" if (s.bucket or dv.filter_of(s)) else "")
    )
    return build(s, rows, bar_(s, ROUTE_KEYS["activity"], w), w, h)
