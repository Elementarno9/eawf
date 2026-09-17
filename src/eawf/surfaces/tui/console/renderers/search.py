"""search: entity hits for one query, with exact counts; event text is never searched."""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import Grid, View, g_frame, thin

_KEYS: tuple[tuple[str, str], ...] = (
    ("↑↓", "hit"),
    ("Enter", "drill"),
    ("\\", "refine"),
    ("k", "kind"),
    ("Esc", "back"),
)
_HITS: tuple[list[str], ...] = (
    ["RUN-538453eb", "task title", "EAWF-0042 Bound replay"],
    ["RUN-be1e085a", "task title", "EAWF-0051 Coalesce"],
    ["EAWF-0042", "name", "Bound replay window"],
    ["BAT-0001", "name", "Semantic event slice"],
    ["MLS-0001", "name", "Replay-safe activity"],
    ["CLM-0004", "claim text", "ordering under replay"],
)


def render(view: View) -> list[str]:
    """Return the Search frame."""
    s, w = view.session, view.w
    grid = Grid([18, 18, 0])
    dv.sel_in(s, len(_HITS))
    body = [
        " QUERY        replay ▏",
        " KINDS        runs 6 · tasks 4 · batches 2 · milestones 1 · claims 1",
        thin(w),
        grid.head(["HIT", "WHAT MATCHED", "WHERE"]),
    ]
    body.extend(grid.row(x, i == s.sel, w) for i, x in enumerate(_HITS))
    body.extend(
        [
            thin(w),
            " WINDOW       6 of 14 · sorted by kind, then id · every count exact",
            " SCOPE        Entities only — event text is not searched.",
        ]
    )
    return g_frame(
        view,
        crumb="Eä ▸ eawf-core ▸ Search",
        ctx="query “replay” · entities only · 14 hits, exact",
        body=body,
        keys=_KEYS,
    )
