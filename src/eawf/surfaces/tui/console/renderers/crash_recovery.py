"""crash.recovery: the three doors back into a projection the console lost; none discards work."""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.frame import Grid, View, g_frame, thin
from eawf.surfaces.tui.console.renderers.read_model import native, native_frame

_KEYS: tuple[tuple[str, str], ...] = (
    ("↑↓", "door"),
    ("Enter", "choose"),
    ("i", "inspect"),
    ("Esc", "later"),
)


def _doors(revision: int) -> tuple[list[str], ...]:
    """Return each door, what it costs and what it cannot recover past ``revision``."""
    return (
        ["reattach", "≈ 4s", f"events after {group(revision)}"],
        ["replay", "≈ 90s from 41,190", "nothing — exact to the point"],
        ["read-only", "≈ 1s", "no mutation until you attach"],
    )


def render(view: View) -> list[str]:
    """Return the Recovery frame, native when a read model is held."""
    model = native(view)
    if model is not None:
        return native_frame(view, model)
    s, w = view.session, view.w
    doors = _doors(view.fixture.proto.revision)
    grid = Grid([13, 21, 0])
    dv.sel_in(s, len(doors))
    body = [
        " HAPPENED     The console lost its projection at 14:02:11.",
        "              Agents kept working · 4 runs were active then",
        thin(w),
        grid.head(["DOOR", "COSTS", "CANNOT RECOVER"]),
    ]
    body.extend(grid.row(x, i == s.sel, w) for i, x in enumerate(doors))
    chosen = doors[s.sel]
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
        crumb=f"Eä ▸ {view.fixture.scope} ▸ Recovery",
        ctx="The console stopped at 14:02:11 · the daemon did not",
        body=body,
        keys=_KEYS,
    )
