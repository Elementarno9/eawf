"""attention: the exception buckets, severity-first, as a strip at 80 columns and a rail wider.

The counts derive from the action register and stay fleet-wide whatever the bucket filter
shows.
"""

from __future__ import annotations

from collections.abc import Sequence

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.attention import (
    NEEDS,
    OPEN,
    attn_list,
    bucket_count,
    bucket_label,
    open_count,
    top_bucket,
    verbs_for,
)
from eawf.surfaces.tui.console.fixture import Action
from eawf.surfaces.tui.console.frame import (
    Fixed,
    Table,
    View,
    bar,
    build,
    header,
    route_keys_bar,
    thin,
)
from eawf.surfaces.tui.console.keybar import KEY, KeyEntry
from eawf.surfaces.tui.console.reads import attn_cell, can_mutate, reads
from eawf.surfaces.tui.console.renderers.registers import native_frame
from eawf.surfaces.tui.console.width import cell_len, pad

RAIL_W = 29
_KIND_INDENT = " " * 14
_LEDGER_LEAD = " " * 14 + "ledger  "
_LEDGER_CONT = " " * 22
_RESOLVED = "RESOLVED"
_VERB_KEYS = {"a": KEY["answer"], "x": KEY["deny"], "z": KEY["snooze"], "v": KEY["resolve"]}
_CLEAR_BUCKET = KeyEntry("clear bucket", ("Escape",))


def _group_of(action: Action) -> str:
    return top_bucket(action.bucket) if action.state == OPEN else _RESOLVED


def _ledger_rows(action: Action, bw: int) -> list[str]:
    """Return the ledger wrapped rather than cut: a confirmed stage is never truncated."""
    out: list[str] = []
    line_w = bw - cell_len(_LEDGER_LEAD)
    cur = ""
    first = True
    for stage, when in action.ledger:
        step = f"{stage} {when}"
        piece = (cur + " → " if cur else "") + step
        if cell_len(piece) > line_w:
            out.append((_LEDGER_LEAD if first else _LEDGER_CONT) + cur + " →")
            first = False
            cur = step
        else:
            cur = piece
    if cur:
        out.append((_LEDGER_LEAD if first else _LEDGER_CONT) + cur)
    return out


def _items(view: View) -> list[dv.StripItem]:
    """Return every bucket with its open count, ``all`` first, sub-buckets after their parent."""
    fx = view.fixture
    proto = fx.proto
    items = [dv.StripItem(None, "all", sum(1 for a in proto.attention if a.state == OPEN))]
    for bucket in proto.xbuckets:
        items.append(dv.StripItem(bucket.key, bucket.label, bucket_count(fx, bucket.key)))
        items.extend(
            dv.StripItem(sub.key, "↳ " + sub.label, bucket_count(fx, sub.key))
            for sub in bucket.sub or ()
        )
    return items


def _body(view: View, shown: Sequence[Action], bw: int) -> list[str]:
    s, fx = view.session, view.fixture
    body: list[str] = []
    last: str | None = None
    table = Table([9, max(30, bw - 32), 10, 0], 2)
    for i, action in enumerate(shown):
        group = _group_of(action)
        if group != last:
            # the count belongs to the bucket it counts: a fleet total restates the header
            n = sum(1 for x in shown if _group_of(x) == group)
            name = _RESOLVED if group == _RESOLVED else bucket_label(fx, group).upper()
            body.append(f" {name}  {n}")
            last = group
        body.append(table.row([action.id, action.text, action.state, action.due], i == s.sel))
        if i == s.sel:
            body.append(_KIND_INDENT + action.kind)
            if action.ledger:
                body.extend(_ledger_rows(action, bw))
    if not shown:
        body.append("   nothing in this bucket needs you")
    return body


def _receded(view: View, item: dv.StripItem | None) -> bool:
    """Return whether a rail row recedes: the filter excludes it and it is not a child."""
    bucket = view.session.bucket
    if item is None or not bucket:
        return False
    return not (item.key == bucket or (bucket == NEEDS and top_bucket(item.key) == NEEDS))


def _with_rail(view: View, body: list[str], items: list[dv.StripItem], col: int) -> list[str]:
    """Return ``body`` beside the rail: every bucket, sub-buckets indented, filtered ones dim."""
    s, w = view.session, view.w
    rail = ["BUCKETS"] + [
        ("▸" if x.key == s.bucket else " ") + pad(x.label, 24) + str(x.n) for x in items[1:]
    ]
    out: list[str] = []
    for ri in range(max(len(body), len(rail))):
        left = pad(body[ri] if ri < len(body) else "", col)
        right = pad(rail[ri] if ri < len(rail) else "", RAIL_W - 2)
        item = items[ri] if 0 < ri < len(items) else None
        raw = pad(left + "│ " + right, w)
        out.append(Fixed(raw) if _receded(view, item) else raw)
    return out


def _keys(view: View, shown: Sequence[Action]) -> list[KeyEntry]:
    s = view.session
    selected = shown[s.sel] if s.sel < len(shown) else None
    escape = _CLEAR_BUCKET if s.bucket else KEY["esc"]
    if can_mutate(s) and selected is not None and selected.state == OPEN:
        verbs = [_VERB_KEYS[v] for v in verbs_for(selected)]
        return [KEY["up"], KEY["tab"], *verbs, KEY["actions"], escape]
    return [KEY["up"], KEY["tab"], KEY["actions"], escape]


def render(view: View) -> list[str]:
    """Return the Attention frame."""
    if view.register is not None:
        return native_frame(view, view.register)
    s, fx, w, h = view.session, view.fixture, view.w, view.h
    n = open_count(fx)
    rd = reads(s)
    shown = attn_list(s, fx)
    head = [
        header(view, f" Eä ▸ {fx.proto.scope} ▸ Needs you"),
        f" {attn_cell(s, n)} mine · {attn_cell(s, n)}"
        + (" fleet-wide" if rd.complete else " known")
        + " · nothing here opened itself",
        bar(w),
    ]
    if not rd.complete:
        head.extend([f" ATTACHED  {rd.age}", thin(w)])
    wide = w >= 120
    col = w - RAIL_W - 1
    bw = col if wide else w
    items = _items(view)
    if not wide:
        head.append(dv.strip_row(s, items, w))
    dv.sel_by_id(s, [a.id for a in shown])
    body = _body(view, shown, bw)
    if wide:
        body = _with_rail(view, body, items, col)
    rows = head + body
    if s.bucket:
        # the readout docks to the foot of its frame rather than floating under the list
        rows.extend("" for _ in range(h - 3 - len(rows)))
        rows.append(thin(w))
        open_n = sum(1 for a in shown if a.state == OPEN)
        label = bucket_label(fx, s.bucket).upper()
        rows.append(f" WINDOW    ▸{label}  {open_n} of {items[0].n} matching")
    return build(view, rows, route_keys_bar(view, _keys(view, shown)))
