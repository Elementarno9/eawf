"""export: the parts a plain-text report of a Run carries and what each costs.

Nothing leaves the machine.

When the console holds the export route's read model the card states the plan of that
view: each part sized off the rows in hand rather than estimated, and the digest the
report would be taken at. Taking the report writes nothing into the tree, so the card
promises an artifact rather than a state change.
"""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import CHIP_END, LABEL_MARK, View, boxed, g_pad
from eawf.surfaces.tui.console.navigation import Ctx, busy
from eawf.surfaces.tui.console.renderers.read_model import native
from eawf.workflow.projection.acceptance import RunReportPlanView, export_report

#: What Enter says when the console holds no read model to report. A prototype register
#: is not a Run, so there is nothing here an export could be taken of.
NOTHING_TO_REPORT = "no read model is held, so there is nothing to report"

_PARTS: tuple[tuple[str, str, str, str], ...] = (
    ("timeline", "yes", "41,208 events", "Every event this Run recorded, in order."),
    ("usage and cost", "yes", "~ 4.62 · derived", "Derived from the events, not from a bill."),
    ("transcript", "yes", "6,102 lines known", "What the runner said, quoted exactly."),
    ("secrets", "never", "∅ redacted by policy", "Policy redacts these; no export can carry them."),
    (
        "sandbox decisions",
        "no",
        "142 · space includes",
        "Left out — including them adds 142 lines.",
    ),
)
_KEYS: tuple[tuple[str, str], ...] = (("↑↓", "part"), ("Enter", "export"), ("Esc", "cancel"))


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
        + g_pad("yes" if part.included else "never", 11)
        + part.size
        for index, part in enumerate(model.parts)
    )
    lines.extend(["", f"Plain text at digest {model.digest} — nothing leaves the machine."])
    return boxed(
        view,
        crumb=f"Eä ▸ {model.scope_id} ▸ Export",
        ctx=f"{model.route} · cursor {model.source_cursor}",
        pre=[],
        title="EXPORT · report this view",
        lines=lines,
        foot=f"{model.parts[session.sel].why}  The report is taken; no record moves.",
        keys=_KEYS,
    )


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
        crumb="Eä ▸ … ▸ RUN-538453eb ▸ Export",
        ctx="Run RUN-538453eb · claude · WAIT-PERM",
        pre=[],
        title="EXPORT · report this Run",
        lines=lines,
        foot=f"{_PARTS[s.sel][3]}  Enter writes the report and names the path.",
        keys=_KEYS,
    )


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Take the report on Enter, which is a read and moves no record.

    The report is rendered from the very read model the frame was drawn from, so what an
    operator is told was reported is what they were looking at. Nothing is written into
    the workspace: the toast names the digest the report was taken at and its size, and
    a console holding no read model says there is nothing to report rather than claiming
    a report it could not have produced.

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
    if isinstance(model, RunReportPlanView):
        report = export_report(model)
        ctx.notify(f"{len(report.lines)} lines at {report.digest} · nothing was written", "report")
    else:
        ctx.notify(NOTHING_TO_REPORT, "report")
    ctx.log(key, "took the report of this view")
    return True
