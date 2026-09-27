"""attention: the exception buckets, severity-first, as a strip at 80 columns and a rail wider.

The counts derive from the action register and stay fleet-wide whatever the bucket filter
shows.

The native frame draws the same shape from the Attention register: the ``mine`` and
fleet-wide counts under the crumb, the actions grouped by the status they state, and the
buckets as a strip at 80 columns and the route's declared rail wider. A register nothing
writes yet states no count at all, so both counts and the list say so rather than drawing
an empty register as ``nothing needs you``.
"""

from __future__ import annotations

from collections.abc import Sequence

from eawf.kernel.projection.registers import UNWRITTEN_REASON, RegisterView, attention_mine
from eawf.kernel.projection.truth import TruthState
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
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.fixture import Action
from eawf.surfaces.tui.console.format import group as group_n
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
from eawf.surfaces.tui.console.keybar import KEY, KeyEntry
from eawf.surfaces.tui.console.keymap import native_keys
from eawf.surfaces.tui.console.reads import can_mutate, prototype_attached, reads
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.renderers.activity import beside, bucket_items, rail_lines
from eawf.surfaces.tui.console.renderers.read_model import (
    UNKNOWN_WORD,
    counts,
    native_head,
    route_crumb,
)
from eawf.surfaces.tui.console.renderers.registers import UNWRITTEN_ROW, restore
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


def counts_line(register: RegisterView) -> str:
    """Return the line under the crumb: this principal's count, the fleet-wide count.

    Both counts are the one Attention register's, which the header's ``!N`` also reads, so
    none of the three can disagree; a register nothing writes states neither.
    """
    mine = attention_mine(register)
    if mine.state is not TruthState.KNOWN or mine.value is None:
        return f"mine {UNKNOWN_WORD} · fleet-wide {UNKNOWN_WORD} · nothing here opened itself"
    n = group_n(int(mine.value))
    return f"{n} mine · {group_n(len(register.rows))} fleet-wide · nothing here opened itself"


def native_frame(view: View, register: RegisterView) -> list[str]:
    """Return the Attention frame drawn from the register the daemon served.

    Args:
        view: The render being built.
        register: The Attention register at the committed cursor.

    Returns:
        The full frame, keybar last.
    """
    s, w = view.session, view.w
    cursor = restore(s, register)
    wide = REGISTRY.rail_at(s.route, w) is not None
    col = w - RAIL_W - 1 if wide else w
    top = native_head(
        view,
        register,
        crumb_text=route_crumb(register, "Needs you"),
        summary=f"{counts_line(register)} · {counts(register)}",
    )
    if not wide:
        top.append(dv.strip_row(s, bucket_items(register), w))
    table = Table([9, max(30, col - 32), 10, 0], 2)
    body: list[str] = []
    if register.withheld:
        body.append(f" ACTIONS    {UNKNOWN_WORD} · {UNWRITTEN_REASON}")
    elif not register.rows:
        body.append("   nothing in this bucket needs you")
    win = window_rows(view, total=len(register.rows), cursor=cursor, chrome=len(top) + 4)
    last: str | None = None
    for index in range(win.start, win.stop):
        row = register.rows[index]
        status = value_cell(row.status).slot
        if status != last:
            body.append(f" {status}  {register.status_counts().get(status, 0)}")
            last = status
        body.append(
            table.row([row.key, row.title or row.urn, status, UNKNOWN_WORD], index == cursor)
        )
    if wide:
        body = beside(body, rail_lines(register), col, w)
    rows = [*top, *body]
    if register.withheld:
        rows += [
            thin(w),
            UNWRITTEN_ROW
            + " "
            + " · ".join(f"{name} {UNKNOWN_WORD}" for name in register.withheld),
        ]
    return build(view, rows, route_keys_bar(view, native_keys(s.route)))


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
        f" {n} mine · {n}"
        + (" fleet-wide" if rd.complete else " known")
        + " · nothing here opened itself",
        bar(w),
    ]
    if not rd.complete:
        head.extend([f" ATTACHED  {prototype_attached(rd, fx)}", thin(w)])
    wide = REGISTRY.rail_at("attention", w) is not None
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
