"""unattended: the dispatch queue the daemon owns.

``a`` and ``d`` ask the daemon through the consequence card, and Enter opens the focused
row's Run.
"""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.frame import Grid, View, chip, g_frame, thin
from eawf.surfaces.tui.console.navigation import Ctx, busy, go
from eawf.surfaces.tui.console.renderers.read_model import native, native_frame

_KEYS: tuple[tuple[str, str], ...] = (
    ("↑↓", "row"),
    ("Enter", "run"),
    ("a", "request pause"),
    ("d", "request drain"),
    ("Esc", "back"),
)
RUNS: tuple[str, ...] = tuple(row[0] for row in pt.QUEUE)


def run_under_cursor(sel: int) -> str:
    """Return the Run of the queue row under the cursor, the last row past the end."""
    return RUNS[min(sel, len(RUNS) - 1)]


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
