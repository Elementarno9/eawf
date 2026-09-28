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

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from eawf.kernel.projection.attention import (
    attention_all,
    attention_mine,
    build_attention_view,
)
from eawf.kernel.projection.compute import ProjectionRow
from eawf.kernel.projection.registers import UNWRITTEN_REASON, RegisterView
from eawf.kernel.projection.truth import TruthState
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.attention import (
    NEEDS,
    OPEN,
    attn_list,
    audience_refusal,
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
from eawf.surfaces.tui.console.renderers.activity import beside
from eawf.surfaces.tui.console.renderers.read_model import (
    UNKNOWN_WORD,
    native_head,
    route_crumb,
)
from eawf.surfaces.tui.console.renderers.registers import UNWRITTEN_ROW
from eawf.surfaces.tui.console.tokens import TRUTH
from eawf.surfaces.tui.console.width import cell_len, pad

RAIL_W = 29
_KIND_INDENT = " " * 14
_LEDGER_LEAD = " " * 14 + "ledger  "
_LEDGER_CONT = " " * 22
_RESOLVED = "RESOLVED"
_VERB_KEYS = {"a": KEY["answer"], "x": KEY["deny"], "z": KEY["snooze"], "v": KEY["resolve"]}
_CLEAR_BUCKET = KEY["clear_bucket"]


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
        return [KEY["up"], KEY["open"], KEY["tab"], *verbs, KEY["actions"], escape]
    return [KEY["up"], KEY["open"], KEY["tab"], KEY["actions"], escape]


#: What the empty Attention route says on its sub line: the frozen phrase belongs here.
NOTHING_NEEDS_YOU = "nothing needs you"

#: The next move an empty Attention route offers instead of a dead screen.
EMPTY_NEXT = "nothing. Runs continue without you. g a shows what is executing."

#: What the frame states about the all-principals count, so it is never read as a queue.
NOT_A_WORK_LIST = "an all-principals count is not a work list"

#: What an absent deadline renders as: the register states none, which is not a zero.
NO_DEADLINE = "due –"  # noqa: RUF001

# The action kinds, as the selected row's second line names them.
_KIND_WORDS: Mapping[str, str] = MappingProxyType(
    {"protected_approval": "approval", "operator_decision": "decision"}
)
_MINE = "MINE"
_ALL_PRINCIPALS = "ALL PRINCIPALS"
_SEALED = "SEALED"


def counts_line(register: RegisterView, *, principal: str | None, complete: bool = True) -> str:
    """Return the line under the crumb: this principal's count, then every principal's.

    Both counts are the one Attention register's, and ``mine`` is the one the header's
    ``!N`` also reads, so none of the three can disagree. A console acting as nobody has
    no ``mine``, so it states the unknown token there rather than everyone's count; under
    a read that cannot vouch for every row the second count is labelled ``known``.

    Args:
        register: The Attention register.
        principal: Who the console acts as, or ``None``.
        complete: Whether the connection lets a count be called complete.
    """
    mine = attention_mine(register, principal=principal)
    every = attention_all(register)
    if not register.withheld and not int(every.value or 0):
        return NOTHING_NEEDS_YOU
    mine_n = (
        group_n(int(mine.value))
        if mine.state is TruthState.KNOWN and mine.value is not None
        else UNKNOWN_WORD
    )
    every_n = group_n(int(every.value or 0)) if not register.withheld else UNKNOWN_WORD
    label = "all principals" if complete else "known"
    return f"{mine_n} mine · {every_n} {label} · nothing here opened itself"


def bucket_items(register: RegisterView) -> list[dv.StripItem]:
    """Return ``all`` then the eight buckets, each ``needs operator`` need after it.

    One derivation feeds the strip and the rail, and a bucket filter never changes it; a
    bucket the register cannot count states the unknown token, never a zero.
    """
    view = build_attention_view(register)
    items = [dv.StripItem(None, "all", len(view.blocking()))]
    items.extend(
        dv.StripItem(
            f"{c.bucket.value}.{c.need.value}" if c.need else c.bucket.value,
            c.label,
            TRUTH["unknown"].unicode if c.count is None else c.count,
        )
        for c in view.bucket_counts()
    )
    return items


def rail_lines(register: RegisterView) -> list[str]:
    """Return the bucket rail: its head, then every bucket, the needs indented under theirs."""
    return [
        "BUCKETS",
        *(
            f" {pad(('  ' if x.label.startswith('↳') else '') + x.label, 24)}{x.n}"
            for x in bucket_items(register)[1:]
        ),
    ]


def kind_word(row: ProjectionRow) -> str:
    """Return the words an action's kind is read as, or the unknown token when unstated."""
    kind = row.facts.get("kind")
    return _KIND_WORDS.get(kind, kind.replace("_", " ")) if kind else UNKNOWN_WORD


def _section(row: ProjectionRow, open_keys: set[str], principal: str | None) -> str:
    """Return the section a row is listed under: MINE, ALL PRINCIPALS or SEALED."""
    if row.key not in open_keys:
        return _SEALED
    mine = principal is not None and not audience_refusal(row.assignee_ref, principal)
    return _MINE if mine else _ALL_PRINCIPALS


def eligibility_line(row: ProjectionRow, section: str, principal: str | None, holders: int) -> str:
    """Return who may act on ``row``: the second line of the selected row."""
    if section == _SEALED:
        return "sealed · it needs nobody"
    refusal = audience_refusal(row.assignee_ref, principal)
    if refusal:
        return refusal
    if principal is None:
        return "no principal is named · relaunch with --actor to act on it"
    return "you are the only eligible answer" if holders <= 1 else "you may answer"


def _empty_lines(view: View, register: RegisterView) -> list[str]:
    """Return the empty Attention route: the revision nothing is open at, and what next."""
    revision = group_n(int(register.source_cursor))
    return [
        f" NOTHING YET  nothing is open at revision {revision}",
        *(
            f"   {pad(x.label, 26)}{x.n}"
            for x in bucket_items(register)[1:]
            if "." not in (x.key or "")
        ),
        thin(view.w),
        f" WHAT TO DO   {EMPTY_NEXT}",
    ]


def native_frame(view: View, register: RegisterView) -> list[str]:
    """Return the Attention frame drawn from the register the daemon served.

    The items this principal may act on are listed under ``MINE``; an item addressed only
    to another principal is listed under ``ALL PRINCIPALS`` naming who holds it, so the
    all-principals count reconciles without ever being counted as this principal's; an
    answered one is listed under ``SEALED``.

    Args:
        view: The render being built.
        register: The Attention register at the committed cursor.

    Returns:
        The full frame, keybar last.
    """
    s, w, principal = view.session, view.w, view.principal
    rd = reads(s)
    items = build_attention_view(register).items if not register.withheld else ()
    open_keys = {item.key for item in items}
    order = {_MINE: 0, _ALL_PRINCIPALS: 1, _SEALED: 2}
    ordered = (
        sorted(register.rows, key=lambda row: order[_section(row, open_keys, principal)])
        if not register.withheld
        else []
    )
    found = next((i for i, row in enumerate(ordered) if row.key == s.sel_id), None)
    cursor = found if found is not None else min(max(s.sel, 0), max(len(ordered) - 1, 0))
    s.sel, s.sel_id = cursor, (ordered[cursor].key if ordered else None)
    wide = REGISTRY.rail_at(s.route, w) is not None
    col = w - RAIL_W - 1 if wide else w
    empty = not register.withheld and not open_keys
    top = native_head(
        view,
        register,
        crumb_text=route_crumb(register, "Needs you"),
        summary=(
            f"{counts_line(register, principal=principal, complete=rd.complete)} · "
            f"cursor {group_n(int(register.source_cursor))}"
        ),
    )
    if not wide and not empty and not register.withheld:
        top.append(dv.strip_row(s, bucket_items(register), w))
    body: list[str] = []
    if register.withheld:
        body.append(f" ACTIONS    {UNKNOWN_WORD} · {UNWRITTEN_REASON}")
    elif empty:
        body.extend(_empty_lines(view, register))
    else:
        holders = len({row.assignee_ref for row in register.rows} - {None} | {principal} - {None})
        sections = [_section(row, open_keys, principal) for row in ordered]
        table = Table([9, max(30, col - 32), 10, 0], 2)
        win = window_rows(view, total=len(ordered), cursor=cursor, chrome=len(top) + 5)
        last: str | None = None
        for index in range(win.start, win.stop):
            row, section = ordered[index], sections[index]
            if section != last:
                note = f" · {NOT_A_WORK_LIST}" if section == _ALL_PRINCIPALS else ""
                body.append(f" {section}  {group_n(sections.count(section))}{note}")
                last = section
            subject = row.facts.get("subject", UNKNOWN_WORD)
            question = row.facts.get("question") or row.title or row.urn
            status = value_cell(row.status).slot
            cells = [row.key, f"{subject} {question}", status, NO_DEADLINE]
            body.append(table.row(cells, index == cursor))
            if index == cursor:
                who = eligibility_line(row, section, principal, holders)
                body.append(_KIND_INDENT + f"{kind_word(row)} · {who}")
    if wide and not empty and not register.withheld:
        body = beside(body, rail_lines(register), col, w)
    rows = [*top, *body]
    if register.withheld:
        rows += [
            thin(w),
            UNWRITTEN_ROW
            + " "
            + " · ".join(f"{name} {UNKNOWN_WORD}" for name in register.withheld),
        ]
    selected = ordered[cursor] if ordered else None
    offered = (
        selected is not None and _section(selected, open_keys, principal) == _MINE and can_mutate(s)
    )
    verbs = set(_VERB_KEYS.values())
    keys = [key for key in native_keys(s.route) if offered or key not in verbs]
    return build(view, rows, route_keys_bar(view, keys))


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
