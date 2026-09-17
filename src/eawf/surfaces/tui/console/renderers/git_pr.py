"""git.pr: the branch, its commits and the PR review as the console sees them.

The route only reads; ``m`` opens the conflict card, and every mutation stays in the
operator's git tool.
"""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import Grid, View, g_frame, thin
from eawf.surfaces.tui.console.navigation import Ctx, busy, go

_COMMITS: tuple[list[str], ...] = (
    ["8f14c2", "13:58", "Bound the replay window to the acked cursor"],
    ["a1d0e9", "13:31", "Add an ordering test for the normalizer"],
    ["77c410", "13:12", "Probe: reproduce the ordering assumption"],
)
_KEYS: tuple[tuple[str, str], ...] = (
    ("↑↓", "row"),
    ("Enter", "commit"),
    ("m", "conflict"),
    ("y", "copy"),
    ("Esc", "back"),
)


def render(view: View) -> list[str]:
    """Return the Git frame."""
    s, w = view.session, view.w
    grid = Grid([9, 9, 0])
    dv.sel_in(s, len(_COMMITS))
    body = [
        " BRANCH       eawf/bound-replay-window · ahead 4 · behind 1",
        " HEAD         8f14c2 · authored by the agent at 13:58",
        thin(w),
        grid.head(["COMMIT", "WHEN", "SUBJECT"]),
    ]
    body.extend(grid.row(x, i == s.sel, w) for i, x in enumerate(_COMMITS))
    body.extend(
        [
            thin(w),
            " REVIEW       PR #418 open · 1 approval · 1 change requested",
            " CHECKS       tests pass · lint pass · conformance ? unknown",
            thin(w),
            " ACTION       Merging, closing and pushing happen in your git tool.",
            "              the merge-conflict overlay also only displays",
        ]
    )
    return g_frame(
        view,
        crumb="Eä ▸ … ▸ BAT-0001 ▸ Git",
        ctx="read only · the console never touches a remote",
        body=body,
        keys=_KEYS,
    )


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Open the conflict card on ``m``."""
    if busy(ctx.s) or key != "m":
        return False
    go(ctx, "merge.conflict", "the conflict in this PR")
    return True
