"""campaign: the research campaign as three windowed sections (PLAN, EVIDENCE, ARTIFACTS),
one walkable at a time. Each section shows a window of its list and counts what is off
either end, so no section may lie about its length; the focused window follows the cursor,
the others show their head."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...chassis import derive as dv
from ...chassis.frame import GTBL, Fixed, chip, g_frame, g_pad, g_row, strip_chips, thin
from ...chassis.keys import Ctx, busy, go
from ...chassis.width import pad

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

Win = dict[str, int]


def cam_list(session: Session, fixture: Fixture) -> tuple[Any, ...]:
    sec = session.cam_sec or "PLAN"
    G = fixture.g
    return G.CAM_STEPS if sec == "PLAN" else G.CAM_EVID if sec == "EVIDENCE" else G.CAM_ART


def cam_caps(w: int) -> dict[str, int]:
    """A cap leaves room for its own indicators: the smallest windowable section is three rows."""
    if w >= 160:
        return {"PLAN": 6, "EVIDENCE": 5, "ARTIFACTS": 5}
    if w >= 120:
        return {"PLAN": 6, "EVIDENCE": 4, "ARTIFACTS": 3}
    return {"PLAN": 3, "EVIDENCE": 3, "ARTIFACTS": 3}


def cam_win(total: int, cap: int, sel: int, focused: bool) -> Win:
    """The window onto a list: a single hidden row is shown rather than announced, and the
    cursor is never outside its own window."""
    if total <= cap:
        return {"from": 0, "take": total, "above": 0, "below": 0}
    sel = max(0, min(total - 1, sel))
    max_from = max(0, total - (cap - 1))

    def fit(frm: int) -> Win:
        if frm == 1:
            frm = 0
        a_ind = 1 if frm > 0 else 0
        take = cap - a_ind
        below = total - frm - take
        if below > 0:
            take = cap - a_ind - 1
            below = total - frm - take
        return {"from": frm, "take": take, "above": frm, "below": max(0, below)}

    win = fit(max(0, min(sel - (cap - 2) // 2, max_from)) if focused else 0)
    if focused and sel >= win["from"] + win["take"]:
        win = fit(max(0, min(sel - (win["take"] - 1), max_from)))
    return win


def made_cell(step: list[Any]) -> str:
    """What a step made, as one cell: the count is the truth, the first name only a sample."""
    made = step[4]
    if made:
        return made[0] + (f" +{len(made) - 1}" if len(made) > 1 else "")
    return "– still running" if "running" in step[1] else "– not started"


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    G = fixture.g
    wide = w >= 120
    x = w >= 160
    sec = s.cam_sec or "PLAN"
    dv.sel_in(s, len(cam_list(s, fixture)))
    ST = (
        GTBL([12, 24, 13, 13, 18, 0])
        if x
        else GTBL([12, 24, 13, 13, 0])
        if wide
        else GTBL([12, 20, 12, 0])
    )
    EV = (
        GTBL([12, 11, 26, 20, 13, 0])
        if x
        else GTBL([12, 11, 26, 20, 0])
        if wide
        else GTBL([12, 11, 26, 0])
    )
    AR = GTBL([12, 22, 0])
    b: list[str] = []

    def dim(t: str) -> Fixed:
        return Fixed(pad(strip_chips(t), w))

    def sect(name: str, text: str, on: bool) -> bool:
        lead = g_pad(" " + name, 13)
        b.append(Fixed(g_pad(lead + text, w)))
        return on

    def put(spec: GTBL, cells: list[str], on: bool, i: int) -> None:
        # the triangle sits one column before the first cell rather than in the gutter,
        # which would leave it twelve columns clear of the row it marks
        r = spec.row([""] + cells, False, w)
        if on and i == s.sel:
            r = r[:13] + "▸" + r[14:]
        b.append(g_row(r, w) if on else dim(r))

    def ind(spec: GTBL, text: str) -> Fixed:
        return dim(spec.row(["", "… " + text], False, w))

    steps = G.CAM_STEPS
    done = len([st for st in steps if "done" in st[1]])
    b.append(" QUESTION    Does provider drift change replay digests?")
    b.append(" BOUNDS      ≤20 runs · ≤8h · 2 providers · stop at first contradiction")
    if x:
        b.append(" STOP RULE   Armed — one contradiction is open, so step 6 may not start.")
    b.append(thin(w))
    on_p = sect(
        "PLAN",
        f"{done} of {len(steps)} steps done · 1 running · 1 blocked by step 5",
        sec == "PLAN",
    )
    if x:
        span1 = "─ ✓ 3 ───"
        span2 = "───────"
        b.append(g_pad(" GRAPH", 13) + "✓ 1 ─┐" + " " * len(span1) + "┌─ ✓ 4 ─┐")
        b.append(g_pad("", 13) + "✓ 2 ─┴" + span1 + "┴" + span2 + "┴─ ► 5 ─── ○ 6")
    elif wide:
        b.append(g_pad(" GRAPH", 13) + "✓ 1 · ✓ 2 → ✓ 3 → ✓ 4 → ► 5 → ○ 6")
    b.append(
        ST.head(["", "STEP", "STATE", "DEPENDS ON", "PRODUCED", "SPENT"])
        if x
        else ST.head(["", "STEP", "STATE", "DEPENDS ON", "PRODUCED"])
        if wide
        else ST.head(["", "STEP", "STATE", "DEPENDS ON"])
    )
    caps = cam_caps(w)
    win_p = cam_win(len(steps), caps["PLAN"], s.sel or 0, on_p)
    if win_p["above"]:
        b.append(ind(ST, f"{win_p['above']} above"))
    for k, st in enumerate(steps[win_p["from"] : win_p["from"] + win_p["take"]]):
        i = win_p["from"] + k
        if x:
            cells = [st[0], chip(st[2], st[1]), st[3], made_cell(st), st[5]]
        elif wide:
            cells = [st[0], chip(st[2], st[1]), st[3], made_cell(st)]
        else:
            cells = [st[0], chip(st[2], st[1]), st[3]]
        put(ST, cells, on_p, i)
    if win_p["below"]:
        b.append(ind(ST, f"{win_p['below']} below"))
    b.append(thin(w))
    evid = G.CAM_EVID
    on_e = sect(
        "EVIDENCE",
        f"{len(evid)} receipts · 1 contradiction open · 2 claims still uncertified",
        sec == "EVIDENCE",
    )
    b.append(
        EV.head(["", "RECEIPT", "WHAT IT SHOWS", "CLAIM", "SUPPORT", "DETAIL"])
        if x
        else EV.head(["", "RECEIPT", "WHAT IT SHOWS", "CLAIM", "SUPPORT"])
        if wide
        else EV.head(["", "RECEIPT", "WHAT IT SHOWS", "CLAIM"])
    )
    win_e = cam_win(len(evid), caps["EVIDENCE"], s.sel or 0, on_e)
    if win_e["above"]:
        b.append(ind(EV, f"{win_e['above']} above"))
    for k, e in enumerate(evid[win_e["from"] : win_e["from"] + win_e["take"]]):
        i = win_e["from"] + k
        cells = (
            [e[0], e[1], e[2], e[3], e[4]]
            if x
            else [e[0], e[1], e[2], e[3]]
            if wide
            else [e[0], e[1], e[2]]
        )
        put(EV, cells, on_e, i)
    if win_e["below"]:
        b.append(ind(EV, f"{win_e['below']} below"))
    if wide:
        b.append(g_pad(" CONFLICT", 13) + "EVD-0011 and EVD-0014 disagree about the same task")
    if x:
        b.append(
            g_pad("", 13) + "the bound says stop · step 6 stays blocked until one is retracted"
        )
    b.append(thin(w))
    arts = G.CAM_ART
    on_a = sect(
        "ARTIFACTS",
        f"{len(arts)} files · 1 finding promoted to MLS-0007 · 2 held",
        sec == "ARTIFACTS",
    )
    b.append(AR.head(["", "ARTIFACT", "WRITTEN"]))
    win_a = cam_win(len(arts), caps["ARTIFACTS"], s.sel or 0, on_a)
    if win_a["above"]:
        b.append(ind(AR, f"{win_a['above']} above"))
    for k, a in enumerate(arts[win_a["from"] : win_a["from"] + win_a["take"]]):
        i = win_a["from"] + k
        put(AR, [a["f"], a["at"]], on_a, i)
    if win_a["below"]:
        b.append(ind(AR, f"{win_a['below']} below"))
    if x:
        b.append(thin(w))
        b.append(" NEXT        Step 5 finishes, then the conflict is settled before the write-up.")
        b.append(" RECORD      Every artifact is kept with CAM-0001 · digests in each card")
    return g_frame(
        s,
        fixture,
        "Eä ▸ eawf-core ▸ Research ▸ CAM-0001",
        "Campaign CAM-0001 Provider drift · REVIEW · W30 · ~6.2 of 8h · 14 of 20 runs",
        b,
        [
            ("↑↓", "row"),
            ("Tab", "section"),
            ("Enter", "open"),
            (".", "actions"),
            ("i", "inspect"),
            ("Esc", "back"),
        ],
        w,
        h,
    )


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Three sections, one of them walkable at a time: Tab cycles, Enter opens what the
    focused section holds under the cursor."""
    s = ctx.s
    G = ctx.fixture.g
    if s.route != "campaign" or busy(s):
        return False
    sects = list(G.CAM_SECTS)
    if key == "Tab":
        ci = sects.index(s.cam_sec or "PLAN")
        s.cam_sec = sects[(ci + (len(sects) - 1 if shift else 1)) % len(sects)]
        s.sel = 0
        ctx.log("Tab", f"section → {s.cam_sec}")
        return True
    if key == "Enter":
        sec = s.cam_sec or "PLAN"
        i = s.sel or 0
        if sec == "ARTIFACTS":
            s.artifact = i
            s.art_scroll = 0
            go(ctx, "campaign.artifact", f"artifact {G.CAM_ART[i]['f']}")
            return True
        if sec == "EVIDENCE":
            go(
                ctx,
                "evidence",
                f"receipt {G.CAM_EVID[i][0]} · the claim it supports",
                G.CAM_EVID[i][5],
            )
            return True
        # a step is a place: Enter opens the step, not one of the things it made. The row
        # is zeroed after the jump so the back stack records the row Esc returns to; the
        # card needs its own zero because its cursor counts products.
        s.cam_step = i
        go(ctx, "campaign.step", f"step {G.CAM_STEPS[i][0]}")
        s.sel = 0
        return True
    return False
