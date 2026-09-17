"""campaign: the research campaign as three windowed sections, one walkable at a time.

Each section shows a window of its list and counts what is off either end, so no section
may lie about its length; the focused window follows the cursor and the others show their
head.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import (
    Fixed,
    Grid,
    View,
    chip,
    g_frame,
    g_pad,
    g_row,
    strip_chips,
    thin,
)
from eawf.surfaces.tui.console.navigation import Ctx, busy, go
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.width import pad

PLAN = "PLAN"
EVIDENCE = "EVIDENCE"
ARTIFACTS = "ARTIFACTS"
CLAIM = "CLM-0004"
_GUTTER = 13
_KEYS: tuple[tuple[str, str], ...] = (
    ("↑↓", "row"),
    ("Tab", "section"),
    ("Enter", "open"),
    (".", "actions"),
    ("i", "inspect"),
    ("Esc", "back"),
)


@dataclass(frozen=True, slots=True)
class Win:
    """A window onto a list: its first shown index, how many, and what hides each side."""

    start: int
    take: int
    above: int
    below: int


def section_list(session: Session, fixture: Fixture) -> tuple[Any, ...]:
    """Return the list the focused section walks."""
    reg = fixture.registers
    sec = session.cam_sec or PLAN
    return reg.cam_steps if sec == PLAN else reg.cam_evid if sec == EVIDENCE else reg.cam_art


def caps(w: int) -> dict[str, int]:
    """Return each section's row cap; the smallest windowable section is three rows."""
    if w >= 160:
        return {PLAN: 6, EVIDENCE: 5, ARTIFACTS: 5}
    if w >= 120:
        return {PLAN: 6, EVIDENCE: 4, ARTIFACTS: 3}
    return {PLAN: 3, EVIDENCE: 3, ARTIFACTS: 3}


def window(total: int, cap: int, sel: int, focused: bool) -> Win:
    """Return the window onto a list of ``total`` rows.

    A single hidden row is shown rather than announced, and the cursor never leaves its
    own window.
    """
    if total <= cap:
        return Win(0, total, 0, 0)
    sel = max(0, min(total - 1, sel))
    max_start = max(0, total - (cap - 1))

    def fit(start: int) -> Win:
        start = 0 if start == 1 else start
        marker = 1 if start > 0 else 0
        take = cap - marker
        if total - start - take > 0:
            take = cap - marker - 1
        return Win(start, take, start, max(0, total - start - take))

    win = fit(max(0, min(sel - (cap - 2) // 2, max_start)) if focused else 0)
    if focused and sel >= win.start + win.take:
        win = fit(max(0, min(sel - (win.take - 1), max_start)))
    return win


def made_cell(step: Sequence[Any]) -> str:
    """Return what a step made as one cell: the count is the truth, the name a sample."""
    made: Sequence[str] = step[4]
    if made:
        return made[0] + (f" +{len(made) - 1}" if len(made) > 1 else "")
    return "– still running" if "running" in step[1] else "– not started"  # noqa: RUF001


@dataclass(frozen=True, slots=True)
class Section:
    """One section's name, grid, head cells and the cells each of its rows shows."""

    name: str
    grid: Grid
    heads: list[str]
    cells: Callable[[Any], list[str]]


def _sections(w: int, fixture: Fixture) -> list[tuple[Section, Sequence[Any], str]]:
    reg = fixture.registers
    x, wide = w >= 160, w >= 120
    steps, evid, arts = reg.cam_steps, reg.cam_evid, reg.cam_art
    done = sum(1 for st in steps if "done" in st[1])
    if x:
        plan = Section(
            PLAN,
            Grid([12, 24, 13, 13, 18, 0]),
            ["STEP", "STATE", "DEPENDS ON", "PRODUCED", "SPENT"],
            lambda st: [st[0], chip(st[2], st[1]), st[3], made_cell(st), st[5]],
        )
        proof = Section(
            EVIDENCE,
            Grid([12, 11, 26, 20, 13, 0]),
            ["RECEIPT", "WHAT IT SHOWS", "CLAIM", "SUPPORT", "DETAIL"],
            lambda e: list(e[:5]),
        )
    elif wide:
        plan = Section(
            PLAN,
            Grid([12, 24, 13, 13, 0]),
            ["STEP", "STATE", "DEPENDS ON", "PRODUCED"],
            lambda st: [st[0], chip(st[2], st[1]), st[3], made_cell(st)],
        )
        proof = Section(
            EVIDENCE,
            Grid([12, 11, 26, 20, 0]),
            ["RECEIPT", "WHAT IT SHOWS", "CLAIM", "SUPPORT"],
            lambda e: list(e[:4]),
        )
    else:
        plan = Section(
            PLAN,
            Grid([12, 20, 12, 0]),
            ["STEP", "STATE", "DEPENDS ON"],
            lambda st: [st[0], chip(st[2], st[1]), st[3]],
        )
        proof = Section(
            EVIDENCE,
            Grid([12, 11, 26, 0]),
            ["RECEIPT", "WHAT IT SHOWS", "CLAIM"],
            lambda e: list(e[:3]),
        )
    files = Section(
        ARTIFACTS, Grid([12, 22, 0]), ["ARTIFACT", "WRITTEN"], lambda a: [a["f"], a["at"]]
    )
    return [
        (plan, steps, f"{done} of {len(steps)} steps done · 1 running · 1 blocked by step 5"),
        (proof, evid, f"{len(evid)} receipts · 1 contradiction open · 2 claims still uncertified"),
        (files, arts, f"{len(arts)} files · 1 finding promoted to MLS-0007 · 2 held"),
    ]


def _receded(text: str, w: int) -> Fixed:
    return Fixed(pad(strip_chips(text), w))


def _section_rows(view: View, section: Section, rows: Sequence[Any], summary: str) -> list[str]:
    """Return one section: its summary row, head, window and edge markers."""
    s, w = view.session, view.w
    on = (s.cam_sec or PLAN) == section.name
    out: list[str] = [Fixed(g_pad(g_pad(" " + section.name, _GUTTER) + summary, w))]
    if section.name == PLAN and w >= 160:
        span = "─ ✓ 3 ───"
        out.append(g_pad(" GRAPH", _GUTTER) + "✓ 1 ─┐" + " " * len(span) + "┌─ ✓ 4 ─┐")
        out.append(g_pad("", _GUTTER) + "✓ 2 ─┴" + span + "┴" + "───────" + "┴─ ► 5 ─── ○ 6")
    elif section.name == PLAN and w >= 120:
        out.append(g_pad(" GRAPH", _GUTTER) + "✓ 1 · ✓ 2 → ✓ 3 → ✓ 4 → ► 5 → ○ 6")
    out.append(section.grid.head(["", *section.heads]))
    win = window(len(rows), caps(w)[section.name], s.sel, on)
    if win.above:
        out.append(_receded(section.grid.row(["", f"… {win.above} above"], False, w), w))
    for k, item in enumerate(rows[win.start : win.start + win.take]):
        row = section.grid.row(["", *section.cells(item)], False, w)
        if on and win.start + k == s.sel:
            # the cursor sits one column before the first cell, not twelve clear in the gutter
            row = row[:13] + "▸" + row[14:]
        out.append(g_row(row, w) if on else _receded(row, w))
    if win.below:
        out.append(_receded(section.grid.row(["", f"… {win.below} below"], False, w), w))
    return out


def render(view: View) -> list[str]:
    """Return the Campaign frame."""
    s, fx, w = view.session, view.fixture, view.w
    x, wide = w >= 160, w >= 120
    dv.sel_in(s, len(section_list(s, fx)))
    body = [
        " QUESTION    Does provider drift change replay digests?",
        " BOUNDS      ≤20 runs · ≤8h · 2 providers · stop at first contradiction",
    ]
    if x:
        body.append(" STOP RULE   Armed — one contradiction is open, so step 6 may not start.")
    for section, rows, summary in _sections(w, fx):
        body.append(thin(w))
        body += _section_rows(view, section, rows, summary)
        if section.name == EVIDENCE and wide:
            body.append(
                g_pad(" CONFLICT", _GUTTER) + "EVD-0011 and EVD-0014 disagree about the same task"
            )
        if section.name == EVIDENCE and x:
            body.append(
                g_pad("", _GUTTER)
                + "the bound says stop · step 6 stays blocked until one is retracted"
            )
    if x:
        body.append(thin(w))
        body.append(
            " NEXT        Step 5 finishes, then the conflict is settled before the write-up."
        )
        body.append(" RECORD      Every artifact is kept with CAM-0001 · digests in each card")
    return g_frame(
        view,
        crumb="Eä ▸ eawf-core ▸ Research ▸ CAM-0001",
        ctx="Campaign CAM-0001 Provider drift · REVIEW · W30 · ~6.2 of 8h · 14 of 20 runs",
        body=body,
        keys=_KEYS,
    )


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Cycle the sections on Tab and open what the focused section holds on Enter."""
    s = ctx.s
    reg = ctx.fixture.registers
    if s.route != "campaign" or busy(s):
        return False
    if key == "Tab":
        sections = list(reg.cam_sects)
        at = sections.index(s.cam_sec or PLAN)
        s.cam_sec = sections[(at + (len(sections) - 1 if shift else 1)) % len(sections)]
        s.sel = 0
        ctx.log("Tab", f"section → {s.cam_sec}")
        return True
    if key != "Enter":
        return False
    sec = s.cam_sec or PLAN
    i = s.sel
    if sec == ARTIFACTS:
        s.artifact = i
        s.art_scroll = 0
        go(ctx, "campaign.artifact", f"artifact {reg.cam_art[i]['f']}")
        return True
    if sec == EVIDENCE:
        receipt = reg.cam_evid[i]
        go(ctx, "evidence", f"receipt {receipt[0]} · the claim it supports", receipt[5])
        return True
    # a step is a place: Enter opens the step, not one of the things it made; the row is
    # zeroed after the jump so the back stack keeps the row Esc returns to
    s.cam_step = i
    go(ctx, "campaign.step", f"step {reg.cam_steps[i][0]}")
    s.sel = 0
    return True
