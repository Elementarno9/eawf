"""unattended: the dispatch queue the daemon owns.

``a`` and ``d`` ask the daemon through the consequence card, and Enter opens the focused
row's Run.

The native frame observes the queue rather than driving it: the Runs whose lifecycle has
not ended, under ``RUN``, ``TASK``, ``STATE`` and ``PROGRESS``, headed by the ``QUEUE`` line
the rows are counted into. Progress is a named numerator or the unknown token, never a
bare percentage, and a queued Run reads ``∅ not started``. A Run enumerates none of its
obligations, so an executing one reads ``opaque`` with its elapsed time against the budget
its capsule sealed; a running verification leg whose command publishes its collection reads
its completed count against its total, the last unit it finished and its running tallies.
Progress carries its freshness: a Run the daemon's stall read says went quiet reads
``stalled``, and once a read itself stops arriving what it stated reads ``stale``, so the
reader losing its source never looks like the work stopping. The concurrency plan and the
control come from the daemon's dispatch-queue read, derived there from the governor and the
dependency graph and stored nowhere.
"""

from __future__ import annotations

from datetime import datetime

from eawf.kernel.projection.liveness import STALLED, freshness_of, run_liveness
from eawf.kernel.projection.operations import DISPATCH_QUEUE_PRODUCER
from eawf.kernel.projection.route_view import RouteReadModel, RouteRecord
from eawf.kernel.projection.truth import Freshness, TruthState
from eawf.kernel.runtime.dispatch_queue import DispatchQueueView, ProgressMode, VerificationLeg
from eawf.kernel.state.epoch2.transitions import TERMINAL_STATUSES, LifecycleEntity
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.cells import NO_VALUE, value_cell
from eawf.surfaces.tui.console.format import clock_minute, span
from eawf.surfaces.tui.console.frame import Grid, View, chip, g_frame, thin, window_rows
from eawf.surfaces.tui.console.keybar import route_pairs
from eawf.surfaces.tui.console.live_reads import held_queue
from eawf.surfaces.tui.console.navigation import Ctx, busy, go
from eawf.surfaces.tui.console.operations import DISPATCH_QUEUE_TARGET
from eawf.surfaces.tui.console.reads import write_refusal
from eawf.surfaces.tui.console.renderers.read_model import (
    UNKNOWN_WORD,
    finish,
    label,
    more,
    native,
    native_head,
    route_crumb,
)
from eawf.surfaces.tui.console.width import cell_len

_KEYS = route_pairs("unattended")
RUNS: tuple[str, ...] = tuple(row[0] for row in pt.QUEUE)


def run_under_cursor(sel: int) -> str:
    """Return the Run of the queue row under the cursor, the last row past the end."""
    return RUNS[min(sel, len(RUNS) - 1)]


#: The stored status a Run waiting for a slot states.
QUEUED = "QUEUED"

#: The stored status a Run holding a slot states.
RUNNING = "RUNNING"

#: What a queued Run's progress reads: it has none, and that is not an unknown.
NOT_STARTED = "∅ not started"


def queue_of(model: RouteReadModel) -> list[RouteRecord]:
    """Return the Runs the queue holds: every Run whose lifecycle has not ended."""
    ended = TERMINAL_STATUSES[LifecycleEntity.RUN]
    return [
        row
        for row in model.rows
        if not (
            row.field("status").state is TruthState.KNOWN and row.field("status").value in ended
        )
    ]


def _state(row: RouteRecord) -> str:
    """Return the stored status a queue row states, as its truth cell."""
    return value_cell(row.field("status")).slot


def _elapsed(since: datetime, view: View, queue: DispatchQueueView) -> str:
    """Return how long ago ``since`` was, on the console clock, against the read's instant."""
    return span(max(0, int(((view.now or queue.read_at) - since).total_seconds())))


def _against(budget: int | None) -> str:
    """Return the budget an elapsed time is read against, or say none was sealed."""
    return f" of {span(budget)}" if budget is not None else " · no budget sealed"


def _progress(row: RouteRecord, view: View, queue: DispatchQueueView | None) -> str:
    """Return a queue row's progress: not started, stale, stalled, opaque, or unknown."""
    if _state(row) == QUEUED:
        return NOT_STARTED
    held = view.liveness
    if _state(row) == RUNNING and held is not None:
        field = run_liveness(row.key, held, now=view.now or held.read_at)
        if field.freshness is Freshness.STALE:
            return f"stale · liveness last read {clock_minute(held.read_at)}"
        if field.value == STALLED and field.occurred_at is not None:
            return f"stalled · nothing since {clock_minute(field.occurred_at)}"
    entry = queue.run(row.key) if queue is not None else None
    if queue is None or entry is None or entry.started_at is None:
        return value_cell(row.field("progress")).slot + " unknown"
    if freshness_of(queue.read_at, view.now or queue.read_at) is Freshness.STALE:
        return f"stale · queue last read {clock_minute(queue.read_at)}"
    return f"opaque · {_elapsed(entry.started_at, view, queue)}{_against(entry.budget_seconds)}"


def _leg(leg: VerificationLeg, view: View, queue: DispatchQueueView) -> list[str]:
    """Return one running leg: its count against its total or opaque, then its tallies.

    The count and the time lead, so a narrow frame clips the last unit's name, never them.
    """
    if freshness_of(leg.heartbeat_at, view.now or queue.read_at) is Freshness.STALE:
        return [f"gate {leg.gate_id} · stale · last heartbeat {clock_minute(leg.heartbeat_at)}"]
    timed = f"{_elapsed(leg.started_at, view, queue)}{_against(leg.budget_seconds)}"
    if leg.progress_mode is ProgressMode.OPAQUE:
        return [f"gate {leg.gate_id} · opaque · {timed}"]
    counted = (
        f"{leg.completed} of {leg.total} obligations"
        if leg.total is not None
        else f"{leg.completed} obligations so far"
    )
    last = f" · last {leg.last_completed}" if leg.last_completed else ""
    return [
        f"gate {leg.gate_id} · {counted} · {timed}",
        f"{leg.passed} pass · {leg.failed} fail · {leg.unknown} unknown{last}",
    ]


def _plan(queue: DispatchQueueView | None) -> list[str]:
    """Return the ``PLAN`` readout: the concurrency and every forced sequential edge."""
    if queue is None:
        return [
            label("PLAN", f"Concurrency {UNKNOWN_WORD} — derived from the dependency graph"),
            more(f"no edge is stated · waiting on {DISPATCH_QUEUE_PRODUCER}"),
        ]
    slots = queue.plan.slots if queue.plan.slots is not None else UNKNOWN_WORD
    edges = [f"{edge.waits} waits on {edge.on} · forced sequential" for edge in queue.plan.edges]
    return [
        label(
            "PLAN",
            f"Concurrency {queue.plan.in_use} of {slots} — derived from the dependency graph",
        ),
        *(more(text) for text in edges or ["∅ no forced sequential edge"]),
    ]


def _control(queue: DispatchQueueView | None) -> list[str]:
    """Return the ``CONTROL`` block: every verb is a request, and the last one's outcome."""
    last = queue.control.last_request if queue is not None else None
    held = queue.control.holding if queue is not None else None
    said = (
        f"{UNKNOWN_WORD} · waiting on {DISPATCH_QUEUE_PRODUCER}"
        if queue is None
        else "∅ no dispatch request is recorded"
    )
    if last is not None:
        why = f" · {last.reason}" if last.reason else ""
        said = (
            f"the daemon {last.outcome.value} request {last.verb.value} by {last.actor} "
            f"at {clock_minute(last.requested_at)}{why}"
        )
    return [
        label("CONTROL", "This surface observes — every verb is a daemon request."),
        more(said),
        *([more(f"dispatch is held by {held.value} · claimed runs go on")] if held else []),
    ]


def _verify(view: View, queue: DispatchQueueView | None) -> list[str]:
    """Return the running verification legs, or nothing while none runs."""
    if queue is None or not queue.legs:
        return []
    legs = [text for leg in queue.legs for text in _leg(leg, view, queue)]
    return [thin(view.w), label("VERIFY", legs[0]), *(more(text) for text in legs[1:])]


def native_frame(view: View, model: RouteReadModel) -> list[str]:
    """Return the Unattended frame drawn from the read model the daemon served.

    Args:
        view: The render being built.
        model: The route's read model at the committed cursor: the scope's Runs.

    Returns:
        The full frame, keybar last.
    """
    s, w = view.session, view.w
    queue = queue_of(model)
    held = held_queue(view.live)
    cursor = dv.sel_by_id(s, [row.key for row in queue]) if queue else dv.sel_in(s, 0)
    s.sel_id = queue[cursor].key if queue else None
    top = native_head(
        view,
        model,
        crumb_text=route_crumb(view, model, "Unattended"),
        summary=(
            f"Dispatch queue · the daemon owns scheduling · {dv.plural(len(queue), 'run')} in it"
        ),
    )
    # the Task column holds the longest Task key whole, since a key never gives way
    longest = max((cell_len(row.parent_key or "") for row in queue), default=0)
    grid = Grid([17, max(14, longest + 2), 13, 0])
    queued = sum(1 for row in queue if _state(row) == QUEUED)
    running = sum(1 for row in queue if _state(row) == RUNNING)
    forced = str(held.forced_sequential()) if held is not None else "?"
    body = [
        label("QUEUE", f"{queued} queued · {running} running · {forced} forced sequential"),
        grid.head(["RUN", "TASK", "STATE", "PROGRESS"]),
    ]
    foot = [
        *_verify(view, held),
        thin(w),
        *_plan(held),
        thin(w),
        *_control(held),
    ]
    win = window_rows(
        view, total=len(queue), cursor=cursor, chrome=len(top) + len(body) + 1 + len(foot)
    )
    body.extend(
        grid.row(
            [row.key, row.parent_key or NO_VALUE, _state(row), _progress(row, view, held)],
            i == cursor,
            w,
        )
        for i, row in enumerate(queue[win.start : win.stop], start=win.start)
    )
    if not queue:
        body.append("   ∅ the queue holds no record: no Run whose lifecycle has not ended")
    body.append(label("WINDOW", win.count(complete=model.complete)))
    return finish(view, top, body, _KEYS, foot=foot)


def render(view: View) -> list[str]:
    """Return the Unattended frame, native when a read model is held."""
    model = native(view)
    if model is not None:
        return native_frame(view, model)
    s, w = view.session, view.w
    not_started = "∅ not started"
    queue = [
        [run, task, chip(tone, state), progress or not_started]
        for run, task, tone, state, progress in pt.QUEUE
    ]
    dv.sel_in(s, len(queue))
    grid = Grid([17, 12, 11, 0])
    body = [
        " QUEUE        9 queued · 4 running · 2 forced sequential",
        grid.head(["RUN", "TASK", "STATE", "PROGRESS"]),
    ]
    body.extend(grid.row(x, i == s.sel, w) for i, x in enumerate(queue))
    body.extend(
        [
            thin(w),
            " PLAN         Concurrency 4 of 6 — derived from the dependency graph.",
            "              EAWF-0051 waits on EAWF-0042 · forced sequential",
            thin(w),
            " CONTROL      This surface observes — every verb is a daemon request.",
            f"              the daemon accepted request pause on {pt.QUEUE_TARGET} at 13:58",
        ]
    )
    return g_frame(
        view,
        crumb=f"Eä ▸ {view.fixture.scope} ▸ Unattended",
        ctx="Dispatch queue · the daemon owns scheduling · 14:02",
        body=body,
        keys=_KEYS,
    )


def _pause_target(ctx: Ctx) -> dict[str, str | None]:
    """Return what ``a`` previews: a pause, or a resume once dispatch is held."""
    if ctx.fixture.prototype:
        return {
            "verb": "request pause",
            "state": None,
            "id": pt.QUEUE_TARGET,
            "kind": "dispatch queue",
            "effects": "the daemon is asked to pause the queue at its next safe point",
            "not": "it does not stop a run that is already claimed",
        }
    queue = held_queue(ctx.live)
    if queue is not None and queue.control.holding is not None:
        return {
            "verb": "request resume",
            "state": None,
            "id": DISPATCH_QUEUE_TARGET,
            "kind": "dispatch queue",
            "effects": "the daemon is asked to admit queued runs again",
            "not": "it does not start a run the governor would not admit",
        }
    return {
        "verb": "request pause",
        "state": None,
        "id": DISPATCH_QUEUE_TARGET,
        "kind": "dispatch queue",
        "effects": "the daemon admits no new run until a resume",
        "not": "it does not stop a run that is already claimed",
    }


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Preview a pause or resume request, or open the Run under the cursor.

    Drain is a menu verb. Where no write can leave, such as offline or under a snapshot
    the operator has not accepted, the request is refused before any card opens.
    """
    s = ctx.s
    if busy(s):
        return False
    if key == "a":
        target = _pause_target(ctx)
        verb = str(target["verb"])
        refusal = "" if ctx.fixture.prototype else write_refusal(s, ctx.fixture, verb=verb)
        if refusal:
            ctx.log(key, f"{verb} refused · {refusal}")
            return True
        s.c_target = target
        s.overlay = "consequence"
        ctx.log(key, f"{verb} → consequence preview")
        return True
    if key == "Enter":
        go(ctx, "run.detail", "the run this dispatch row is about", run_under_cursor(s.sel))
        return True
    return False
