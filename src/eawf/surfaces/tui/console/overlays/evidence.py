"""The evidence viewer: the receipts behind a claim.

The subject comes from the route and the selected row, since a campaign claim and a
milestone criterion are different models. The three state rows replace the built frame's
last three body rows.
"""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import TABLES, View, bar, build, header, thin
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.overlays.states import with_state_rows

_CAMPAIGN_EVIDENCE: tuple[tuple[str, str, str, str, str], ...] = (
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
_MILESTONE_RECEIPTS: tuple[tuple[str, str, str, str], ...] = (
    ("EVT-2201", "every escape has an authority ref", "receipt", "Jul 14"),
    ("EVT-2204", "no escape bypasses the ledger", "receipt", "Jul 14"),
    ("EVT-2209", "replay of the ledger is exact", "receipt", "Jul 15"),
)


def _campaign(view: View) -> list[str]:
    s, w = view.session, view.w
    claim = _CAMPAIGN_EVIDENCE[s.sel] if s.sel < len(_CAMPAIGN_EVIDENCE) else _CAMPAIGN_EVIDENCE[0]
    rows = [
        header(view, f" Eä ▸ evidence · {claim[0]}"),
        " CAM-0001 Provider drift · a claim, and what backs it",
        bar(w),
        f" CLAIM     {claim[2]}",
        f" FROM      {claim[1]}",
        f" SUPPORT   {claim[3]} · {claim[4]}",
        thin(w),
        " CONTRADICTION",
        "   EVD-0011 and EVD-0014 disagree about the same task",
        "   neither is retracted — the campaign records the disagreement",
        "   and its bound says stop here",
        thin(w),
        " NOT       a campaign claim is not acceptance proof: it never counts",
        "           toward a milestone, and it seals no digest.",
    ]
    return build(view, rows, keybar([("y", "copy receipts"), ("Esc", "back")], w))


def _milestone(view: View) -> list[str]:
    s, w = view.session, view.w
    dv.sel_in(s, len(_MILESTONE_RECEIPTS))
    table = TABLES["RC"]
    rows = [
        header(view, " Eä ▸ evidence · MLS-0004"),
        " at digest 7c1f…a94 · quotable at this exact revision",
        bar(w),
        table.head(["EVIDENCE", "RECEIPT", "CLAIM", "KIND"]),
        *(table.row(list(e), i == s.sel) for i, e in enumerate(_MILESTONE_RECEIPTS)),
        thin(w),
        " DENIED    The audit sign-off is recorded — ≠ denied to your class.",
        "           the criterion is neither met nor failed; you cannot see it",
        thin(w),
        " NOT       Reading evidence does not accept the milestone.",
    ]
    keys = keybar([("↑↓", "receipt"), ("y", "copy"), ("Esc", "back")], w)
    return build(view, rows, keys)


def render(view: View) -> list[str]:
    """Return the evidence viewer."""
    frame = _campaign(view) if view.session.route == "campaign" else _milestone(view)
    return with_state_rows("evidence", frame, view.session, view.w)
