"""cost.ceiling: spend against the day's ceiling; observe only, the ceiling moves in settings."""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.frame import Grid, View, g_frame, thin
from eawf.surfaces.tui.console.renderers.registers import native_frame
from eawf.surfaces.tui.console.session import Session

_KEYS: tuple[tuple[str, str], ...] = (("↑↓", "row"), ("Enter", "run"), ("Esc", "back"))
_STOPPED: tuple[tuple[str, str, str], ...] = pt.STOPPED_RUNS


def stopped_run(session: Session) -> str | None:
    """Return the Run the cursor sits on in the stopped list, or nothing when it is empty.

    The footer advertises ``Enter run``, so the key has to name a Run; this is the one
    place the list's shape is read, rather than the dispatcher reaching into it.
    """
    if not _STOPPED:
        return None
    return _STOPPED[min(max(session.sel, 0), len(_STOPPED) - 1)][0]


def render(view: View) -> list[str]:
    """Return the Cost ceiling frame."""
    if view.register is not None:
        return native_frame(view, view.register)
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
        crumb=f"Eä ▸ {view.fixture.scope} ▸ Cost ceiling",
        ctx="spend against ceiling · observe only",
        body=body,
        keys=_KEYS,
    )
