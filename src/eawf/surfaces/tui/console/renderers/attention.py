"""attention: the exception buckets, severity-first, as a strip at 80 columns and a rail wider.

The counts derive from the action register and stay fleet-wide whatever the bucket filter
shows.

The native frame draws the same shape from the Attention register: the ``mine`` and
fleet-wide counts under the crumb, the open actions grouped by the exception bucket each
lands in, and the buckets as a strip at 80 columns and the route's declared rail wider. A
register nothing writes yet states no count at all, so both counts and the list say so
rather than drawing an empty register as ``nothing needs you``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from types import MappingProxyType
from typing import Final

from eawf.kernel.projection.attention import (
    AttentionBucket,
    AttentionItem,
    AttentionNeedKind,
    BucketSource,
    attention_all,
    attention_mine,
    build_attention_view,
)
from eawf.kernel.projection.compute import ProjectionRow
from eawf.kernel.projection.registers import UNWRITTEN_REASON, RegisterView
from eawf.kernel.projection.truth import TruthState
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.budget.notices import BudgetThresholdNotice
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.attention import (
    NEEDS,
    OPEN,
    attn_list,
    audience_refusal,
    bucket_count,
    bucket_label,
    listed_items,
    open_count,
    top_bucket,
    verbs_for,
)
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.decisions import PauseRecord, PauseStatus
from eawf.surfaces.tui.console.fixture import Action
from eawf.surfaces.tui.console.format import group as group_n
from eawf.surfaces.tui.console.format import instant, span
from eawf.surfaces.tui.console.frame import (
    RowWindow,
    Table,
    View,
    bar,
    build,
    header,
    recede_rail,
    route_keys_bar,
    thin,
    window_rows,
)
from eawf.surfaces.tui.console.keybar import KEY, KeyEntry
from eawf.surfaces.tui.console.keymap import native_keys
from eawf.surfaces.tui.console.notices import (
    NOTICE_BUCKET,
    NOTICE_VERBS,
    notice_cells,
    notice_detail,
)
from eawf.surfaces.tui.console.overlays.situations import pause_situation
from eawf.surfaces.tui.console.reads import can_mutate, prototype_attached, reads
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.renderers.activity import beside
from eawf.surfaces.tui.console.renderers.read_model import (
    UNKNOWN_WORD,
    cursor_note,
    label,
    native_head,
    route_crumb,
)
from eawf.surfaces.tui.console.renderers.registers import UNWRITTEN_ROW
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
        out.append(recede_rail(raw, w) if _receded(view, item) else raw)
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
EMPTY_NEXT = "nothing. Runs continue without you."

#: What the frame states about the all-principals count, so it is never read as a queue.
NOT_A_WORK_LIST = "an all-principals count is not a work list"

#: What a notice's second line says in place of who may answer it.
NOTICE_LINE = "a notice · nothing answers it, and it counts toward no one"

#: What a stalled Run's second line says in place of who may answer it.
STALL_LINE = "Enter opens the pause over this Run: resume or let go"

#: Why a pending action's answer keys are not offered: an answer is sealed under a receipt.
NO_RECEIPT_LINE = "answering is sealed under an evidence receipt · relaunch with --receipt-ref"

#: Seconds in a day, the grain an age is stated in from a day on.
_DAY: Final = 86_400

#: What an absent deadline renders as: the register states none, which is not a zero.
NO_DEADLINE = "due –"  # noqa: RUF001

# The action kinds, as the selected row's second line names them.
_KIND_WORDS: Mapping[str, str] = MappingProxyType(
    {
        "protected_approval": "approval",
        "operator_decision": "decision",
        "provider_permission": "permission",
        "child_ceiling_breach": "ceiling breach",
        "run_stall": "stall",
        "run_state": "run",
        "verdict_observation": "audit verdict",
    }
)


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
    return f"{mine_n} mine · {every_n} {label}"


def bucket_items(register: RegisterView, *, notices: int) -> list[dv.StripItem]:
    """Return ``all`` then the counted buckets, each ``needs operator`` need after it.

    One derivation feeds the strip and the rail, and a bucket filter never changes it; a
    bucket no record feeds is not drawn, so no zero reads as counted.
    ``all`` is the filter that lists every bucket, so it counts every item they list,
    notices included; who must answer is the summary line's count, not this one.

    Args:
        register: The Attention register.
        notices: The budget notices listed beside it, which count under ``over budget``
            with the ceiling breaches.
    """
    view = build_attention_view(register)
    items = [dv.StripItem(None, "all", len(view.items) + notices)]
    # a bucket no record feeds is left off: a Run that stopped answering is counted under
    # stalled, as its Run frame and Activity call it, never under a second name
    items.extend(
        dv.StripItem(
            f"{c.bucket.value}.{c.need.value}" if c.need else c.bucket.value,
            c.label,
            c.count + (notices if c.bucket is AttentionBucket.OVER_BUDGET else 0),
        )
        for c in view.bucket_counts()
        if c.source is not BucketSource.HOLE
    )
    return items


def rail_lines(register: RegisterView, bucket: str | None, *, notices: int) -> list[str]:
    """Return the bucket rail: its head, then every bucket, the needs indented under theirs.

    The chosen bucket carries the caret, so the rail says which bucket the list shows
    even when nothing is open in it.
    """
    return [
        "BUCKETS",
        *(
            ("▸" if x.key == bucket else " ")
            + f"{pad(('  ' if x.label.startswith('↳') else '') + x.label, 24)}{x.n}"
            for x in bucket_items(register, notices=notices)[1:]
        ),
    ]


def kind_word(row: ProjectionRow) -> str:
    """Return the words an action's kind is read as, or the unknown token when unstated."""
    kind = row.facts.get("kind")
    return _KIND_WORDS.get(kind, kind.replace("_", " ")) if kind else UNKNOWN_WORD


def eligibility_line(row: ProjectionRow, principal: str | None, holders: int) -> str:
    """Return who may act on ``row``: the owner half of the selected row's second line."""
    refusal = audience_refusal(row.assignee_ref, principal)
    if refusal:
        return refusal
    if principal is None:
        return "no principal is named · relaunch with --actor to act on it"
    return "you are the only eligible answer" if holders <= 1 else "you may answer"


def due_cell(row: ProjectionRow, now: datetime | None) -> str:
    """Return the due cell: a permission's provider deadline, else how long it has waited.

    A record with no deadline is not due at any time, so the cell says how old it is
    instead, which is what tells a stale item from a fresh one.

    Args:
        row: The register row.
        now: The instant its age is measured to; ``None`` states no age.
    """
    deadline = row.facts.get("deadline_at")
    if deadline and len(deadline) >= 16:
        return deadline[11:16]
    asked = instant(row.facts.get("created_at"))
    if asked is None or now is None:
        return NO_DEADLINE
    return f"{age_words(max(0, int((now - asked).total_seconds())))} old"


def age_words(total: int) -> str:
    """Return a non-negative age in whole seconds: days from a day on, else a span."""
    return f"{total // _DAY}d" if total >= _DAY else span(total)


def permission_lines(row: ProjectionRow) -> list[str]:
    """Return who may decide a provider permission, when it expires, and what it lacks.

    The repository affordance is drawn disabled naming the classes that may approve,
    never left out, and there is no hold: the provider owns the deadline and it expires.
    """
    approve = row.facts.get("approve", UNKNOWN_WORD)
    repository = (
        "repository approve enabled"
        if row.facts.get("repository_may_approve") == "yes"
        else f"repository approve disabled · approve is {approve} only"
    )
    stated = row.facts.get("deadline_at", "")
    deadline = f"{stated[11:19]} UTC" if len(stated) >= 19 else UNKNOWN_WORD
    return [
        f"approve {approve} · deny {row.facts.get('deny', UNKNOWN_WORD)} · {repository}",
        f"expires {deadline} · no hold: the provider owns the deadline",
    ]


def _selected_lines(
    row: ProjectionRow, item: AttentionItem, principal: str | None, holders: int, sealable: bool
) -> list[str]:
    """Return the selected row's second lines: its kind and who may act, then any authority.

    A pending action whose answer the console holds no receipt to seal under says so, in
    place of the answer keys the keybar leaves out.
    """
    if item.read_only:
        return [_KIND_INDENT + f"{kind_word(row)} · {NOTICE_LINE}"]
    if item.bucket is AttentionBucket.STALLED:
        return [_KIND_INDENT + f"{kind_word(row)} · {STALL_LINE}"]
    lines = [_KIND_INDENT + f"{kind_word(row)} · {eligibility_line(row, principal, holders)}"]
    if item.need is AttentionNeedKind.PERMISSION:
        lines.extend(_KIND_INDENT + line for line in permission_lines(row))
    if row.collection is Epoch2Collection.PENDING_ACTION and principal and not sealable:
        lines.append(_KIND_INDENT + NO_RECEIPT_LINE)
    return lines


def _empty_lines(register: RegisterView, bucket: str | None) -> list[str]:
    """Return the body of a list with nothing open in it, and the next move.

    Args:
        register: The Attention register, whose revision nothing is open at.
        bucket: The chosen bucket; a filtered-out list says so rather than calling the
            register empty.
    """
    if bucket is not None:
        return ["   nothing in this bucket needs you"]
    revision = group_n(int(register.source_cursor))
    return [
        label("NOTHING YET", f"nothing is open at revision {revision}"),
        "",
        label("WHAT TO DO", EMPTY_NEXT),
    ]


def native_frame(view: View, register: RegisterView) -> list[str]:
    """Return the Attention frame drawn from the register the daemon served.

    Only open items are listed -- an answered one needs nobody, so it is not attention --
    grouped under the exception bucket each lands in, severity first. The selected row's
    second line names its kind and who may act on it, so an item addressed only to another
    principal is listed naming who holds it and never offered as this principal's. The
    buckets are always drawn: as a strip at 80 columns and as the route's rail wider.

    Args:
        view: The render being built.
        register: The Attention register at the committed cursor.

    Returns:
        The full frame, keybar last.
    """
    s, w, principal = view.session, view.w, view.principal
    rd = reads(s)
    by_key = {row.key: row for row in register.rows}
    listed = listed_items(register, s.bucket)
    # a budget notice lands in the over-budget bucket, so a filter on another hides it
    notices = list(view.notices) if s.bucket in (None, AttentionBucket.OVER_BUDGET.value) else []
    # a pause is work held waiting, listed beside what it waits on and counted by neither
    pauses = held_pauses(view) if s.bucket is None else []
    keys = [
        *(item.key for item in listed),
        *(pause.id for pause in pauses),
        *(notice.notice_key for notice in notices),
    ]
    cursor = dv.restore_by_id(s, keys)
    # a frame that lists no row gives the cursor nothing to walk and Enter nothing to open
    s.nav_rows = len(keys)
    wide = REGISTRY.rail_at(s.route, view.columns) is not None
    col = w - RAIL_W - 1 if wide else w
    top = native_head(
        view,
        register,
        crumb_text=route_crumb(view, register, "Needs you"),
        summary=counts_line(register, principal=principal, complete=rd.complete)
        + cursor_note(register),
    )
    strip = bucket_items(register, notices=len(view.notices)) if not register.withheld else []
    s.bucket_keys = [x.key for x in strip] or None
    if not wide and strip:
        top.append(dv.strip_row(s, strip, w, lead=label("BUCKETS")))
    body: list[str] = []
    if register.withheld:
        body.append(label("ACTIONS", f"{UNKNOWN_WORD} · {UNWRITTEN_REASON}"))
    elif not keys:
        body.extend(_empty_lines(register, s.bucket))
    else:
        holders = len({row.assignee_ref for row in register.rows} - {None} | {principal} - {None})
        # a key column fits the longest key listed: a PERM- key is one wider than an ACT- one
        key_w = max([9, *(cell_len(item.key) + 1 for item in listed)])
        table = Table([key_w, max(30, col - 23 - key_w), 10, 0], 2)
        win = window_rows(view, total=len(keys), cursor=cursor, chrome=len(top) + 5)
        last: AttentionBucket | None = None
        for index in range(win.start, min(win.stop, len(listed))):
            item = listed[index]
            row = by_key[item.key]
            if item.bucket is not last:
                n = sum(1 for x in listed if x.bucket is item.bucket)
                # a ceiling breach and a budget notice are both over budget: one heading
                n += len(notices) if item.bucket is AttentionBucket.OVER_BUDGET else 0
                body.append(f" {item.bucket.value.upper()}  {group_n(n)}")
                last = item.bucket
            subject = row.facts.get("subject", UNKNOWN_WORD)
            question = row.facts.get("question") or row.title or row.urn
            due = due_cell(row, view.now or register.generated_at)
            cells = [row.key, f"{subject} {question}", value_cell(row.status).slot, due]
            body.append(table.row(cells, index == cursor))
            if index == cursor:
                body.extend(_selected_lines(row, item, principal, holders, view.sealable))
        body.extend(_pause_lines(view, pauses, table, first=len(listed), win=win, cursor=cursor))
        body.extend(
            _notice_lines(
                notices,
                table,
                first=len(listed) + len(pauses),
                win=win,
                cursor=cursor,
                headed=last is AttentionBucket.OVER_BUDGET,
            )
        )
        others = sum(1 for item in listed if audience_refusal(item.assignee_ref, principal))
        if others:
            held = "is" if others == 1 else "are"
            body.append(f"   {group_n(others)} {held} another principal's · {NOT_A_WORK_LIST}")
    if wide and strip:
        body = beside(body, rail_lines(register, s.bucket, notices=len(view.notices)), col, w)
    rows = [*top, *body]
    if register.withheld:
        rows += [
            thin(w),
            UNWRITTEN_ROW
            + " "
            + " · ".join(f"{name} {UNKNOWN_WORD}" for name in register.withheld),
        ]
    selected = _verb_row(listed, cursor, by_key)
    on_notice = cursor >= len(listed) + len(pauses) and bool(notices)
    return build(view, rows, route_keys_bar(view, _bar_keys(view, selected, on_notice=on_notice)))


#: The heading the held work is listed under.
PAUSED_HEADING = "PAUSED"

#: The pause states that still hold work.
_HOLDING: Final = frozenset({PauseStatus.OPEN, PauseStatus.HELD})


def held_pauses(view: View) -> list[PauseRecord]:
    """Return the pauses still holding work, from the records the overlays are bound to."""
    held = view.decisions.pauses if view.decisions is not None else ()
    return [pause for pause in held if pause.status in _HOLDING]


def _verb_row(
    listed: Sequence[AttentionItem], cursor: int, by_key: Mapping[str, ProjectionRow]
) -> ProjectionRow | None:
    """Return the row the caret is on when the Attention verbs may act on it.

    A ceiling breach is read-only: it is listed, but no verb is offered on it. A question
    is answered from its own detail, where its options are, and a stalled Run from the pause
    over it, so none is offered on either.
    """
    if cursor >= len(listed) or listed[cursor].read_only:
        return None
    row = by_key[listed[cursor].key]
    answered_elsewhere = (Epoch2Collection.OPEN_QUESTION, Epoch2Collection.RUN)
    return None if row.collection in answered_elsewhere else row


def _pause_lines(
    view: View,
    pauses: Sequence[PauseRecord],
    table: Table,
    *,
    first: int,
    win: RowWindow,
    cursor: int,
) -> list[str]:
    """Return the pauses the window shows, after the actions, under their own heading.

    Args:
        view: The render being built, whose records state each affected Run's state.
        pauses: The pauses listed after the actions.
        table: The table the actions were drawn in, so the columns line up.
        first: The offset of the first pause in the whole list.
        win: The window of the whole list the frame draws.
        cursor: The offset the caret sits on.
    """
    states = view.decisions.run_states if view.decisions is not None else {}
    lines: list[str] = []
    for index in range(max(win.start, first), min(win.stop, first + len(pauses))):
        pause = pauses[index - first]
        if index in (first, win.start):
            lines.append(f" {PAUSED_HEADING}  {group_n(len(pauses))}")
        situation = pause_situation(pause, states.get(pause.scope))
        cells = [pause.id, f"{pause.scope} {situation.name}", pause.status.value, NO_DEADLINE]
        lines.append(table.row(cells, index == cursor))
        if index == cursor:
            lines.append(_KIND_INDENT + f"pause · ends when {situation.ends}")
    return lines


def _notice_lines(
    notices: Sequence[BudgetThresholdNotice],
    table: Table,
    *,
    first: int,
    win: RowWindow,
    cursor: int,
    headed: bool,
) -> list[str]:
    """Return the notices the window shows, after the actions, under their bucket heading.

    Args:
        notices: The notices listed after the actions.
        table: The table the actions were drawn in, so the columns line up.
        first: The offset of the first notice in the whole list.
        win: The window of the whole list the frame draws.
        cursor: The offset the caret sits on.
        headed: Whether the window already drew the over-budget heading above them,
            over a ceiling breach, so the notices continue under it.
    """
    lines: list[str] = []
    for index in range(max(win.start, first), win.stop):
        notice = notices[index - first]
        if index in (first, win.start) and not headed:
            lines.append(f" {NOTICE_BUCKET}  {group_n(len(notices))}")
        lines.append(table.row(notice_cells(notice), index == cursor))
        if index == cursor:
            lines.append(_KIND_INDENT + notice_detail(notice))
    return lines


def _bar_keys(view: View, selected: ProjectionRow | None, *, on_notice: bool) -> list[KeyEntry]:
    """Return the keybar's keys: the verbs only on a row this principal may act on.

    A notice asks nothing, so on one only its own two verbs are offered.
    """
    s, principal = view.session, view.principal
    writable = principal is not None and can_mutate(s)
    offered = (
        writable and selected is not None and not audience_refusal(selected.assignee_ref, principal)
    )
    # an answer to a pending action is sealed under an evidence receipt, so without one
    # its answer keys would only refuse; a permission is decided without one
    unsealable = (
        selected is not None
        and selected.collection is Epoch2Collection.PENDING_ACTION
        and not view.sealable
    )
    verbs = set(_VERB_KEYS.values())
    # acknowledge is offered on the action menu alone, so only the row's own keys show
    row_keys = (_VERB_KEYS[key] for key in NOTICE_VERBS if key in _VERB_KEYS)
    notice_verbs = set(row_keys) if writable and on_notice else set()
    answers = {_VERB_KEYS["a"], _VERB_KEYS["x"]}
    return [
        key
        for key in native_keys(s.route, windowed=s.windowed)
        if key not in verbs
        or key in notice_verbs
        or (offered and not (unsealable and key in answers))
    ]


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
    wide = REGISTRY.rail_at("attention", view.columns) is not None
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
