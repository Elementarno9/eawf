"""history.diff: one entity at two revisions, field by field; `p` cycles the revision
pair and `e` opens the entity the diff is about."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis import keys
from ...chassis.frame import GTBL, chip, g_frame, thin

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_PAIRS: tuple[str, ...] = ("41,150 → 41,208", "41,088 → 41,150", "40,990 → 41,088")
_ENTITY = "RUN-538453eb"


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    fields: list[list[str]] = [
        ["state", chip("ok", "RUNNING"), chip("wn", "WAIT-PERM"), "the agent"],
        ["reason", "run active", "needs permission", "the agent"],
        ["attempt", "1 of 1", "1 of 1", "– unchanged"],
        ["cost", "~ 3.10", "~ 4.62", "rate card"],
        ["peak rss", "∅ uncertified", "∅ uncertified", "– unchanged"],
    ]
    dv.sel_in(s, len(fields))
    hd = GTBL([12, 17, 17, 0])
    body: list[str] = [
        f" SUBJECT      {_ENTITY} · EAWF-0042 Bound replay",
        " BETWEEN      41,150  13:58:04   →   41,208  14:02:11",
        thin(w),
        hd.head(["FIELD", "THEN", "NOW", "CAUSED BY"]),
    ]
    for i, x in enumerate(fields):
        body.append(hd.row(x, i == s.sel, w))
    body.append(thin(w))
    body.append(" UNCHANGED    11 fields · counted here, never hidden")
    body.append(" RULE         One entity at two revisions — the subject is never a range.")
    ctx = f"{_ENTITY} · {s.diff_pair or _PAIRS[0]} · 4 fields changed"
    klist = [
        ("↑↓", "field"),
        ("Enter", "field"),
        ("e", "entity"),
        ("p", "revisions"),
        ("Esc", "back"),
    ]
    return g_frame(s, fixture, f"Eä ▸ {fixture.scope} ▸ History ▸ Diff", ctx, body, klist, w, h)


def seam(ctx: keys.Ctx, key: str, shift: bool) -> bool:
    s = ctx.s
    if keys.busy(s):
        return False
    if key == "e":
        keys.go(ctx, "run.detail", "the entity this diff is about", _ENTITY)
        return True
    if key == "p":
        cur = s.diff_pair or _PAIRS[0]
        ix = _PAIRS.index(cur) if cur in _PAIRS else -1
        s.diff_pair = _PAIRS[(ix + 1) % len(_PAIRS)]
        ctx.notify(s.diff_pair, title="revisions")
        ctx.log("p", f"revisions → {s.diff_pair}")
        return True
    return False
