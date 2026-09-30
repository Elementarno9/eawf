"""activity: the fleet table with the bucket strip at 80 columns and a bucket rail wider.

The window follows the cursor and always says what is off screen.

The native frame draws the same table from the Run register: each Run with the Task it
runs and its stored status, the buckets being the eight exception buckets the Run grouping
partitions the rows into -- ``needs operator``'s suspension reasons indented under it in
the rail and folded into it in the strip. A Run's reason is read off
its suspension, failure or purpose and the instant is when its record last moved; a Run
whose record states neither wears the unknown token rather than a blank. The rail is the route's
declared rail, so it folds into a strip exactly where the registry says it does. A running
Run the daemon's stall read says went quiet is drawn under ``lost or stale`` with when it
last produced anything, never counted as running.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from eawf.kernel.projection.activity import (
    STATUS_BUCKETS,
    ActivityExceptionBucket,
    ActivityGrouping,
    group_runs,
)
from eawf.kernel.projection.compute import ProjectionRow
from eawf.kernel.projection.liveness import HeldLiveness
from eawf.kernel.projection.registers import RegisterView
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.cells import NO_VALUE, value_cell
from eawf.surfaces.tui.console.fixture import FleetRow
from eawf.surfaces.tui.console.format import clock_minute, group, instant
from eawf.surfaces.tui.console.frame import (
    Fixed,
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
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS, keybar
from eawf.surfaces.tui.console.keymap import native_keys
from eawf.surfaces.tui.console.navigation import Ctx, busy
from eawf.surfaces.tui.console.reads import prototype_attached, reads
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.renderers.read_model import (
    UNKNOWN_WORD,
    counts,
    label,
    native_head,
    route_crumb,
)
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.tokens import TRUTH
from eawf.surfaces.tui.console.width import cell_len, clip_words, pad

RAIL_W = 29
#: The rail's count column, the count set against its right edge.
_COUNT_W = 4
_AS_OF_W = 6
_FOOTER = 2
_COL_HEAD = 1
#: What the filter row says its keys do: Esc and Enter answer only while it is typed into;
#: a kept filter stays until ``\\`` starts a new one, since Esc then leaves the route.
FILTER_TYPING = "Esc clears · Enter keeps"
FILTER_KEPT = "kept · \\ starts a new filter"
#: The native keybar while the filter field takes the typing.
FILTER_KEYS: tuple[tuple[str, str], ...] = (("type", "narrow"), ("Enter", "keep"), ("Esc", "clear"))


def _filter_hint(s: Session) -> str:
    """Return the filter row's key hint for the field as it stands."""
    return FILTER_TYPING if s.typing else FILTER_KEPT


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
            " FILTER    \\" + dv.filter_of(s) + ("▏" if s.typing else "") + f"   {_filter_hint(s)}"
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
        out.append(recede_rail(raw, w) if receded else raw)
    return out


def bucket_items(grouping: ActivityGrouping) -> list[dv.StripItem]:
    """Return ``all`` then the eight buckets; the strip folds the sub-buckets into theirs."""
    return [
        dv.StripItem(None, "all", grouping.total),
        *(
            dv.StripItem(c.bucket.value, c.label, value_cell(c.count).slot)
            for c in grouping.top_level()
        ),
    ]


def rail_lines(grouping: ActivityGrouping, bucket: str | None) -> list[str]:
    """Return the bucket rail: its head, then every bucket, the sub-buckets indented.

    The chosen bucket carries the caret, so the rail says which bucket the list shows
    even when that bucket is empty; its sub-buckets are part of it and carry none.
    """
    label_w = RAIL_W - 2 - _COUNT_W
    return [
        "BUCKETS",
        *(
            ("▸" if not c.sub and c.bucket.value == bucket else " ")
            + pad(("  " if c.sub else "") + c.label, label_w)
            + _count_cell(value_cell(c.count).slot)
            for c in grouping.counts
        ),
    ]


def _count_cell(slot: str) -> str:
    """Return a count set against the right edge of its column, so the digits line up."""
    return " " * max(0, _COUNT_W - cell_len(slot)) + slot


def rail_receded(grouping: ActivityGrouping, bucket: str | None) -> frozenset[int]:
    """Return the rail rows that recede: every bucket but the chosen one, once one is.

    A sub-bucket is part of its bucket, so it recedes with it; the head never does.
    """
    if bucket is None:
        return frozenset()
    return frozenset(i for i, c in enumerate(grouping.counts, start=1) if c.bucket.value != bucket)


def bucket_seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Cycle Tab over the buckets the native frame drew, ``all`` after the last.

    The prototype frame publishes no bucket keys, so its Tab stays the dispatcher's.
    """
    s = ctx.s
    keys = s.bucket_keys
    if key != "Tab" or keys is None or busy(s):
        return False
    at = keys.index(s.bucket) if s.bucket in keys else 0
    s.bucket = keys[(at + (-1 if shift else 1)) % len(keys)]
    s.sel, s.sel_id, s.scroll = 0, None, 0
    ctx.log("Tab", f"bucket → {s.bucket or 'all'}")
    return True


def beside(
    body: list[str],
    rail: list[str],
    col: int,
    w: int,
    *,
    receded: frozenset[int] = frozenset(),
) -> list[str]:
    """Return ``body`` set in ``col`` cells with ``rail`` drawn beside it past a rule.

    The rail rows whose offsets are in ``receded`` are painted receded, the body beside
    them untouched.
    """
    out: list[str] = []
    for i in range(max(len(body), len(rail))):
        line = pad(
            pad(body[i] if i < len(body) else "", col) + "│ " + (rail[i] if i < len(rail) else ""),
            w,
        )
        out.append(recede_rail(line, w) if i in receded else Fixed(line))
    return out


#: What a Run waits for, read as the reason Activity gives: each suspension names the fact
#: that clears it, so the reason is that fact in the operator's words.
_WAITING_FOR: Mapping[str, str] = MappingProxyType(
    {
        "AWAITING_OPERATOR_INPUT": "needs your answer",
        "AWAITING_HUMAN_REVIEW": "needs your review",
        "AWAITING_PERMISSION_GRANT": "needs permission",
        "AWAITING_DEPENDENCY": "waiting on other work",
        "AWAITING_LEASE": "waiting for a lease",
        "AWAITING_PROVIDER_CAPACITY": "waiting for provider capacity",
    }
)

#: What a Run in a state that is its own reason is doing, by the purpose it runs for.
_DOING: Mapping[str, str] = MappingProxyType(
    {
        "implement": "implementing",
        "integrate": "integrating",
        "repair": "repairing",
        "review": "checking",
        "audit": "checking",
        "research": "researching",
        "plan": "planning",
        "observe": "observing",
    }
)

#: The reason a Run's status states by itself, where no further fact is needed.
_STATUS_REASON: Mapping[str, str] = MappingProxyType(
    {"QUEUED": "waiting for a slot", "COMPLETED": "run finished", "CANCELLED": "cancelled"}
)


#: The next move an empty Activity route offers: nothing executes until a Task is claimed.
EMPTY_NEXT = "a Run starts when a Task is dispatched"


def run_reason(row: ProjectionRow, liveness: HeldLiveness | None = None) -> str:
    """Return why a Run stands where it does, read off the facts its record states.

    A suspended Run names what it waits for, a failed one its failure, a running one what
    it runs for, or -- when a stall stands over it -- that it went quiet and since when; a
    status that states no reason of its own wears the unknown token.
    """
    facts, status = row.facts, row.status.value
    stall = liveness.stall_of(row.key) if liveness is not None and status == "RUNNING" else None
    if stall is not None:
        return f"stalled · nothing since {clock_minute(stall.last_activity_at)}"
    waiting = _WAITING_FOR.get(row.suspension_reason or "")
    if status == "SUSPENDED" and waiting:
        return waiting
    if facts.get("failure"):
        return facts["failure"]
    if status == "RUNNING" and facts.get("purpose") in _DOING:
        return _DOING[facts["purpose"]]
    return _STATUS_REASON.get(status or "", UNKNOWN_WORD)


def as_of(row: ProjectionRow) -> str:
    """Return the minute a Run's record last moved, or the unknown slot when unstated.

    The column is a clock's width, so an unstated minute is the bare unknown token: the
    token keeps its form where its word would be cut to an ellipsis.
    """
    at = instant(row.facts.get("updated_at"))
    return clock_minute(at) if at is not None else TRUTH["unknown"].unicode


def task_cell(row: ProjectionRow, room: int | None = None) -> str:
    """Return the Task a Run runs: its key and title, or the no-value mark.

    Args:
        row: The Run's row; its parent key names the Task.
        room: The cells the column holds, when the text is being fitted to one. The
            title gives way at a word and the key never does, so a title with no room
            left is dropped rather than cut to an ellipsis beside the key.
    """
    if row.parent_key is None:
        return NO_VALUE
    title = row.facts.get("task_title")
    if not title:
        return row.parent_key
    if room is None:
        return f"{row.parent_key} {title}"
    left = room - cell_len(row.parent_key) - 1
    return f"{row.parent_key} {clip_words(title, left)}" if left > 1 else row.parent_key


def _bucket_of(row: ProjectionRow, stalled: frozenset[str]) -> str | None:
    """Return the exception bucket a Run lands in, by the grouping's own tables."""
    try:
        status = RunStatus(row.status.value or "")
    except ValueError:
        return None
    if status is RunStatus.RUNNING and row.key in stalled:
        return ActivityExceptionBucket.LOST_STALE.value
    return STATUS_BUCKETS[status].value


def _shown(view: View, register: RegisterView) -> list[ProjectionRow]:
    """Return the Runs the bucket and the filter leave, in the register's order."""
    s, liveness = view.session, view.liveness
    stalled = liveness.stalled_keys() if liveness is not None else frozenset()
    flt = dv.filter_of(s).lower()
    return [
        row
        for row in register.rows
        if not (s.bucket and _bucket_of(row, stalled) != s.bucket)
        and not (
            flt and flt not in f"{row.key} {task_cell(row)} {run_reason(row, liveness)}".lower()
        )
    ]


def empty_lines(view: View, register: RegisterView) -> list[str]:
    """Return what an empty Activity route says: a filter result, a gap, or real emptiness.

    Each names what it is, because the three answer different questions: a filter hid
    the Runs, a read could not vouch for them, or the scope really runs nothing at this
    revision -- and real emptiness offers the next move.
    """
    s = view.session
    revision = group(int(register.source_cursor))
    if register.rows and s.bucket and not dv.filter_of(s):
        return [
            f"   nothing in {s.bucket} · {len(register.rows)} runs are in other buckets",
        ]
    if register.rows and (s.bucket or dv.filter_of(s)):
        return [
            f"   nothing matches the filter · {len(register.rows)} runs hidden",
        ]
    if not reads(s).complete:
        return [
            f"   no Run is known at revision {revision} · this read cannot vouch for every Run",
            "   the count fills in once the link is whole again",
        ]
    return [
        f"   NOTHING RUNS  this scope holds no record of a Run at revision {revision}",
        f"   {EMPTY_NEXT}",
    ]


def native_frame(view: View, register: RegisterView) -> list[str]:
    """Return the Activity frame drawn from the Run register the daemon served.

    Args:
        view: The render being built.
        register: The Run register at the committed cursor.

    Returns:
        The full frame, keybar last.
    """
    s, w = view.session, view.w
    shown = _shown(view, register)
    cursor = dv.restore_by_id(s, [row.key for row in shown])
    wide = REGISTRY.rail_at(s.route, view.columns) is not None
    col = w - RAIL_W - 1 if wide else w
    liveness = view.liveness
    grouping = group_runs(register, liveness.stalled_keys() if liveness is not None else None)
    top = native_head(
        view, register, crumb_text=route_crumb(view, register, "Activity"), summary=counts(register)
    )
    if s.typing or dv.filter_of(s):
        # the keys that answer the field are the keybar's to promise, never the row's
        typed = "▏" if s.typing else ""
        top.append(label("FILTER", f"\\{dv.filter_of(s)}{typed}"))
    items = bucket_items(grouping)
    s.bucket_keys = [item.key for item in items]
    if not wide:
        top.append(dv.strip_row(s, items, w, lead=label("BUCKETS")))
    unstated = grouping.unbucketed
    if unstated:
        top.append(label("UNBUCKETED", f"{unstated} in no bucket · the row states no status"))
    # the column holds the longest Task key whole, since a key never gives way
    longest = max((cell_len(row.parent_key or "") for row in shown), default=0)
    task_w = max(14, longest + 1, (col - 16 - 12 - _AS_OF_W) // 2)
    cols = [16, task_w, 12, max(12, col - 16 - task_w - 12 - _AS_OF_W - 1)]
    table = Table([cols[0] - 3, cols[1], cols[2], cols[3], 0], 2)
    body: list[str] = [table.head(["RUN", "TASK", "STATE", "REASON", "AS OF"])]
    win = window_rows(view, total=len(shown), cursor=cursor, chrome=len(top) + 1 + _FOOTER)
    for index in range(win.start, win.stop):
        row = shown[index]
        cells = [
            row.key,
            pad(task_cell(row, cols[1] - 1), cols[1] - 1),
            value_cell(row.status).slot,
            pad(run_reason(row, liveness), cols[3] - 1),
            as_of(row),
        ]
        body.append(table.row(cells, index == cursor))
    if not shown:
        body.extend(empty_lines(view, register))
    if wide:
        receded = rail_receded(grouping, s.bucket)
        body = beside(body, rail_lines(grouping, s.bucket), col, w, receded=receded)
    matching = " matching" if (s.bucket or dv.filter_of(s)) else ""
    # a WINDOW row owes the operator only the rows it hides
    foot = [thin(w), win.line(complete=register.complete) + matching] if win.hides else []
    rows = [*top, *body, *foot]
    if s.typing:
        return build(view, rows, keybar(list(FILTER_KEYS), w))
    return build(view, rows, route_keys_bar(view, native_keys(s.route, windowed=s.windowed)))


def render(view: View) -> list[str]:
    """Return the Activity frame."""
    if view.register is not None:
        return native_frame(view, view.register)
    s, w = view.session, view.w
    rows_all = _rows(view)
    if s.sel >= len(rows_all):
        s.sel = max(0, len(rows_all) - 1)
    wide = REGISTRY.rail_at("activity", view.columns) is not None
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
