"""git.pr: the branch, its commits and the PR review as the console sees them; read only,
`m` opens the conflict card and every mutation stays in the operator's git tool."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis import keys
from ...chassis.frame import GTBL, g_frame, thin

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_COMMITS: list[list[str]] = [
    ["8f14c2", "13:58", "Bound the replay window to the acked cursor"],
    ["a1d0e9", "13:31", "Add an ordering test for the normalizer"],
    ["77c410", "13:12", "Probe: reproduce the ordering assumption"],
]
_KEYS: list[tuple[str, str]] = [
    ("↑↓", "row"),
    ("Enter", "commit"),
    ("m", "conflict"),
    ("y", "copy"),
    ("Esc", "back"),
]


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    gp = GTBL([9, 9, 0])
    dv.sel_in(s, len(_COMMITS))
    b: list[str] = [
        " BRANCH       eawf/bound-replay-window · ahead 4 · behind 1",
        " HEAD         8f14c2 · authored by the agent at 13:58",
        thin(w),
        gp.head(["COMMIT", "WHEN", "SUBJECT"]),
    ]
    for i, x in enumerate(_COMMITS):
        b.append(gp.row(x, i == s.sel, w))
    b.extend(
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
        s,
        fixture,
        "Eä ▸ … ▸ BAT-0001 ▸ Git",
        "read only · the console never touches a remote",
        b,
        _KEYS,
        w,
        h,
    )


def seam(ctx: keys.Ctx, key: str, shift: bool) -> bool:
    if keys.busy(ctx.s):
        return False
    if key == "m":
        keys.go(ctx, "merge.conflict", "the conflict in this PR")
        return True
    return False
