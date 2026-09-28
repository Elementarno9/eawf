"""unattended: the dispatch queue the daemon owns.

``a`` and ``d`` ask the daemon through the consequence card, and Enter opens the focused
row's Run.

The native frame observes the queue rather than driving it: the Runs whose lifecycle has
not ended, under ``RUN``, ``TASK``, ``STATE`` and ``PROGRESS``, headed by the ``QUEUE`` line
the rows are counted into. Progress is a named numerator or the unknown token, never a
bare percentage, and a queued Run reads ``∅ not started``. The concurrency plan derives
from the dependency graph at render time; the dispatch-queue projection that states it
has not shipped, so the ``PLAN`` readout names that producer instead of a number.
"""

from __future__ import annotations

from eawf.kernel.projection.operations import DISPATCH_QUEUE_PRODUCER
from eawf.kernel.projection.route_view import RouteReadModel, RouteRecord
from eawf.kernel.projection.truth import TruthState
from eawf.kernel.state.epoch2.transitions import TERMINAL_STATUSES, LifecycleEntity
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.cells import NO_VALUE, value_cell
from eawf.surfaces.tui.console.frame import Grid, View, chip, g_frame, thin, window_rows
from eawf.surfaces.tui.console.keybar import route_pairs
from eawf.surfaces.tui.console.navigation import Ctx, busy, go
from eawf.surfaces.tui.console.renderers.read_model import (
    UNKNOWN_WORD,
    counts,
    finish,
    label,
    more,
    native,
    native_head,
    route_crumb,
)

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


def _progress(row: RouteRecord) -> str:
    """Return a queue row's progress: not started for a queued Run, else the producer's cell."""
    if _state(row) == QUEUED:
        return NOT_STARTED
    return value_cell(row.field("progress")).slot + " unknown"


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
    cursor = dv.sel_by_id(s, [row.key for row in queue]) if queue else dv.sel_in(s, 0)
    s.sel_id = queue[cursor].key if queue else None
    top = native_head(
        view,
        model,
        crumb_text=route_crumb(model, "Unattended"),
        summary=f"Dispatch queue · the daemon owns scheduling · {counts(model)}",
    )
    queued = sum(1 for row in queue if _state(row) == QUEUED)
    running = sum(1 for row in queue if _state(row) == RUNNING)
    body = [
        label("QUEUE", f"{queued} queued · {running} running · ? forced sequential"),
        Grid([17, 14, 13, 0]).head(["RUN", "TASK", "STATE", "PROGRESS"]),
    ]
    foot = [
        thin(w),
        label("PLAN", f"Concurrency {UNKNOWN_WORD} — derived from the dependency graph"),
        more(f"no edge is stated · waiting on {DISPATCH_QUEUE_PRODUCER}"),
        thin(w),
        label("CONTROL", "This surface observes — every verb is a daemon request."),
        more("∅ no request has been sent from this console"),
    ]
    grid = Grid([17, 14, 13, 0])
    win = window_rows(
        view, total=len(queue), cursor=cursor, chrome=len(top) + len(body) + 1 + len(foot)
    )
    body.extend(
        grid.row([row.key, row.parent_key or NO_VALUE, _state(row), _progress(row)], i == cursor, w)
        for i, row in enumerate(queue[win.start : win.stop], start=win.start)
    )
    if not queue:
        body.append("   ∅ the queue holds no record: no Run whose lifecycle has not ended")
    body.append(win.line(complete=model.complete))
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


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Preview a pause or drain request, or open the Run under the cursor."""
    s = ctx.s
    if busy(s):
        return False
    if key in ("a", "d"):
        pause = key == "a"
        s.c_target = {
            "verb": "request pause" if pause else "request drain",
            "state": None,
            "id": pt.QUEUE_TARGET,
            "kind": "dispatch queue",
            "effects": "the daemon is asked to pause the queue at its next safe point"
            if pause
            else "the daemon is asked to stop claiming new work and finish what it holds",
            "not": "it does not stop a run that is already claimed"
            if pause
            else "it does not cancel anything already running",
        }
        s.overlay = "consequence"
        ctx.log(key, f"{s.c_target['verb']} → consequence preview")
        return True
    if key == "Enter":
        go(ctx, "run.detail", "the run this dispatch row is about", run_under_cursor(s.sel))
        return True
    return False
