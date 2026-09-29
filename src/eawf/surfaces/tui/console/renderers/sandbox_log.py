"""sandbox.log: authorisation decisions, the focused one explained at the foot.

Enter opens the Run a decision was about, and ``p`` the policy it was read against.

The native frame draws the decision window the packet names -- ``TIME``, ``DECISION``,
``RUN``, ``REASON``, and the policy revision at 120 and wider -- over the sandbox-decision
record. That record has no producer yet, so the window states its counts unknown and
names the producer it waits on rather than claiming an empty log; it never claims
completeness it cannot vouch for. The policies the decisions would be read against are
the rows the read model does hold, each cited in the packet's form, ``sandbox policy ·
rev N``.
"""

from __future__ import annotations

from eawf.kernel.projection.operations import SANDBOX_DECISION_PRODUCER
from eawf.kernel.projection.route_view import RouteReadModel
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.frame import Grid, View, chip, g_frame, g_pad, thin, window_rows
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
    restore,
    route_crumb,
)
from eawf.surfaces.tui.console.tokens import Severity

_KEYS = route_pairs("sandbox.log")
#: Why ``p`` opens no settings section on a live tree.
NO_POLICY_SECTION = "no settings section holds the sandbox policy"
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


def policy_reference(revision: int) -> str:
    """Return a policy revision in the packet's citation form."""
    return f"sandbox policy · rev {revision}"


def native_frame(view: View, model: RouteReadModel) -> list[str]:
    """Return the Sandbox log frame drawn from the read model the daemon served.

    Args:
        view: The render being built.
        model: The route's read model at the committed cursor: the sandbox policies.

    Returns:
        The full frame, keybar last.
    """
    s, w = view.session, view.w
    wide = view.wide
    cursor = restore(s, model)
    top = native_head(
        view,
        model,
        crumb_text=route_crumb(view, model, "Sandbox log"),
        summary=f"Authorisation decisions for every agent · {counts(model)}",
    )
    # the head sits over the rows below it, in the same two-cell caret gutter
    decisions = Grid([7, 10, 17, 30, 0] if wide else [7, 10, 17, 0])
    body = [
        label("WINDOW", f"{UNKNOWN_WORD} decisions · ? denied · 0 of ? shown"),
        decisions.head(["TIME", "DECISION", "RUN", "REASON", *(["REVISION"] if wide else [])]),
        f"   no decision record is held · waiting on {SANDBOX_DECISION_PRODUCER}",
        thin(w),
        label(
            "POLICIES", f"{dv.plural(len(model.rows), 'policy', 'ies')} the decisions read against"
        ),
    ]
    policies = Grid([24, 14, 0])
    win = window_rows(view, total=len(model.rows), cursor=cursor, chrome=len(top) + len(body) + 5)
    body.extend(
        policies.row(
            [row.key, value_cell(row.field("status")).slot, policy_reference(row.revision)],
            i == cursor,
            w,
        )
        for i, row in enumerate(model.rows[win.start : win.stop], start=win.start)
    )
    if not model.rows:
        body.append("   ∅ no sandbox policy is held for this scope")
    focused = model.rows[cursor] if model.rows else None
    cited = policy_reference(focused.revision) if focused else "∅ unavailable"
    foot = [
        thin(w),
        label("DECISION", "∅ no decision is focused · the decision record has no producer yet"),
        more(f"the policy in force · {cited}"),
    ]
    return finish(view, top, body, _KEYS, foot=foot)


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


def _no_policy_section(ctx: Ctx) -> None:
    """Name the policy under the caret and say why ``p`` opens no settings section for it.

    The sandbox policy is a record of its own; no settings section holds it, so opening
    Settings would land on a section that says nothing about the policy.
    """
    model = ctx.projection if isinstance(ctx.projection, RouteReadModel) else None
    rows = model.rows if model is not None else ()
    row = next((r for r in rows if r.key == ctx.s.sel_id), rows[0] if rows else None)
    held = f"{row.key} · {policy_reference(row.revision)}" if row else "no sandbox policy is held"
    note = f"{held} · {NO_POLICY_SECTION}"
    ctx.notify(note, "policy", Severity.WARN)
    ctx.log("p", note)


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Open the policy on ``p`` and the decision's Run on Enter."""
    if busy(ctx.s):
        return False
    if key == "p" and (isinstance(ctx.projection, RouteReadModel) or not ctx.fixture.prototype):
        _no_policy_section(ctx)
        return True
    if key == "p":
        ctx.notify("settings ▸ sandbox · pol-2026-08-11.3", "opened")
        go(ctx, "settings", "the policy these decisions were read against")
        return True
    if key == "Enter":
        go(ctx, "run.detail", "the run this decision was about", run_under_cursor(ctx.s.sel))
        return True
    return False
