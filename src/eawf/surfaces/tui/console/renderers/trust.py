"""trust: the milestone's truth fields, who answered for each, and the agents' track record.

Enter opens the evidence behind the focused field, which is the route's own key rather
than a dispatcher one: the footer advertises it, so something has to claim it.
"""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import Grid, View, chip, g_frame, thin
from eawf.surfaces.tui.console.navigation import Ctx, busy, go
from eawf.surfaces.tui.console.renderers.read_model import native, native_frame

_KEYS: tuple[tuple[str, str], ...] = (
    ("↑↓", "field"),
    ("Enter", "evidence"),
    (".", "actions"),
    ("i", "inspect"),
    ("Esc", "back"),
)


def render(view: View) -> list[str]:
    """Return the Trust frame, native when a read model is held."""
    model = native(view)
    if model is not None:
        return native_frame(view, model)
    s, w = view.session, view.w
    fields = [
        ["jury-01", chip("ok", "pass"), "conformance runner", "14:01"],
        ["jury-02", chip("ok", "pass"), "conformance runner", "13:58"],
        ["jury-03", chip("info", "? unknown"), "conformance runner", "no outcome recorded"],
    ]
    dv.sel_in(s, len(fields))
    juries = Grid([10, 12, 21, 0])
    record = Grid([16, 11, 11, 0], 3)
    # the column says who answered for the field, never the internal "producer" word
    body = [juries.head(["JURY", "VERDICT", "ANSWERED BY", "FRESHNESS"])]
    body.extend(juries.row(r, i == s.sel, w) for i, r in enumerate(fields))
    body.extend(
        [
            thin(w),
            " CALIBRATION  ~ 0.31 Brier over 41 resolved · metrics · 13:40",
            thin(w),
            " TRACK RECORD",
            record.head(["AGENT", "ACCEPTED", "REJECTED", "RATE"]),
            record.row(["claude", "18", "2", "~ 0.90"], False, w),
            record.row(["codex", "6", "0", "~ 1.00"], False, w),
            record.row(["local-runner", "0", "0", "∅ unavailable"], False, w),
            thin(w),
            " FIELD        A rate over zero judged attempts is ∅ unavailable, never 0.00.",
        ]
    )
    return g_frame(
        view,
        crumb="Eä ▸ eawf-core ▸ MLS-0007 ▸ Trust",
        ctx="Milestone MLS-0007 · 3 truth fields · as of 14:02",
        body=body,
        keys=_KEYS,
    )


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Open the evidence behind the focused truth field on Enter."""
    s = ctx.s
    if s.route != "trust" or key != "Enter" or busy(s):
        return False
    go(ctx, "evidence", "the evidence behind this truth field")
    return True
