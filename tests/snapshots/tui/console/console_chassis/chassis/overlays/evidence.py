"""The evidence viewer: the subject is derived from the route and the selected row, since
a campaign claim and a milestone criterion are different models and must not share one
fixture. The three state-model rows replace the frame's last three body rows, as the
prototype's wrap does over the built frame."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.frame import TSPEC, bar, build, header_row, keybar, thin
from ...chassis.overlays import with_state_rows

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_CAMPAIGN_EV: tuple[tuple[str, str, str, str, str], ...] = (
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
_MILESTONE_CR: tuple[tuple[str, str, str, str], ...] = (
    ("EVT-2201", "every escape has an authority ref", "receipt", "Jul 14"),
    ("EVT-2204", "no escape bypasses the ledger", "receipt", "Jul 14"),
    ("EVT-2209", "replay of the ledger is exact", "receipt", "Jul 15"),
)


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    if s.route == "campaign":
        e = _CAMPAIGN_EV[s.sel] if s.sel < len(_CAMPAIGN_EV) else _CAMPAIGN_EV[0]
        rows: list[str] = [
            header_row(s, fixture, f" Eä ▸ evidence · {e[0]}", w),
            " CAM-0001 Provider drift · a claim, and what backs it",
            bar(w),
            f" CLAIM     {e[2]}",
            f" FROM      {e[1]}",
            f" SUPPORT   {e[3]} · {e[4]}",
            thin(w),
            " CONTRADICTION",
            "   EVD-0011 and EVD-0014 disagree about the same task",
            "   neither is retracted — the campaign records the disagreement",
            "   and its bound says stop here",
            thin(w),
            " NOT       a campaign claim is not acceptance proof: it never counts",
            "           toward a milestone, and it seals no digest.",
        ]
        frame = build(s, rows, keybar([("y", "copy receipts"), ("Esc", "back")], w), w, h)
        return with_state_rows("evidence", frame, s, w)
    dv.sel_in(s, len(_MILESTONE_CR))
    RC = TSPEC["RC"]
    rows = [
        header_row(s, fixture, " Eä ▸ evidence · MLS-0004", w),
        " at digest 7c1f…a94 · quotable at this exact revision",
        bar(w),
        RC.head(["EVIDENCE", "RECEIPT", "CLAIM", "KIND"]),
    ]
    rows.extend(RC.row(list(e), i == s.sel) for i, e in enumerate(_MILESTONE_CR))
    rows.extend(
        [
            thin(w),
            " DENIED    The audit sign-off is recorded — ≠ denied to your class.",
            "           the criterion is neither met nor failed; you cannot see it",
            thin(w),
            " NOT       Reading evidence does not accept the milestone.",
        ]
    )
    frame = build(s, rows, keybar([("↑↓", "receipt"), ("y", "copy"), ("Esc", "back")], w), w, h)
    return with_state_rows("evidence", frame, s, w)
