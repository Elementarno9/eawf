"""sandbox.log: authorisation decisions, the focused one explained at the foot.

Enter opens the Run a decision was about, and ``p`` the policy it was read against.
"""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.frame import Grid, View, chip, g_frame, g_pad, thin
from eawf.surfaces.tui.console.navigation import Ctx, busy, go
from eawf.surfaces.tui.console.renderers.read_model import native, native_frame

_KEYS: tuple[tuple[str, str], ...] = (
    ("↑↓", "row"),
    ("Enter", "run"),
    ("\\", "filter"),
    ("p", "policy"),
    ("Esc", "back"),
)
RUNS: tuple[str, ...] = pt.SANDBOX_RUNS


def _decisions() -> list[list[str]]:
    return [
        [
            "14:02",
            chip("er", "denied"),
            RUNS[0],
            "write outside root",
            "fs.write.scope = project root",
        ],
        ["14:01", chip("ok", "allowed"), RUNS[1], "read repository", "fs.read.scope = repository"],
        [
            "14:00",
            chip("ok", "allowed"),
            RUNS[2],
            "network egress pinned",
            "net.egress = pinned hosts",
        ],
        [
            "13:59",
            chip("er", "denied"),
            RUNS[3],
            "network egress open",
            "net.egress = pinned hosts",
        ],
    ]


def run_under_cursor(sel: int) -> str:
    """Return the Run of the decision under the cursor, the last row past the end."""
    return RUNS[min(sel, len(RUNS) - 1)]


def render(view: View) -> list[str]:
    """Return the Sandbox log frame, native when a read model is held."""
    model = native(view)
    if model is not None:
        return native_frame(view, model)
    s, w, h = view.session, view.w, view.h
    decisions = _decisions()
    dv.sel_in(s, len(decisions))
    grid = Grid([7, 10, 17, 0], 1)
    body = [
        " WINDOW       142 decisions · 6 denied · 4 of 142 shown",
        grid.head(["TIME", "DECISION", "RUN", "REASON"]),
    ]
    body.extend(grid.row(x[:4], i == s.sel, w) for i, x in enumerate(decisions))
    cur = decisions[s.sel]
    foot = [
        thin(w),
        f" DECISION     {cur[1]} · {cur[3]}",
        f"              {cur[2]} · rule {cur[4]}",
        "              pol-2026-08-11.3 · the revision makes it explicable",
    ]
    body.extend(g_pad("", w) for _ in range(h - 4 - len(foot) - len(body)))
    body.extend(foot)
    return g_frame(
        view,
        crumb=f"Eä ▸ {view.fixture.scope} ▸ Sandbox log",
        ctx=f"Authorisation decisions for every agent in {view.fixture.scope}",
        body=body,
        keys=_KEYS,
    )


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Open the policy on ``p`` and the decision's Run on Enter."""
    if busy(ctx.s):
        return False
    if key == "p":
        ctx.notify("settings ▸ sandbox · pol-2026-08-11.3", "opened")
        go(ctx, "settings", "the policy these decisions were read against")
        return True
    if key == "Enter":
        go(ctx, "run.detail", "the run this decision was about", run_under_cursor(ctx.s.sel))
        return True
    return False
