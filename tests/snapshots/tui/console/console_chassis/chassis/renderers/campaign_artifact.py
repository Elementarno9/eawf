"""campaign.artifact: one artifact as rendered text with the file's own provenance,
scrolled where the terminal is shorter than the file rather than truncated without
saying so. The card scrolls its own file; the route beneath keeps its cursor exactly."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...chassis.frame import boxed
from ...chassis.keys import Ctx

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

Win = dict[str, int]


def _art(session: Session, fixture: Fixture) -> dict[str, Any]:
    arts = fixture.g.CAM_ART
    return arts[min(session.artifact or 0, len(arts) - 1)]


def art_window(session: Session, a: dict[str, Any], h: int) -> Win:
    """The rows the chrome leaves (header, rule, two provenance lines, both borders, the
    foot and the keys) minus whatever the indicators themselves cost; the offset is
    corrected for this render so an overshoot never leaves ArrowUp doing nothing."""
    total = len(a["body"])
    room = h - 9
    if total <= room:
        session.art_scroll = 0
        session.art_max = 0
        return {"from": 0, "take": total, "above": 0, "below": 0}

    def fit(off: int) -> Win:
        a_ind = 1 if off > 0 else 0
        take = room - a_ind
        below = total - off - take
        if below > 0:
            take = room - a_ind - 1
            below = total - off - take
        return {"from": off, "take": take, "above": off, "below": max(0, below)}

    max_off = total - fit(total)["take"]
    off = max(0, min(session.art_scroll or 0, max_off))
    session.art_scroll = off
    session.art_max = max_off
    return fit(off)


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    a = _art(s, fixture)
    pre = [
        f" SOURCE     {a['f']} · {a['k']} · {a['sz']} · written {a['at']} by {a['by']}",
        f" RECORD     Kept with CAM-0001 · {a['dg']}",
    ]
    win = art_window(s, a, h)
    lines: list[str] = []
    if win["above"]:
        lines.append(f"… {win['above']} line{'' if win['above'] == 1 else 's'} above")
    lines.extend(a["body"][win["from"] : win["from"] + win["take"]])
    if win["below"]:
        lines.append(f"… {win['below']} line{'' if win['below'] == 1 else 's'} below")
    klist: list[tuple[str, str]] = [("↑↓", "scroll")] if (win["above"] or win["below"]) else []
    klist.extend([("y", "copy"), ("Esc", "close")])
    arts = list(fixture.g.CAM_ART)
    return boxed(
        s,
        fixture,
        f"Eä ▸ eawf-core ▸ Research ▸ CAM-0001 ▸ {a['f']}",
        f"Campaign CAM-0001 · artifact {arts.index(a) + 1} of {len(arts)} · as of 14:02",
        pre,
        str(a["f"]).upper(),
        lines,
        "the file is the record · the console renders it, it does not rewrite it",
        klist,
        w,
        h,
        {"total": len(a["body"]), "from": win["from"], "take": win["take"]},
    )


def copy(session: Session, fixture: Fixture) -> str:
    return f"urn:eawf:{fixture.scope}:CAM-0001:artifact:{_art(session, fixture)['f']}"


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    s = ctx.s
    if s.route != "campaign.artifact" or key not in ("ArrowDown", "ArrowUp"):
        return False
    # the fit is computed for this artifact, never read from the last render: a file that
    # fits refuses the key with its reason instead of moving a cursor nothing can show
    art = _art(s, ctx.fixture)
    s.art_scroll = max(0, s.art_scroll or 0)
    art_window(s, art, ctx.h)
    mx = s.art_max or 0
    if not mx:
        s.art_scroll = 0
        ctx.log(key, "the whole file is shown — nothing to scroll")
        return True
    direction = 1 if key == "ArrowDown" else -1
    nxt = (s.art_scroll or 0) + direction
    # offset 1 hides a single line behind an indicator that costs the row it would have
    # shown, so the step passes over it rather than the clamp undoing it
    if nxt == 1 and mx >= 2:
        nxt = 2 if direction > 0 else 0
    s.art_scroll = max(0, min(nxt, mx))
    ctx.log(key, f"scroll {art['f']}")
    return True
