"""The readiness matrix: every signal with its evidence, then the approval and publish refusal.

The state rows are spliced above the keybar of the built frame.
"""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import TABLES, Table, View, bar, build, header, thin
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.overlays.states import with_state_rows
from eawf.surfaces.tui.console.renderers.release import SIGNALS


def render(view: View) -> list[str]:
    """Return the readiness matrix."""
    s, w = view.session, view.w
    dv.sel_in(s, len(SIGNALS))
    rows = [
        header(view, " Eä ▸ readiness · REL-0001"),
        " every signal, with the evidence behind it",
        bar(w),
        Table([38, 11, 0], 2).head(["READINESS SIGNAL", "STATE", "EVIDENCE"]),
        *(TABLES["RD"].row(list(x), i == s.sel) for i, x in enumerate(SIGNALS)),
        thin(w),
        " APPROVAL  Granted at head 9d2b41c · still exact.",
        "           if that head moves this approval is invalidated and the",
        "           release returns to CANDIDATE with the cause named",
        thin(w),
        " NOT       Publish is unavailable — 1 member is not accepted.",
    ]
    frame = build(view, rows, keybar([("↑↓", "signal"), ("Esc", "back")], w))
    return with_state_rows("readiness", frame, s, w)
