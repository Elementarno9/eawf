"""history.diff: one entity at two revisions, field by field.

``p`` cycles the revision pair and ``e`` opens the entity the diff is about.
"""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import Grid, View, chip, g_frame, thin
from eawf.surfaces.tui.console.navigation import Ctx, busy, go
from eawf.surfaces.tui.console.renderers.spine import held, native_frame

PAIRS: tuple[str, ...] = ("41,150 → 41,208", "41,088 → 41,150", "40,990 → 41,088")
ENTITY = "RUN-538453eb"
_KEYS: tuple[tuple[str, str], ...] = (
    ("↑↓", "field"),
    ("Enter", "field"),
    ("e", "entity"),
    ("p", "revisions"),
    ("Esc", "back"),
)


def render(view: View) -> list[str]:
    """Return the History diff frame, native when a read model is held."""
    spine = held(view)
    if spine is not None:
        return native_frame(view, spine)
    s, w = view.session, view.w
    unchanged = "– unchanged"  # noqa: RUF001
    fields = [
        ["state", chip("ok", "RUNNING"), chip("wn", "WAIT-PERM"), "the agent"],
        ["reason", "run active", "needs permission", "the agent"],
        ["attempt", "1 of 1", "1 of 1", unchanged],
        ["cost", "~ 3.10", "~ 4.62", "rate card"],
        ["peak rss", "∅ uncertified", "∅ uncertified", unchanged],
    ]
    dv.sel_in(s, len(fields))
    grid = Grid([12, 17, 17, 0])
    body = [
        f" SUBJECT      {ENTITY} · EAWF-0042 Bound replay",
        " BETWEEN      41,150  13:58:04   →   41,208  14:02:11",
        thin(w),
        grid.head(["FIELD", "THEN", "NOW", "CAUSED BY"]),
    ]
    body.extend(grid.row(x, i == s.sel, w) for i, x in enumerate(fields))
    body.extend(
        [
            thin(w),
            " UNCHANGED    11 fields · counted here, never hidden",
            " RULE         One entity at two revisions — the subject is never a range.",
        ]
    )
    return g_frame(
        view,
        crumb=f"Eä ▸ {view.fixture.scope} ▸ History ▸ Diff",
        ctx=f"{ENTITY} · {s.diff_pair or PAIRS[0]} · 4 fields changed",
        body=body,
        keys=_KEYS,
    )


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Open the diffed entity on ``e`` and cycle the revision pair on ``p``."""
    s = ctx.s
    if busy(s):
        return False
    if key == "e":
        go(ctx, "run.detail", "the entity this diff is about", ENTITY)
        return True
    if key == "p":
        cur = s.diff_pair or PAIRS[0]
        at = PAIRS.index(cur) if cur in PAIRS else -1
        s.diff_pair = PAIRS[(at + 1) % len(PAIRS)]
        ctx.notify(s.diff_pair, title="revisions")
        ctx.log("p", f"revisions → {s.diff_pair}")
        return True
    return False
