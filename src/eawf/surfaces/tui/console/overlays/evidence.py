"""The evidence viewer: one claim, the receipts behind it and the ladder it stands on.

Its subject is the claim it was opened on, captured when it opened, so walking its receipts
never moves it; the acceptance evidence overlay is the other overlay, over a milestone's
sealed bundle.
"""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import View, bar, build, header, thin
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.overlays.chassis import crumb, cursor_foot
from eawf.surfaces.tui.console.width import pad

NAME = "evidence"
# id, what it was read from, what it claims, its support and the receipts backing it
CLAIMS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "EVD-0011",
        "digest differs on retry",
        "drift is real",
        "3 receipts",
        "EVT-9101 EVT-9104 EVT-9108",
    ),
    ("EVD-0014", "digest stable over 40", "drift is bounded", "2 receipts", "EVT-9120 EVT-9126"),
    ("EVD-0019", "provider changed model", "cause is upstream", "1 receipt", "EVT-9131"),
)
_LABEL = 9


def claim_id(sel: int) -> str:
    """Return the id of the claim on route row ``sel``, the first past the end."""
    return CLAIMS[sel][0] if 0 <= sel < len(CLAIMS) else CLAIMS[0][0]


def render(view: View) -> list[str]:
    """Return the evidence viewer."""
    s, w = view.session, view.w
    claim = next((c for c in CLAIMS if c[0] == s.ov_subject), CLAIMS[0])
    receipts = claim[4].split()
    sel = dv.sel_in(s, len(receipts))
    rows = [
        header(view, crumb(NAME, claim[0])),
        " CAM-0001 Provider drift · a claim, and what backs it",
        bar(w),
        f" CLAIM     {claim[2]}",
        f" FROM      {claim[1]}",
        f" SUPPORT   {claim[3]}",
        *(
            f" {pad('RECEIPTS' if i == 0 else '', _LABEL)} {'▸' if i == sel else ' '} {receipt}"
            for i, receipt in enumerate(receipts)
        ),
        thin(w),
        " CONTRADICTION",
        "   EVD-0011 and EVD-0014 disagree about the same task",
        "   neither is retracted — the campaign records the disagreement",
        "   and its bound says stop here",
        thin(w),
        " NOT       a campaign claim is not acceptance proof: it never counts",
        "           toward a milestone, and it seals no digest.",
        cursor_foot("RECEIPT", sel + 1, len(receipts)),
    ]
    return build(view, rows, keybar([("↑↓", "receipt"), ("y", "copy"), ("Esc", "back")], w))
