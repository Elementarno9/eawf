"""crash.recovery: the three doors back into a projection the console lost; none discards work."""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import Grid, View, g_frame, thin

_KEYS: tuple[tuple[str, str], ...] = (
    ("↑↓", "door"),
    ("Enter", "choose"),
    ("i", "inspect"),
    ("Esc", "later"),
)
_DOORS: tuple[list[str], ...] = (
    ["reattach", "≈ 4s", "events after 41,208"],
    ["replay", "≈ 90s from 41,190", "nothing — exact to the point"],
    ["read-only", "≈ 1s", "no mutation until you attach"],
)


def render(view: View) -> list[str]:
    """Return the Recovery frame."""
    s, w = view.session, view.w
    grid = Grid([13, 21, 0])
    dv.sel_in(s, len(_DOORS))
    body = [
        " HAPPENED     The console lost its projection at 14:02:11.",
        "              Agents kept working · 4 runs were active then",
        thin(w),
        grid.head(["DOOR", "COSTS", "CANNOT RECOVER"]),
    ]
    body.extend(grid.row(x, i == s.sel, w) for i, x in enumerate(_DOORS))
    chosen = _DOORS[s.sel]
    body.extend(
        [
            thin(w),
            f" CHOSEN       {chosen[0]} · costs {chosen[1]}",
            f"              cannot recover {chosen[2]}",
            thin(w),
            " NO LOSS      No door discards work; two of them defer reading it.",
        ]
    )
    return g_frame(
        view,
        crumb="Eä ▸ eawf-core ▸ Recovery",
        ctx="The console stopped at 14:02:11 · the daemon did not",
        body=body,
        keys=_KEYS,
    )
