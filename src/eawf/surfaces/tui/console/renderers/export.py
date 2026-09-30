"""export: the parts a plain-text report of a Run carries and what each costs.

Nothing leaves the machine.

When the console holds the export route's read model the card states the plan
``eawf run report`` writes: the Run report's parts in its order, each included as that
command includes it, a size it counts off the Run's event lines left unstated rather than
guessed, and the digest the view was read at. Enter takes it exactly as that command does:
the report of the Run the card is about is written under the tree's ``.ea/local/`` and the
rack answers with its path and the purged ranges it marks. No record moves, so the card
promises an artifact rather than a state change.
"""

from __future__ import annotations

from eawf.observability.reflect.run_report import (
    DEFAULT_PARTS,
    ReportPartName,
    plan_run_report,
    write_run_report,
)
from eawf.observability.reflect.runs import read_tree_run
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.frame import CHIP_END, LABEL_MARK, View, boxed, g_pad, scope_label
from eawf.surfaces.tui.console.keybar import route_pairs
from eawf.surfaces.tui.console.navigation import Ctx, busy
from eawf.surfaces.tui.console.renderers.read_model import native
from eawf.surfaces.tui.console.tokens import Severity
from eawf.workflow.projection.acceptance import RunReportPlanView

#: What Enter says when the console holds no read model to report. A prototype register
#: is not a Run, so there is nothing here an export could be taken of.
NOTHING_TO_REPORT = "no read model is held, so there is nothing to report"
#: What Enter says when the link names no tree for the report to land in.
NO_TREE = "the console names no tree to write the report into · nothing was written"
#: The head line of a written report that lists the purged ranges it marks.
_PURGED_HEAD = "purged ranges:"

_PARTS: tuple[tuple[str, str, str, str], ...] = pt.EXPORT_PARTS
_KEYS = route_pairs("export")


def native_card(view: View, model: RunReportPlanView) -> list[str]:
    """Return the export card, planned from the read model the daemon served.

    Args:
        view: The render being built; its session carries the selected part.
        model: The export route's read model at the committed cursor.

    Returns:
        The full card, keybar last.
    """
    session = view.session
    dv.sel_in(session, len(model.parts))
    lines = [f"{LABEL_MARK}PART                INCLUDED   SIZE{CHIP_END}", ""]
    lines.extend(
        ("▸" if index == session.sel else " ")
        + g_pad(part.name, 19)
        + g_pad(_included(part.name, included=part.included), 11)
        + part.size
        for index, part in enumerate(model.parts)
    )
    lines.extend(["", f"Plain text at digest {model.digest} — nothing leaves the machine."])
    command = (
        f"eawf run report {session.subj_id} writes it"
        if session.subj_id
        else "no Run is open, so no report command is named"
    )
    return boxed(
        view,
        crumb=f"Eä ▸ {scope_label(view, model.scope_id)} ▸ Export",
        ctx=f"{model.route} · cursor {model.source_cursor}",
        pre=[],
        title="EXPORT · report this Run",
        lines=lines,
        foot=f"{model.parts[session.sel].why}.  {command}; no record moves.",
        keys=_KEYS,
    )


def _included(name: str, *, included: bool) -> str:
    """Return the INCLUDED cell: secrets are never carried, an unasked part is not."""
    if included:
        return "yes"
    return "never" if name == ReportPartName.SECRETS else "no"


def render(view: View) -> list[str]:
    """Return the export card, native when its read model is held."""
    model = native(view)
    if isinstance(model, RunReportPlanView):
        return native_card(view, model)
    s = view.session
    dv.sel_in(s, len(_PARTS))
    lines = [f"{LABEL_MARK}PART                INCLUDED   SIZE{CHIP_END}", ""]
    lines.extend(
        ("▸" if i == s.sel else " ") + g_pad(part, 19) + g_pad(included, 11) + size
        for i, (part, included, size, _why) in enumerate(_PARTS)
    )
    lines.extend(["", "Plain text, one line per fact — nothing leaves the machine."])
    return boxed(
        view,
        crumb=f"Eä ▸ … ▸ {pt.EXPORT_RUN} ▸ Export",
        ctx=f"Run {pt.EXPORT_RUN} · claude · WAIT-PERM",
        pre=[],
        title="EXPORT · report this Run",
        lines=lines,
        foot=f"{_PARTS[s.sel][3]}  Enter renders the report; nothing is written.",
        keys=_KEYS,
    )


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Write the report of the card's Run on Enter, answering in the rack with its path.

    The plan the card states is the preview, so Enter writes at once and opens no second
    card. The file is the one ``eawf run report`` writes, planned from the tree the link
    reads and attributed to the principal the console acts as; the rack names where it
    landed and the purged ranges it marks rather than omits. A console holding no read
    model, or no tree to write into, says so and writes nothing.

    Args:
        ctx: The keystroke's context, carrying the frame's own read model.
        key: The dispatcher name of the key pressed.
        shift: Whether shift was held; the export card binds no shifted key.

    Returns:
        Whether the key was claimed.
    """
    if ctx.s.route != "export" or key != "Enter" or busy(ctx.s):
        return False
    model = ctx.projection
    run = ctx.s.subj_id
    if not isinstance(model, RunReportPlanView):
        ctx.notify(NOTHING_TO_REPORT, "report")
    elif ctx.tree_root is None or run is None:
        ctx.notify(NO_TREE, "report", Severity.WARN)
    else:
        try:
            reading = read_tree_run(ctx.tree_root, run)
        except LookupError as error:
            ctx.notify(f"{error} · nothing was written", "report", Severity.WARN)
            ctx.log(key, f"no report · {error}")
            return True
        plan = plan_run_report(
            reading,
            parts=DEFAULT_PARTS,
            actor=ctx.principal,
            tree_root=ctx.tree_root,
            on=ctx.clock.wall().date(),
        )
        write_run_report(plan)
        purged = next(line for line in plan.head if line.startswith(_PURGED_HEAD))
        ctx.notify(f"wrote {plan.shown_destination} · {purged}", "report")
    ctx.log(key, "took the report of this Run")
    return True
