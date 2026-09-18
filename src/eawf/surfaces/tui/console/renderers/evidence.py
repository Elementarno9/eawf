"""evidence: a claim as a sentence somebody has to act on, with its ladder of rungs.

One ladder feeds both this route and the rung card, so the row and the card can never
disagree.
"""

from __future__ import annotations

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import Grid, View, chip, g_frame, lab, prose, thin
from eawf.surfaces.tui.console.navigation import Ctx, busy, go
from eawf.surfaces.tui.console.renderers.read_model import native, native_frame

_KEYS: tuple[tuple[str, str], ...] = (
    ("↑↓", "rung"),
    ("Enter", "what it found"),
    ("y", "copy"),
    ("Esc", "back"),
)


def _prose(wide: bool, x: bool, room: int) -> list[str]:
    rows = prose(
        "IN WORDS",
        [
            "Replay any recorded log and every event arrives in the order it was recorded"
            " — no inversions, and no gaps in the sequence."
        ],
        room,
    )
    proves = (
        "A replayed run reads exactly like the live one, which is what lets Activity, the"
        " Timeline and History be rebuilt from the log at all."
        if wide
        else "A replay can be read exactly like the live run."
    )
    rows += prose("IT PROVES", [proves], room)
    rows += prose(
        "BREAKS IF",
        [
            "One replay puts two events out of order, or a gap is closed by inventing an event"
            " instead of reconciling it."
        ],
        room,
    )
    if x:
        rows += prose(
            "WHO CARES",
            [
                "Every operator reading a rebuilt Timeline, and every verdict a Run signs off"
                " from replayed evidence."
            ],
            room,
        )
    return rows


def _ladder(wide: bool, room: int) -> list[str]:
    first = "Each rung is a harder test than the one below it, and only the top rung certifies."
    ladder = (
        [first, "A claim with three passes is still an uncertified claim."] if wide else [first]
    )
    standing = (
        [
            "This claim stands uncertified.",
            "Rung 3 has no outcome — unknown, not failed — so nothing here has been retracted.",
        ]
        if wide
        else ["This claim stands uncertified.", "Rung 3 has no outcome — unknown, not failed."]
    )
    return prose("LADDER", ladder, room) + prose("STANDING", standing, room)


def render(view: View) -> list[str]:
    """Return the Evidence frame, native when a read model is held."""
    model = native(view)
    if model is not None:
        return native_frame(view, model)
    s, w = view.session, view.w
    rungs = view.fixture.registers.ev_rungs
    wide = w >= 120
    x = w >= 160
    room = w - 15
    if x:
        grid = Grid([13, 12, 38, 24, 0])
        heads = ["RUNG", "OUTCOME", "WHAT IT CHECKED", "IT RAN OVER", "AS OF"]
    elif wide:
        grid = Grid([13, 12, 38, 0])
        heads = ["RUNG", "OUTCOME", "WHAT IT CHECKED", "AS OF"]
    else:
        grid = Grid([13, 12, 0])
        heads = ["RUNG", "OUTCOME", "WHAT IT CHECKED"]
    dv.sel_in(s, len(rungs))
    body = [lab("CLAIM", "CLM-0004 · the normalizer preserves event ordering under replay")]
    body += _prose(wide, x, room)
    body.append(lab("SUPPORTS", "MLS-0007 · 3 claims in graph · 1 uncertified"))
    body.append(thin(w))
    body.append(grid.head(heads))
    for i, r in enumerate(rungs):
        cells = [r["n"], chip(r["sev"], r["out"]), r["checked"]]
        if x:
            cells.append(r["over"])
        if wide:
            cells.append(r["as"])
        body.append(grid.row(cells, i == s.sel, w))
    body.append(thin(w))
    body += _ladder(wide, room)
    if wide:
        body.append(thin(w))
        body.append(lab("GRAPH", "CLM-0004 ← CLM-0002 probe evidence ← EVT-0119"))
    return g_frame(
        view,
        crumb="Eä ▸ eawf-core ▸ Evidence ▸ CLM-0004",
        ctx=f"Claim CLM-0004 · {len(rungs)} rungs · uncertified · as of 14:02",
        body=body,
        keys=_KEYS,
    )


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Open the rung card on Enter; the rung is captured when the card opens."""
    s = ctx.s
    if s.route != "evidence" or key != "Enter" or busy(s):
        return False
    s.rung = s.sel
    go(ctx, "evidence.digest", f"rung {s.rung + 1} · its input digest and what it found")
    return True
