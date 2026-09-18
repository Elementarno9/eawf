"""scope.home: the landing route, two regions and a tree.

The outcomes pane is a tree a track expands to its milestones, and the cursor lands on
leaves only. Tab moves to the attention list, and the pane that does not own the arrows
recedes.
"""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.attention import bucket_label, open_actions, top_bucket
from eawf.surfaces.tui.console.fixture import Action
from eawf.surfaces.tui.console.frame import (
    Fixed,
    Table,
    View,
    bar,
    build,
    header,
    route_keys_bar,
    snap_caret,
    thin,
)
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.navigation import Ctx, busy, go
from eawf.surfaces.tui.console.reads import attn_cell, reads
from eawf.surfaces.tui.console.registry import route_of
from eawf.surfaces.tui.console.renderers.spine import held, native_frame
from eawf.surfaces.tui.console.width import cell_len, pad

ATTENTION_REGION = "attention"
OUTCOMES_REGION = "outcomes"
_TRACKS = Table([30, 7, 12, 0], 2)
_MILESTONES = Table([34, 0], 4)
# Cells a due cell takes at the end of an attention row, with its space.
_DUE_W = 7


def _tree(view: View, open_: list[Action], attn: bool) -> list[str]:
    """Return the track table with the focused track's milestones expanded under it."""
    s, fx, w = view.session, view.fixture, view.w
    ti = dv.home_track(s, fx)
    head = _TRACKS.head(["MILESTONES", "RUNS", "ATTENTION", "PROGRESS"])
    rows: list[str] = [Fixed(pad(head, w)) if attn else head]
    for i, track in enumerate(fx.proto.tracks):
        tn = sum(1 for a in open_ if a.track == track.id)
        on = not attn and i == ti and s.home_ms < 0
        cells = [track.id, str(track.runs), f"!{tn}" if tn else attn_cell(s, 0), track.prog]
        raw = snap_caret(_TRACKS.row(cells, on))
        rows.append(Fixed(pad(raw, w)) if attn or on else raw)
        if i != ti:
            continue
        for k, milestone in enumerate(track.milestones):
            onm = not attn and s.home_ms == k
            leaf = snap_caret(
                _MILESTONES.row([f"{milestone.id} {milestone.name}", milestone.state], onm)
            )
            rows.append(Fixed(pad(leaf, w)) if attn or onm else leaf)
    return rows


def _attention(view: View, open_: list[Action], attn: bool) -> list[str]:
    """Return the attention list, grouped by bucket; it recedes unless it holds the focus."""
    s, fx, w = view.session, view.fixture, view.w
    rows = [" ATTENTION" + ("" if open_ else "   nothing here opened itself")]
    if not open_:
        rows.append("   nothing is waiting on you · runs continue without you")
    last: str | None = None
    for i, action in enumerate(open_):
        group = top_bucket(action.bucket)
        if group != last:
            n = sum(1 for x in open_ if top_bucket(x.bucket) == group)
            head = f" {bucket_label(fx, group).upper()}  {n}"
            rows.append(head if attn else Fixed(pad(head, w)))
            last = group
        on = attn and i == s.home_sel
        row = " " + ("▸ " if on else "  ") + pad(action.text, w - 3 - _DUE_W) + " " + action.due
        if cell_len(row) > w:
            row = row[:w]
        rows.append(Fixed(pad(row, w)) if on or not attn else row)
    return rows


def render(view: View) -> list[str]:
    """Return the scope-home frame, native when a read model is held."""
    spine = held(view)
    if spine is not None:
        return native_frame(view, spine)
    s, fx, w = view.session, view.fixture, view.w
    proto = fx.proto
    open_ = open_actions(fx)
    attn = s.home_region == ATTENTION_REGION and bool(open_)
    if attn:
        s.home_sel = max(0, min(len(open_) - 1, s.home_sel))
    s.sel = dv.home_track(s, fx)
    rd = reads(s)
    rows = [
        header(view, f" Eä ▸ {proto.scope}"),
        f" {len(proto.tracks)} tracks · {len(proto.fleet)} runs"
        + ("" if rd.complete else f" · {rd.label}"),
        bar(w),
    ]
    if not rd.complete:
        rows.extend([f" ATTACHED  {rd.age}", thin(w)])
    rows.extend(_tree(view, open_, attn))
    rows.append(thin(w))
    rows.extend(_attention(view, open_, attn))
    return build(view, rows, route_keys_bar(view, ROUTE_KEYS["scope.home"]))


def _attention_key(ctx: Ctx, key: str, open_: list[Action]) -> bool:
    """Handle a key while the attention list holds the focus."""
    s = ctx.s
    if key in ("ArrowDown", "ArrowUp"):
        step = 1 if key == "ArrowDown" else len(open_) - 1
        s.home_sel = (s.home_sel + step) % len(open_)
        ctx.log(key, open_[s.home_sel].text[:44])
        return True
    if key == "Enter":
        action = open_[s.home_sel] if s.home_sel < len(open_) else None
        entity = dv.entity_id_in(action.text if action else "")
        to = (entity and route_of(entity)) or "attention"
        s.sel_id = entity or None
        go(ctx, to, f"what wants you · {entity or 'row'}", entity)
        return True
    if key == "Escape":
        s.home_region = OUTCOMES_REGION
        ctx.log("Esc", "focus → outcomes")
        return True
    return False


def _tree_key(ctx: Ctx, key: str) -> bool:
    """Handle a key while the outcome tree holds the focus."""
    s = ctx.s
    tracks = ctx.fixture.proto.tracks
    if key in ("ArrowDown", "ArrowUp"):
        dv.home_step(s, ctx.fixture, 1 if key == "ArrowDown" else -1)
        track = tracks[s.home_track]
        at = s.home_ms
        leaf = track.milestones[at] if at >= 0 else None
        ctx.log(key, track.id if leaf is None else f"  ↳ {leaf.id} {leaf.name}")
        return True
    if key == "Enter" and s.home_ms >= 0:
        milestone = tracks[s.home_track].milestones[s.home_ms].id
        go(ctx, route_of(milestone) or "milestone", f"tree · {milestone}", milestone)
        return True
    return False


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Swap regions on Tab, walk the focused region on the arrows, open on Enter."""
    s = ctx.s
    if s.route != "scope.home" or busy(s):
        return False
    open_ = open_actions(ctx.fixture)
    if key == "Tab":
        if not open_:
            ctx.log("Tab", "nothing is waiting — no list to focus")
            return True
        on_list = s.home_region != ATTENTION_REGION
        s.home_region = ATTENTION_REGION if on_list else OUTCOMES_REGION
        ctx.log(
            "Tab",
            "focus → what wants you · ↑↓ picks · Enter opens it" if on_list else "focus → outcomes",
        )
        return True
    if s.home_region == ATTENTION_REGION and open_:
        return _attention_key(ctx, key, open_)
    if s.home_region != ATTENTION_REGION:
        return _tree_key(ctx, key)
    return False
