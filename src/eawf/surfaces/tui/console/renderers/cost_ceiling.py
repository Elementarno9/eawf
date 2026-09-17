"""cost.ceiling: spend against the day's ceiling; observe only, the ceiling moves in settings."""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import Grid, View, g_frame, thin

_KEYS: tuple[tuple[str, str], ...] = (("↑↓", "row"), ("Enter", "run"), ("Esc", "back"))
_STOPPED: tuple[list[str], ...] = (
    ["RUN-1c93af08", "13:41", "hard limit reached"],
    ["RUN-4e2b6c77", "12:08", "hard limit reached"],
)


def render(view: View) -> list[str]:
    """Return the Cost ceiling frame."""
    s, w = view.session, view.w
    grid = Grid([17, 8, 0])
    dv.sel_in(s, len(_STOPPED))
    body = [
        " CEILING      ~ 41.20 of 60.00 today · ≈ 18.80 left",
        " OWNED BY     settings ▸ execution ▸ concurrency · policy",
        thin(w),
        " STOPPED",
        grid.head(["RUN", "AT", "REASON"]),
    ]
    body.extend(grid.row(x, i == s.sel, w) for i, x in enumerate(_STOPPED))
    body.extend(
        [
            thin(w),
            " SPEND        claude  ~ 31.40 · 8 runs",
            "              codex   ~ 9.80 · 6 runs",
            "              local   ∅ unmetered — no rate card",
            thin(w),
            " AUTHORITY    This surface observes — the ceiling moves in settings.",
        ]
    )
    return g_frame(
        view,
        crumb="Eä ▸ eawf-core ▸ Cost ceiling",
        ctx="spend against ceiling · observe only",
        body=body,
        keys=_KEYS,
    )
