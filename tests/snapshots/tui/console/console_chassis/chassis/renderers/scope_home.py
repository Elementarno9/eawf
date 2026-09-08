"""scope.home: the landing route. Two regions and a tree: the outcomes pane is a tree a
track expands to its milestones (the cursor lands on leaves only), Tab moves to the
attention list, the pane that does not own the arrows recedes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.attention import open_actions, xlabel, xtop
from ...chassis.frame import TBL, Fixed, bar, bar_, build, header_row, snap_caret, thin
from ...chassis.keys import ROUTE_KEYS, Ctx, busy, go
from ...chassis.registry import route_of
from ...chassis.seam import attn_cell, reads
from ...chassis.width import cell_len, pad

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    P = fixture.proto
    open_ = open_actions(fixture)
    n = len(open_)
    attn = s.home_region == "attention" and n > 0
    if attn:
        s.home_sel = max(0, min(n - 1, s.home_sel or 0))
    ti = dv.home_track(s, fixture)
    mi = s.home_ms
    s.sel = ti
    rd = reads(s)

    def dim(t: str) -> Fixed:
        return Fixed(pad(t, w))

    def cur(t: str) -> Fixed:
        return Fixed(pad(t, w))

    rows: list[str] = [
        header_row(s, fixture, f" Eä ▸ {P.scope}", w),
        f" {len(P.tracks)} tracks · {len(P.fleet)} runs"
        + ("" if rd.complete else f" · {rd.label}"),
        bar(w),
    ]
    if not rd.complete:
        rows.append(f" ATTACHED  {rd.age}")
        rows.append(thin(w))
    SH = TBL([30, 7, 12, 0], 2)
    SM = TBL([34, 0], 4)
    head = SH.head(["MILESTONES", "RUNS", "ATTENTION", "PROGRESS"])
    rows.append(dim(head) if attn else head)
    for i, t in enumerate(P.tracks):
        tn = sum(1 for a in open_ if a.track == t.id)
        on = (not attn) and i == ti and mi < 0
        raw = snap_caret(
            SH.row([t.id, str(t.runs), f"!{tn}" if tn else attn_cell(s, 0), t.prog], on)
        )
        rows.append(dim(raw) if attn else (cur(raw) if on else raw))
        if i == ti:
            for k, m in enumerate(t.milestones):
                onm = (not attn) and mi == k
                mr = snap_caret(SM.row([f"{m.id} {m.name}", m.state], onm))
                rows.append(dim(mr) if attn else (Fixed(pad(mr, w)) if onm else mr))
    rows.append(thin(w))
    rows.append(" ATTENTION" + ("" if n else "   nothing here opened itself"))
    if not n:
        rows.append("   nothing is waiting on you · runs continue without you")
    last_b = None
    for ai, a in enumerate(open_):
        xg = xtop(a.bucket)
        if xg != last_b:
            bn = sum(1 for x in open_ if xtop(x.bucket) == xg)
            gh = f" {xlabel(fixture, xg).upper()}  {bn}"
            rows.append(gh if attn else dim(gh))
            last_b = xg
        on = attn and ai == (s.home_sel or 0)
        row = " " + ("▸ " if on else "  ") + pad(a.text, w - 3 - 7) + " " + a.due
        if cell_len(row) > w:
            row = row[:w]
        rows.append(cur(row) if on else (row if attn else dim(row)))
    return build(s, rows, bar_(s, ROUTE_KEYS["scope.home"], w), w, h)


def seam(ctx: Ctx, k: str, shift: bool) -> bool:
    """Two regions and a tree: Tab swaps regions, arrows walk the focused one, Enter opens."""
    s = ctx.s
    P = ctx.fixture.proto
    if s.route != "scope.home" or busy(s):
        return False
    open_ = open_actions(ctx.fixture)
    if k == "Tab":
        if not open_:
            ctx.log("Tab", "nothing is waiting — no list to focus")
            return True
        s.home_region = "outcomes" if s.home_region == "attention" else "attention"
        s.home_sel = s.home_sel or 0
        ctx.log(
            "Tab",
            "focus → what wants you · ↑↓ picks · Enter opens it"
            if s.home_region == "attention"
            else "focus → outcomes",
        )
        return True
    if s.home_region == "attention" and open_:
        if k in ("ArrowDown", "ArrowUp"):
            s.home_sel = ((s.home_sel or 0) + (1 if k == "ArrowDown" else len(open_) - 1)) % len(
                open_
            )
            ctx.log(k, open_[s.home_sel].text[:44])
            return True
        if k == "Enter":
            it = open_[s.home_sel or 0] if (s.home_sel or 0) < len(open_) else None
            entity = dv.entity_id_in(it.text if it else "")
            to = (entity and route_of(entity)) or "attention"
            s.sel_id = entity or None
            go(ctx, to, f"what wants you · {entity or 'row'}", entity)
            return True
        if k == "Escape":
            s.home_region = "outcomes"
            ctx.log("Esc", "focus → outcomes")
            return True
    if s.home_region != "attention":
        if k in ("ArrowDown", "ArrowUp"):
            dv.home_step(s, ctx.fixture, 1 if k == "ArrowDown" else -1)
            t = P.tracks[s.home_track or 0]
            m = s.home_ms
            ctx.log(k, t.id if m < 0 else f"  ↳ {t.milestones[m].id} {t.milestones[m].name}")
            return True
        if k == "Enter":
            t2 = P.tracks[s.home_track or 0]
            m2 = s.home_ms
            if m2 >= 0:
                msid = t2.milestones[m2].id
                mto = route_of(msid) or "milestone"
                go(ctx, mto, f"tree · {msid}", msid)
                return True
    return False
