"""campaign.step: one step of a campaign.

A running step is watched (runner, progress, the tail of its activity); a finished step is
read (when it ran, what it produced); a blocked step says what it waits on and shows no
activity. Two regions share one pair of arrows, and Tab says which of them the arrows walk.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import (
    CHIP_END,
    LABEL_MARK,
    Fixed,
    Grid,
    View,
    chip,
    g_frame,
    g_row,
    lab,
    prose,
    strip_chips,
    thin,
)
from eawf.surfaces.tui.console.navigation import Ctx, busy, go
from eawf.surfaces.tui.console.renderers.campaign import CLAIM, window
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.width import pad

HISTORY = "HISTORY"
PRODUCED = "PRODUCED"
_PRODUCTS = Grid([12, 22, 0])
_ACTIVITY = Grid([12, 8, 0])


def step_index(session: Session, fixture: Fixture) -> int:
    """Return the step the card was opened on, the last step past the end."""
    return min(session.cam_step, len(fixture.registers.cam_steps) - 1)


def _waits_on(step: Sequence[Any], detail: Mapping[str, Any]) -> str:
    """Return a dependency list as a sentence: which steps, and whether they are done."""
    if step[3] == "–":  # noqa: RUF001
        return "Nothing — it could start at once."
    ids = str(step[3]).split(" · ")
    several = len(ids) > 1
    names = f"Steps {', '.join(ids[:-1])} and {ids[-1]}" if several else f"Step {ids[0]}"
    if detail["state"] == "blocked":
        return names + (" are not finished yet." if several else " is not finished yet.")
    return names + (" are finished." if several else " is finished.")


def _when(detail: Mapping[str, Any]) -> str:
    if detail["state"] == "running":
        return f"started {detail['started']}"
    if detail["state"] == "done":
        return f"{detail['started']} → {detail['ended']}"
    return "never started"


def _receded(text: str, w: int) -> Fixed:
    return Fixed(pad(strip_chips(text), w))


def _head_row(name: str, head: str) -> str:
    return lab(name, "") + LABEL_MARK + head[13:] + CHIP_END


def _activity(view: View, acts: list[Any], on_history: bool, running: bool) -> list[str]:
    s, w = view.session, view.w
    if not acts:
        return [lab("ACTIVITY", "∅ Nothing to show — the step has not started.")]
    head = _ACTIVITY.head(["", "AT", "WHAT HAPPENED"])
    rows = [_head_row("ACTIVITY" if running else "HISTORY", head)]
    cap = 8 if w >= 160 else 5 if w >= 120 else 2
    win = window(len(acts), cap, s.hist_sel if on_history else 0, on_history)
    if win.above:
        rows.append(_receded(_ACTIVITY.row(["", "", f"… {win.above} earlier"], False, w), w))
    for k, act in enumerate(acts[win.start : win.start + win.take]):
        row = _ACTIVITY.row(["", act[0], act[1]], False, w)
        if on_history and win.start + k == s.hist_sel:
            row = row[:13] + "▸" + row[14:]
        rows.append(g_row(row, w))
    if win.below:
        rows.append(_receded(_ACTIVITY.row(["", "", f"… {win.below} later"], False, w), w))
    return rows


def _products(view: View, made: list[str], on_history: bool, running: bool) -> list[str]:
    s, w = view.session, view.w
    if not made:
        still = "it is still running." if running else "it has not started."
        return [lab(PRODUCED, f"∅ None yet · {still}")]
    rows = [_head_row(PRODUCED, _PRODUCTS.head(["", "WHAT IT MADE", "KIND"]))]
    for k, item in enumerate(made):
        kind = (
            f"receipt · supports {CLAIM}"
            if item.startswith("EVD-")
            else "artifact · kept with CAM-0001"
        )
        row = _PRODUCTS.row(["", item, kind], False, w)
        if not on_history and k == s.sel:
            row = row[:13] + "▸" + row[14:]
        rows.append(g_row(row, w))
    return rows


def render(view: View) -> list[str]:
    """Return the campaign step card."""
    s, fx, w = view.session, view.fixture, view.w
    reg = fx.registers
    i = step_index(s, fx)
    step = reg.cam_steps[i]
    detail = reg.cam_step_detail[i]
    made = list(step[4])
    dv.sel_in(s, max(1, len(made)))
    running = detail["state"] == "running"
    body = [
        lab("STEP", f"{step[0]} · {chip(step[2], step[1])} · {_when(detail)}"),
        lab("WAITS ON", _waits_on(step, detail)),
        lab(
            "RUNNER",
            f"{detail['run']} · {detail['prov']}" + (" · session fresh" if running else ""),
        ),
        lab("SPENT", step[5]),
        thin(w),
    ]
    heading = "PROGRESS" if running else "OUTCOME" if detail["state"] == "done" else "WAITING"
    body += prose(heading, [detail["sum"]], w - 15)
    body.append(thin(w))
    acts = list(detail["acts"])
    on_history = (s.step_reg or HISTORY) == HISTORY and bool(acts)
    body += _activity(view, acts, on_history, running)
    body.append(thin(w))
    body += _products(view, made, on_history, running)
    keys: list[tuple[str, str]] = [("Tab", "region")] if acts and made else []
    if on_history:
        keys.append(("↑↓", "line"))
    elif made:
        keys.extend([("↑↓", "product"), ("Enter", "open")])
    keys.extend([("y", "copy"), ("Esc", "back")])
    n = str(step[0]).split(" ")[0]
    return g_frame(
        view,
        crumb=f"Eä ▸ eawf-core ▸ Research ▸ CAM-0001 ▸ Step {n}",
        ctx=f"Campaign CAM-0001 · step {n} of {len(reg.cam_steps)} · as of 14:02",
        body=body,
        keys=keys,
    )


def copy(session: Session, fixture: Fixture) -> str:
    """Return the step's stable URN."""
    n = str(fixture.registers.cam_steps[step_index(session, fixture)][0]).split(" ")[0]
    return f"urn:eawf:{fixture.scope}:CAM-0001:step:{n}"


def _region_key(ctx: Ctx, key: str, acts: list[Any], made: list[Any]) -> bool:
    """Swap regions on Tab and walk the history on the arrows."""
    s = ctx.s
    if key == "Tab":
        if not acts or not made:
            ctx.log("Tab", "this step has only one region to walk")
            return True
        s.step_reg = PRODUCED if (s.step_reg or HISTORY) == HISTORY else HISTORY
        ctx.log("Tab", f"region → {s.step_reg}")
        return True
    if (s.step_reg or HISTORY) == HISTORY and acts and key in ("ArrowDown", "ArrowUp"):
        step = 1 if key == "ArrowDown" else -1
        s.hist_sel = max(0, min(len(acts) - 1, s.hist_sel + step))
        act = acts[s.hist_sel]
        ctx.log(key, f"{act[0]} · {' '.join(str(act[1]).split())[:40]}")
        return True
    return False


def _open_product(ctx: Ctx, step: Sequence[Any]) -> bool:
    """Open the product under the cursor: an artifact as text, a receipt as its claim."""
    s = ctx.s
    item = step[4][s.sel] if s.sel < len(step[4]) else None
    if not item:
        ctx.log("Enter", f"{step[0]} has produced nothing to open yet")
        return True
    if str(item).startswith("EVD-"):
        go(ctx, "evidence", f"{item} · the claim it supports", CLAIM)
        return True
    arts = ctx.fixture.registers.cam_art
    s.artifact = max((n for n, a in enumerate(arts) if a["f"] == item), default=0)
    s.art_scroll = 0
    go(ctx, "campaign.artifact", f"{step[0]} wrote {item}")
    return True


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Handle the step card's regions and open a product from the step that made it."""
    s = ctx.s
    if s.route != "campaign.step":
        return False
    reg = ctx.fixture.registers
    i = step_index(s, ctx.fixture)
    acts = list(reg.cam_step_detail[i]["acts"])
    made = list(reg.cam_steps[i][4])
    if not busy(s) and _region_key(ctx, key, acts, made):
        return True
    if key == "Enter" and not busy(s) and (s.step_reg or HISTORY) == PRODUCED:
        return _open_product(ctx, reg.cam_steps[i])
    return False
