"""release: the candidate's membership table over its readiness matrix.

Tab moves the cursor between the two regions, and Enter on a readiness row opens the
matrix overlay.
"""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import (
    TABLES,
    Table,
    View,
    bar,
    build,
    header,
    route_keys_bar,
    thin,
)
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS

MEMBERSHIP = "MEMBERSHIP"
READINESS = "READINESS"
_MEMBERS: tuple[tuple[str, str, str], ...] = (
    ("MLS-0001 Replay-safe activity", "Runtime", "Jul 14"),
    ("MLS-0007 Calibration set", "Research", "Jul 15"),
    ("MLS-0004 Escape-ledger", "Trust", "not yet"),
)
SIGNALS: tuple[tuple[str, str, str], ...] = (
    ("acceptance complete", "no", "1 still in review"),
    ("policy gate", "passed", "receipt EVT-3301"),
    ("artifact build", "passed", "receipt EVT-3302"),
    ("approvals", "1 of 2", "owner sign-off open"),
)


def render(view: View) -> list[str]:
    """Return the Release frame."""
    s, fx, w = view.session, view.fixture, view.w
    dv.sel_in(s, len(_MEMBERS))
    on_readiness = (s.rel_reg or MEMBERSHIP) == READINESS
    if on_readiness:
        s.rel_sel = max(0, min(len(SIGNALS) - 1, s.rel_sel))
    members = TABLES["MB"]
    rows = [
        header(view, f" Eä ▸ {fx.scope} ▸ REL-0001"),
        " Release REL-0001 v0.7.0-rc1 · CANDIDATE",
        bar(w),
        members.head(["MEMBERSHIP", "TRACK", "ACCEPTED"]),
    ]
    rows.extend(
        members.row(list(m), not on_readiness and i == s.sel) for i, m in enumerate(_MEMBERS)
    )
    readiness = Table([34, 11, 0], 2)
    rows.append(thin(w))
    rows.append(readiness.head(["READINESS", "STATE", "EVIDENCE"]))
    rows.extend(
        readiness.row(list(x), on_readiness and i == s.rel_sel) for i, x in enumerate(SIGNALS)
    )
    rows.extend(
        [
            thin(w),
            " APPROVAL  Granted at head 9d2b41c · still exact.",
            " PUBLICATION  Not started — nothing has been published.",
        ]
    )
    return build(view, rows, route_keys_bar(view, ROUTE_KEYS["release"]))
