"""campaign.step: one step of a campaign. A running step is watched (runner, progress,
the tail of its activity); a finished step is read (when it ran, what it produced); a
blocked step says what it waits on and shows no activity. Two regions, one pair of
arrows: Tab says which of them the arrows walk."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...chassis import derive as dv
from ...chassis.frame import GTBL, Fixed, chip, g_frame, g_row, lab, prose, strip_chips, thin
from ...chassis.keys import Ctx, busy, go
from ...chassis.renderers.campaign import cam_win
from ...chassis.width import pad

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session


def _step_ix(session: Session, fixture: Fixture) -> int:
    return min(session.cam_step or 0, len(fixture.g.CAM_STEPS) - 1)


def _waits_on(step: list[Any], detail: dict[str, Any]) -> str:
    """A dependency list said as a sentence: which steps, and whether they are done."""
    if step[3] == "–":
        return "Nothing — it could start at once."
    ids = str(step[3]).split(" · ")
    names = f"Steps {', '.join(ids[:-1])} and {ids[-1]}" if len(ids) > 1 else f"Step {ids[0]}"
    if detail["state"] == "blocked":
        return names + (" are not finished yet." if len(ids) > 1 else " is not finished yet.")
    return names + (" are finished." if len(ids) > 1 else " is finished.")


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    G = fixture.g
    i = _step_ix(s, fixture)
    st = G.CAM_STEPS[i]
    d = G.CAM_STEP_DETAIL[i]
    wide = w >= 120
    x = w >= 160
    made = list(st[4])
    dv.sel_in(s, max(1, len(made)))
    PR = GTBL([12, 22, 0])

    def dim(t: str) -> Fixed:
        return Fixed(pad(strip_chips(t), w))

    room = w - 15
    when = (
        f"started {d['started']}"
        if d["state"] == "running"
        else f"{d['started']} → {d['ended']}"
        if d["state"] == "done"
        else "never started"
    )
    b: list[str] = [
        lab("STEP", f"{st[0]} · {chip(st[2], st[1])} · {when}"),
        lab("WAITS ON", _waits_on(st, d)),
        lab(
            "RUNNER",
            f"{d['run']} · {d['prov']}" + (" · session fresh" if d["state"] == "running" else ""),
        ),
        lab("SPENT", st[5]),
        thin(w),
    ]
    b.extend(
        prose(
            "PROGRESS"
            if d["state"] == "running"
            else "OUTCOME"
            if d["state"] == "done"
            else "WAITING",
            [d["sum"]],
            room,
        )
    )
    b.append(thin(w))
    acts = list(d["acts"])
    on_hist = (s.step_reg or "HISTORY") == "HISTORY" and len(acts) > 0
    AT = GTBL([12, 8, 0])
    if acts:
        hh = AT.head(["", "AT", "WHAT HAPPENED"])
        b.append(
            lab("ACTIVITY" if d["state"] == "running" else "HISTORY", "")
            + "\x07"
            + hh[13:]
            + "\x06"
        )
        cap = 8 if x else 5 if wide else 2
        hw = cam_win(len(acts), cap, (s.hist_sel or 0) if on_hist else 0, on_hist)
        if hw["above"]:
            b.append(dim(AT.row(["", "", f"… {hw['above']} earlier"], False, w)))
        for k, t in enumerate(acts[hw["from"] : hw["from"] + hw["take"]]):
            r = AT.row(["", t[0], t[1]], False, w)
            if on_hist and hw["from"] + k == (s.hist_sel or 0):
                r = r[:13] + "▸" + r[14:]
            b.append(g_row(r, w))
        if hw["below"]:
            b.append(dim(AT.row(["", "", f"… {hw['below']} later"], False, w)))
    else:
        b.append(lab("ACTIVITY", "∅ Nothing to show — the step has not started."))
    b.append(thin(w))
    if made:
        ph = PR.head(["", "WHAT IT MADE", "KIND"])
        b.append(lab("PRODUCED", "") + "\x07" + ph[13:] + "\x06")
        for k, m in enumerate(made):
            kind = (
                "receipt · supports CLM-0004"
                if m.startswith("EVD-")
                else "artifact · kept with CAM-0001"
            )
            r = PR.row(["", m, kind], False, w)
            if not on_hist and k == s.sel:
                r = r[:13] + "▸" + r[14:]
            b.append(g_row(r, w))
    else:
        b.append(
            lab(
                "PRODUCED",
                "∅ None yet · "
                + ("it is still running." if d["state"] == "running" else "it has not started."),
            )
        )
    klist: list[tuple[str, str]] = [("Tab", "region")] if (acts and made) else []
    if on_hist:
        klist.append(("↑↓", "line"))
    elif made:
        klist.extend([("↑↓", "product"), ("Enter", "open")])
    klist.extend([("y", "copy"), ("Esc", "back")])
    n = str(st[0]).split(" ")[0]
    return g_frame(
        s,
        fixture,
        f"Eä ▸ eawf-core ▸ Research ▸ CAM-0001 ▸ Step {n}",
        f"Campaign CAM-0001 · step {n} of {len(G.CAM_STEPS)} · as of 14:02",
        b,
        klist,
        w,
        h,
    )


def copy(session: Session, fixture: Fixture) -> str:
    n = str(fixture.g.CAM_STEPS[_step_ix(session, fixture)][0]).split(" ")[0]
    return f"urn:eawf:{fixture.scope}:CAM-0001:step:{n}"


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    s = ctx.s
    G = ctx.fixture.g
    if s.route != "campaign.step":
        return False
    i = _step_ix(s, ctx.fixture)
    dd = G.CAM_STEP_DETAIL[i]
    acts = list(dd["acts"])
    made0 = list(G.CAM_STEPS[i][4])
    if not busy(s):
        if key == "Tab":
            if not acts or not made0:
                ctx.log("Tab", "this step has only one region to walk")
                return True
            s.step_reg = "PRODUCED" if (s.step_reg or "HISTORY") == "HISTORY" else "HISTORY"
            ctx.log("Tab", f"region → {s.step_reg}")
            return True
        if (s.step_reg or "HISTORY") == "HISTORY" and acts and key in ("ArrowDown", "ArrowUp"):
            s.hist_sel = max(
                0, min(len(acts) - 1, (s.hist_sel or 0) + (1 if key == "ArrowDown" else -1))
            )
            what = " ".join(str(acts[s.hist_sel][1]).split())[:40]
            ctx.log(key, f"{acts[s.hist_sel][0]} · {what}")
            return True
    # a product opens from the step that made it: an artifact as rendered text, a receipt
    # as the claim it supports
    if key == "Enter" and not busy(s) and (s.step_reg or "HISTORY") == "PRODUCED":
        stp = G.CAM_STEPS[i]
        mk = stp[4][s.sel or 0] if (s.sel or 0) < len(stp[4]) else None
        if not mk:
            ctx.log("Enter", f"{stp[0]} has produced nothing to open yet")
            return True
        if str(mk).startswith("EVD-"):
            go(ctx, "evidence", f"{mk} · the claim it supports", "CLM-0004")
            return True
        ai = 0
        for n, a in enumerate(G.CAM_ART):
            if a["f"] == mk:
                ai = n
        s.artifact = ai
        s.art_scroll = 0
        go(ctx, "campaign.artifact", f"{stp[0]} wrote {mk}")
        return True
    return False
