"""evidence: a claim as a sentence somebody has to act on, its ladder of rungs and what
each one established. One ladder feeds both this route and the rung card, so the row
and the card can never disagree."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.frame import GTBL, chip, g_frame, lab, prose, thin
from ...chassis.keys import Ctx, busy, go

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    rungs = fixture.g.EV_RUNGS
    wide = w >= 120
    x = w >= 160
    room = w - 15
    EV = GTBL([13, 12, 38, 24, 0]) if x else GTBL([13, 12, 38, 0]) if wide else GTBL([13, 12, 0])
    dv.sel_in(s, len(rungs))
    b: list[str] = [lab("CLAIM", "CLM-0004 · the normalizer preserves event ordering under replay")]
    b.extend(
        prose(
            "IN WORDS",
            [
                "Replay any recorded log and every event arrives in the order it was recorded — no inversions, and no gaps in the sequence."
            ],
            room,
        )
    )
    b.extend(
        prose(
            "IT PROVES",
            [
                "A replayed run reads exactly like the live one, which is what lets Activity, the Timeline and History be rebuilt from the log at all."
            ]
            if wide
            else ["A replay can be read exactly like the live run."],
            room,
        )
    )
    b.extend(
        prose(
            "BREAKS IF",
            [
                "One replay puts two events out of order, or a gap is closed by inventing an event instead of reconciling it."
            ],
            room,
        )
    )
    if x:
        b.extend(
            prose(
                "WHO CARES",
                [
                    "Every operator reading a rebuilt Timeline, and every verdict a Run signs off from replayed evidence."
                ],
                room,
            )
        )
    b.append(lab("SUPPORTS", "MLS-0007 · 3 claims in graph · 1 uncertified"))
    b.append(thin(w))
    b.append(
        EV.head(["RUNG", "OUTCOME", "WHAT IT CHECKED", "IT RAN OVER", "AS OF"])
        if x
        else EV.head(["RUNG", "OUTCOME", "WHAT IT CHECKED", "AS OF"])
        if wide
        else EV.head(["RUNG", "OUTCOME", "WHAT IT CHECKED"])
    )
    for i, r in enumerate(rungs):
        if x:
            cells = [r["n"], chip(r["sev"], r["out"]), r["checked"], r["over"], r["as"]]
        elif wide:
            cells = [r["n"], chip(r["sev"], r["out"]), r["checked"], r["as"]]
        else:
            cells = [r["n"], chip(r["sev"], r["out"]), r["checked"]]
        b.append(EV.row(cells, i == s.sel, w))
    b.append(thin(w))
    b.extend(
        prose(
            "LADDER",
            [
                "Each rung is a harder test than the one below it, and only the top rung certifies.",
                "A claim with three passes is still an uncertified claim.",
            ]
            if wide
            else [
                "Each rung is a harder test than the one below it, and only the top rung certifies."
            ],
            room,
        )
    )
    b.extend(
        prose(
            "STANDING",
            [
                "This claim stands uncertified.",
                "Rung 3 has no outcome — unknown, not failed — so nothing here has been retracted.",
            ]
            if wide
            else ["This claim stands uncertified.", "Rung 3 has no outcome — unknown, not failed."],
            room,
        )
    )
    if wide:
        b.append(thin(w))
        b.append(lab("GRAPH", "CLM-0004 ← CLM-0002 probe evidence ← EVT-0119"))
    return g_frame(
        s,
        fixture,
        "Eä ▸ eawf-core ▸ Evidence ▸ CLM-0004",
        "Claim CLM-0004 · 4 rungs · uncertified · as of 14:02",
        b,
        [("↑↓", "rung"), ("Enter", "what it found"), ("y", "copy"), ("Esc", "back")],
        w,
        h,
    )


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Enter opens the rung card; the rung is captured when the card opens and nothing in it moves."""
    s = ctx.s
    if s.route != "evidence" or key != "Enter" or busy(s):
        return False
    s.rung = s.sel or 0
    go(ctx, "evidence.digest", f"rung {s.rung + 1} · its input digest and what it found")
    return True
