"""timeline: three lanes with a marker cursor of their own, then the UNDATED and RELEASES
regions; the legend rides the keybar where it fits and keeps the row above elsewhere."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.frame import TBL, Fixed, bar, bar_, build, header_row, thin
from ...chassis.keys import ROUTE_KEYS
from ...chassis.width import cell_len, pad

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

# One lane fixture for the whole console: the route draws these rows, the marker cursor
# counts its milestones from the label row, and the card reads its glyph from the same bar,
# so the two can never disagree about what a marker commits to.
TL_LANES: tuple[tuple[str, str, str], ...] = (
    (
        "Runtime",
        "●───────●───────●───────●────┼───○┄┄┄┄┄┄○",
        "   0000    0001    0003    0002 │   0006   0010",
    ),
    (
        "Trust",
        "────────●───────────────●────┼─────────────────○",
        "           0004            0011 │                 0008",
    ),
    ("Research", "──────────○┄┄┄┄┄┄┄┄┄┄┄┄┄─────┼", "             0007               │"),
)
TL_NAMES: tuple[str, ...] = tuple(lane[0] for lane in TL_LANES)

_LANE = 12
_LABEL_OFFSET = 9
_WEEKS = "            W26     W27     W28     W29  │  W30     W31     W32"
_LEG = "● dated  ○ forecast  ┄ uncertain  │ now  ▣ release"
_UND: tuple[tuple[str, str, str], ...] = (
    ("MLS-0012", "Calibration follow-up", "no date proposed yet"),
    ("MLS-0014", "Snapshot retention review", "waits on MLS-0011"),
)
_RELS: tuple[tuple[str, str, str, str], ...] = (
    ("REL-0001", "v0.7.0-rc1", "W29", "CANDIDATE"),
    ("REL-0000", "v0.6.4", "W26", "PUBLISHED"),
)
_ID4 = re.compile(r"\d{4}")
_MARK = re.compile("[●○]")
_DIGIT = re.compile(r"\d")


def lane_of(ix: int) -> tuple[str, str, str]:
    return TL_LANES[min(ix or 0, len(TL_LANES) - 1)]


def lane_glyphs(lane: str) -> list[str]:
    """The glyphs the bar draws, in label order, guarded against the label count."""
    row = next((lane_row for lane_row in TL_LANES if lane_row[0] == lane), None)
    if row is None:
        return []
    glyphs = [ch for ch in row[1] if ch in ("●", "○")]
    labels = _ID4.findall(row[2])
    if len(glyphs) != len(labels):
        raise ValueError(f"{lane} draws {len(glyphs)} markers but labels {len(labels)}")
    return glyphs


def marker_glyph(lane: str, ix: int) -> str:
    glyphs = lane_glyphs(lane)
    if not glyphs:
        return "●"
    return glyphs[min(ix or 0, max(0, len(glyphs) - 1))]


def _markers_of(lane: tuple[str, str, str]) -> list[dict[str, object]]:
    return [
        {"id": f"MLS-{m.group(0)}", "at": _LABEL_OFFSET + m.start()} for m in _ID4.finditer(lane[2])
    ]


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    dv.sel_in(s, 3)
    rows: list[str] = [
        header_row(s, fixture, f" Eä ▸ {fixture.scope} ▸ Timeline", w),
        " W26 → W32 · now W29 · 8 of 26 weeks in view",
        bar(w),
        _WEEKS,
    ]
    now_col = _WEEKS.index("│")
    on_lanes = (s.tl_reg or "LANES") == "LANES"
    focus = _markers_of(TL_LANES[s.sel] if s.sel < len(TL_LANES) else TL_LANES[0])
    if s.mark is None or s.mark < 0:
        s.mark = 0
    if s.mark >= len(focus):
        s.mark = max(0, len(focus) - 1)
    s.timeline_marks = len(focus)
    s.timeline_marker = str(focus[s.mark]["id"]) if focus else None
    for i, lane in enumerate(TL_LANES):
        marker_col = _LANE + _MARK.search(lane[1]).start()
        label_col = _LABEL_OFFSET + _DIGIT.search(lane[2]).start()
        if marker_col != label_col:
            raise ValueError(f"{lane[0]} label at {label_col} but marker at {marker_col}")
        if _LANE + lane[1].index("┼") != now_col:
            raise ValueError(
                f"{lane[0]} keyline at {_LANE + lane[1].index('┼')} but header │ at {now_col}"
            )
        on = on_lanes and i == s.sel
        rows.append(Fixed(pad(("▸" if on else " ") + pad(lane[0], _LANE - 1) + lane[1], w)))
        lab_raw = " " * _LABEL_OFFSET + lane[2]
        # the focused marker is only coloured in the prototype: the text is the same either way
        rows.append(Fixed(pad(lab_raw, w)) if on and focus else lab_raw)
    reg = s.tl_reg or "LANES"
    reglist = _UND if reg == "UNDATED" else _RELS if reg == "RELEASES" else None
    if reglist:
        s.tl_sel = max(0, min(len(reglist) - 1, s.tl_sel or 0))
    ut = TBL([12, 11, 26, 0], 2)
    rt = TBL([12, 13, 11, 11, 0], 2)
    rows.append(thin(w))
    rows.append(f" UNDATED     {len(_UND)} milestones with no date proposed")
    for i, u in enumerate(_UND):
        rows.append(ut.row(["", u[0], u[1], u[2]], reg == "UNDATED" and i == (s.tl_sel or 0)))
    rows.append(thin(w))
    cand = sum(1 for r in _RELS if r[3] == "CANDIDATE")
    pub = sum(1 for r in _RELS if r[3] == "PUBLISHED")
    rows.append(f" RELEASES    {cand} candidate · {pub} published")
    for i, r in enumerate(_RELS):
        rows.append(
            rt.row(["", "▣ " + r[0], r[1], r[2], r[3]], reg == "RELEASES" and i == (s.tl_sel or 0))
        )
    rows.append(thin(w))
    rows.append(" ┊ Runtime MLS-0002 waits on Trust MLS-0011")
    rows.append(thin(w))
    s.tl_regs = {"UNDATED": [list(u) for u in _UND], "RELEASES": [list(r) for r in _RELS]}
    kb = bar_(s, list(ROUTE_KEYS["timeline"]), w)
    left = kb.rstrip()
    if cell_len(left) + 2 + cell_len(_LEG) <= w:
        gap = w - cell_len(left) - cell_len(_LEG) - 1
        foot = left + " " * gap + _LEG + " "
    else:
        while len(rows) < h - 2:
            rows.append("")
        rows.append("   " + _LEG)
        foot = kb
    return build(s, rows, foot, w, h)
