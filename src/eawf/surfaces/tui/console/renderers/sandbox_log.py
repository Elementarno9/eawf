"""sandbox.log: authorisation decisions, the focused one explained at the foot.

Enter opens the Run a decision was about, and ``p`` the policy it was read against.

The native frame draws the decision window the packet names -- ``TIME``, ``DECISION``,
``RUN``, ``REASON``, and the policy revision at 120 and wider -- over the sandbox-decision
records the gateway files for every call it guards, allowed as well as denied. The
``WINDOW`` line counts them once, and a read that cannot claim every row says so rather
than claiming completeness. A decision whose policy revision cannot be read renders
unavailable in place of its outcome, because an allowance nobody can explain is not one.
The call's raw target never reaches a row; the readout names the rule that decided with
its value in force and the policy in the packet's form, ``sandbox policy · rev N``. The
policies the document holds are listed under the window.
"""

from __future__ import annotations

from eawf.kernel.projection.route_view import RouteReadModel, RouteRecord
from eawf.kernel.projection.truth import TruthState
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.frame import Grid, View, chip, g_frame, g_pad, thin, window_rows
from eawf.surfaces.tui.console.keybar import route_pairs
from eawf.surfaces.tui.console.navigation import Ctx, busy, go
from eawf.surfaces.tui.console.renderers.read_model import (
    UNAVAILABLE,
    counts,
    finish,
    label,
    more,
    native,
    native_head,
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


def decisions_of(model: RouteReadModel) -> tuple[RouteRecord, ...]:
    """Return the decision rows of the read model, in the order the gateway filed them."""
    return tuple(row for row in model.rows if row.collection is Epoch2Collection.RECEIPT)


def _revision(row: RouteRecord) -> str:
    """Return the policy a decision cites, or unavailable when its revision cannot be read."""
    revision = row.field("policy_revision")
    if revision.state is not TruthState.KNOWN or revision.value is None:
        return UNAVAILABLE
    return policy_reference(int(revision.value))


def _decision_cells(row: RouteRecord, wide: bool) -> list[str]:
    """Return one decision row: its time, its outcome, its Run, its reason and its revision.

    An outcome whose policy revision cannot be read is drawn unavailable: an allowance
    read against no policy anyone can name is not one.
    """
    cited = _revision(row)
    outcome = row.field("decision").value or ""
    drawn = (
        UNAVAILABLE
        if cited == UNAVAILABLE
        else chip("er" if outcome == "denied" else "ok", outcome)
    )
    at = row.field("decided_at").value or ""
    return [
        at[11:16] or UNAVAILABLE,
        drawn,
        value_cell(row.field("run")).slot,
        value_cell(row.field("reason")).slot,
        *([cited] if wide else []),
    ]


def _readout(focused: RouteRecord | None, policies: tuple[RouteRecord, ...]) -> list[str]:
    """Return the ``DECISION`` readout: the Run, the rule with its value, and the policy.

    With no decision to focus, the readout names the policy a decision would be read
    against instead, so the foot still says what is in force.
    """
    if focused is None:
        cited = policy_reference(policies[0].revision) if policies else UNAVAILABLE
        return [
            label("DECISION", "∅ no decision is focused · none is recorded for this scope"),
            more(f"the policy in force · {cited}"),
        ]
    rule, held = focused.field("rule").value, focused.field("rule_value").value
    return [
        label("DECISION", f"{focused.field('decision').value} · {focused.field('run').value}"),
        more(f"rule {rule} · {held} · {_revision(focused)}"),
    ]


def native_frame(view: View, model: RouteReadModel) -> list[str]:
    """Return the Sandbox log frame drawn from the read model the daemon served.

    Args:
        view: The render being built.
        model: The route's read model at the committed cursor: the decisions and the
            sandbox policies.

    Returns:
        The full frame, keybar last.
    """
    s, w = view.session, view.w
    wide = view.wide
    decisions = decisions_of(model)
    policies = tuple(row for row in model.rows if row.collection is Epoch2Collection.SANDBOX_POLICY)
    cursor = dv.restore_by_id(s, [row.key for row in decisions])
    top = native_head(
        view,
        model,
        crumb_text=route_crumb(view, model, "Sandbox log"),
        summary=f"Authorisation decisions for every agent · {counts(model)}",
    )
    # the head sits over the rows below it, in the same two-cell caret gutter
    grid = Grid([7, 13, 17, 40, 0] if wide else [7, 13, 17, 0])
    noun = "policy" if len(policies) == 1 else "policies"
    tail = 4 + len(policies) + (0 if policies else 1)
    win = window_rows(view, total=len(decisions), cursor=cursor, chrome=len(top) + 2 + tail + 3)
    denied = sum(1 for row in decisions if row.field("decision").value == "denied")
    held = len(decisions)
    shown = f"{group(win.stop - win.start)} of {group(held)} shown"
    claim = "" if model.complete else " · not every decision was read"
    body = [
        label("WINDOW", f"{group(held)} decisions · {group(denied)} denied · {shown}{claim}"),
        grid.head(["TIME", "DECISION", "RUN", "REASON", *(["REVISION"] if wide else [])]),
    ]
    body.extend(
        grid.row(_decision_cells(decisions[i], wide), i == cursor, w)
        for i in range(win.start, win.stop)
    )
    if not decisions:
        body.append("   no decision is recorded for this scope")
    body.extend([thin(w), label("POLICIES", f"{group(len(policies))} {noun} the document holds")])
    listed = Grid([24, 14, 0])
    body.extend(
        listed.row(
            [row.key, value_cell(row.field("status")).slot, policy_reference(row.revision)],
            False,
            w,
        )
        for row in policies
    )
    if not policies:
        body.append("   ∅ no sandbox policy is held for this scope")
    focused = decisions[cursor] if decisions else None
    return finish(view, top, body, _KEYS, foot=[thin(w), *_readout(focused, policies)])


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
    decision = next(
        (r for r in rows if r.key == ctx.s.sel_id and r.collection is Epoch2Collection.RECEIPT),
        None,
    )
    policies = [r for r in rows if r.collection is Epoch2Collection.SANDBOX_POLICY]
    if decision is not None:
        held = f"{decision.key} · {_revision(decision)}"
    elif policies:
        held = f"{policies[0].key} · {policy_reference(policies[0].revision)}"
    else:
        held = "no sandbox policy is held"
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
    if key == "Enter" and isinstance(ctx.projection, RouteReadModel):
        return _open_run(ctx, ctx.projection)
    if key == "Enter":
        go(ctx, "run.detail", "the run this decision was about", run_under_cursor(ctx.s.sel))
        return True
    return False


def _open_run(ctx: Ctx, model: RouteReadModel) -> bool:
    """Open the Run the focused decision names; claim nothing when no decision is focused."""
    focused = next((r for r in decisions_of(model) if r.key == ctx.s.sel_id), None)
    run = focused.field("run").value if focused is not None else None
    if run is None:
        return False
    go(ctx, "run.detail", "the run this decision was about", run)
    return True
