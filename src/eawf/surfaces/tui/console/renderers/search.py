"""search: entity hits for one query, with exact counts; event text is never searched."""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.frame import Grid, View, g_frame, thin
from eawf.surfaces.tui.console.renderers.spine import held, native_frame

_KEYS: tuple[tuple[str, str], ...] = (
    ("↑↓", "hit"),
    ("Enter", "drill"),
    ("\\", "refine"),
    ("k", "kind"),
    ("Esc", "back"),
)
_HITS: tuple[tuple[str, str, str], ...] = pt.SEARCH_HITS


def render(view: View) -> list[str]:
    """Return the Search frame, native when a read model is held."""
    spine = held(view)
    if spine is not None:
        return native_frame(view, spine)
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
        crumb=f"Eä ▸ {view.fixture.scope} ▸ Search",
        ctx="query “replay” · entities only · 14 hits, exact",
        body=body,
        keys=_KEYS,
    )
