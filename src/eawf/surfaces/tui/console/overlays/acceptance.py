"""The acceptance evidence overlay: a milestone's sealed bundle and the receipts inside it.

Its subject is the milestone whose bundle it reads, captured when it opened; the evidence
viewer is the other overlay, over one Claim. The cursor walks the receipts and the foot
names the one it is on.
"""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.frame import TABLES, View, bar, build, header, thin
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.overlays.chassis import crumb, cursor_foot

NAME = "acceptance"
RECEIPTS: tuple[tuple[str, str, str, str], ...] = (
    ("EVT-2201", "every escape has an authority ref", "receipt", "Jul 14"),
    ("EVT-2204", "no escape bypasses the ledger", "receipt", "Jul 14"),
    ("EVT-2209", "replay of the ledger is exact", "receipt", "Jul 15"),
)


def render(view: View) -> list[str]:
    """Return the acceptance evidence overlay."""
    s, w = view.session, view.w
    sel = dv.sel_in(s, len(RECEIPTS))
    table = TABLES["RC"]
    rows = [
        header(view, crumb(NAME, s.ov_subject or pt.OWN_MILESTONE)),
        " at digest 7c1f…a94 · quotable at this exact revision",
        bar(w),
        table.head(["EVIDENCE", "RECEIPT", "CLAIM", "KIND"]),
        *(table.row(list(e), i == sel) for i, e in enumerate(RECEIPTS)),
        thin(w),
        " DENIED    The audit sign-off is recorded — ≠ denied to your class.",
        "           the criterion is neither met nor failed; you cannot see it",
        thin(w),
        " NOT       Reading evidence does not accept the milestone.",
        cursor_foot("RECEIPT", sel + 1, len(RECEIPTS)),
    ]
    keys = keybar([("↑↓", "receipt"), ("y", "copy"), ("Esc", "back")], w)
    return build(view, rows, keys)
