"""timeline: three lanes with a marker cursor of their own, then the undated and releases regions.

The legend rides the keybar where it fits and takes the row above it elsewhere. One lane
table serves the whole console: the route draws its rows, the marker cursor counts its
milestones from the label row, and the marker card reads its glyph from the same bar, so
the two can never disagree about what a marker commits to.
"""

from __future__ import annotations

import re

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import (
    Fixed,
    Table,
    View,
    bar,
    build,
    header,
    route_keys_bar,
    thin,
)
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.renderers.spine import held, native_frame
from eawf.surfaces.tui.console.width import cell_len, pad

Lane = tuple[str, str, str]
TL_LANES: tuple[Lane, ...] = (
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
LANES = "LANES"
UNDATED = "UNDATED"
RELEASES = "RELEASES"
REGIONS: tuple[str, ...] = (LANES, UNDATED, RELEASES)
DATED = "●"
FORECAST = "○"

_LANE = 12
_LABEL_OFFSET = 9
_WEEKS = "            W26     W27     W28     W29  │  W30     W31     W32"
_LEGEND = "● dated  ○ forecast  ┄ uncertain  │ now  ▣ release"
UNDATED_ROWS: tuple[tuple[str, str, str], ...] = (
    ("MLS-0012", "Calibration follow-up", "no date proposed yet"),
    ("MLS-0014", "Snapshot retention review", "waits on MLS-0011"),
)
RELEASE_ROWS: tuple[tuple[str, str, str, str], ...] = (
    ("REL-0001", "v0.7.0-rc1", "W29", "CANDIDATE"),
    ("REL-0000", "v0.6.4", "W26", "PUBLISHED"),
)
_ID4 = re.compile(r"\d{4}")
_MARK = re.compile("[●○]")
_DIGIT = re.compile(r"\d")


def region_rows(region: str) -> tuple[tuple[str, ...], ...]:
    """Return the rows region ``region`` lists; the lanes list none."""
    if region == UNDATED:
        return UNDATED_ROWS
    if region == RELEASES:
        return RELEASE_ROWS
    return ()


def lane_glyphs(lane: str) -> list[str]:
    """Return the marker glyphs lane ``lane`` draws, in label order.

    Raises:
        ValueError: the lane draws a different number of markers than it labels.
    """
    row = next((r for r in TL_LANES if r[0] == lane), None)
    if row is None:
        return []
    glyphs = [ch for ch in row[1] if ch in (DATED, FORECAST)]
    labels = _ID4.findall(row[2])
    if len(glyphs) != len(labels):
        raise ValueError(f"{lane} draws {len(glyphs)} markers but labels {len(labels)}")
    return glyphs


def marker_glyph(lane: str, ix: int) -> str:
    """Return the glyph of marker ``ix`` on ``lane``, clamped to the lane's markers."""
    glyphs = lane_glyphs(lane)
    if not glyphs:
        return DATED
    return glyphs[min(ix, len(glyphs) - 1)]


def lane_name(ix: int) -> str:
    """Return the name of lane ``ix``, the first lane past the end."""
    return TL_NAMES[ix] if ix < len(TL_NAMES) else TL_NAMES[0]


def _marker_ids(lane: Lane) -> list[str]:
    return [f"MLS-{m.group(0)}" for m in _ID4.finditer(lane[2])]


def _check_lane(lane: Lane, now_col: int) -> None:
    """Refuse a lane whose markers or keyline drift off its labels or the week header.

    Raises:
        ValueError: a marker, its label or the keyline sits in the wrong column.
    """
    mark, digit = _MARK.search(lane[1]), _DIGIT.search(lane[2])
    marker_col = _LANE + (mark.start() if mark else -1)
    label_col = _LABEL_OFFSET + (digit.start() if digit else -1)
    if marker_col != label_col:
        raise ValueError(f"{lane[0]} label at {label_col} but marker at {marker_col}")
    keyline = _LANE + lane[1].index("┼")
    if keyline != now_col:
        raise ValueError(f"{lane[0]} keyline at {keyline} but header │ at {now_col}")


def _lanes(view: View) -> list[str]:
    s, w = view.session, view.w
    rows: list[str] = []
    now_col = _WEEKS.index("│")
    on_lanes = (s.tl_reg or LANES) == LANES
    focus = _marker_ids(TL_LANES[s.sel] if s.sel < len(TL_LANES) else TL_LANES[0])
    s.mark = max(0, min(s.mark, len(focus) - 1))
    s.timeline_marks = len(focus)
    s.timeline_marker = focus[s.mark] if focus else None
    for i, lane in enumerate(TL_LANES):
        _check_lane(lane, now_col)
        on = on_lanes and i == s.sel
        rows.append(Fixed(pad(("▸" if on else " ") + pad(lane[0], _LANE - 1) + lane[1], w)))
        labels = " " * _LABEL_OFFSET + lane[2]
        rows.append(Fixed(pad(labels, w)) if on and focus else labels)
    return rows


def _regions(view: View) -> list[str]:
    s, w = view.session, view.w
    region = s.tl_reg or LANES
    listed = region_rows(region)
    if listed:
        s.tl_sel = max(0, min(len(listed) - 1, s.tl_sel))
    undated = Table([12, 11, 26, 0], 2)
    releases = Table([12, 13, 11, 11, 0], 2)
    rows = [thin(w), f" UNDATED     {len(UNDATED_ROWS)} milestones with no date proposed"]
    rows.extend(
        undated.row(["", *u], region == UNDATED and i == s.tl_sel)
        for i, u in enumerate(UNDATED_ROWS)
    )
    candidates = sum(1 for r in RELEASE_ROWS if r[3] == "CANDIDATE")
    published = sum(1 for r in RELEASE_ROWS if r[3] == "PUBLISHED")
    rows.append(thin(w))
    rows.append(f" RELEASES    {candidates} candidate · {published} published")
    rows.extend(
        releases.row(["", "▣ " + r[0], r[1], r[2], r[3]], region == RELEASES and i == s.tl_sel)
        for i, r in enumerate(RELEASE_ROWS)
    )
    rows.extend([thin(w), " ┊ Runtime MLS-0002 waits on Trust MLS-0011", thin(w)])
    s.tl_regs = {name: [list(r) for r in region_rows(name)] for name in (UNDATED, RELEASES)}
    return rows


def render(view: View) -> list[str]:
    """Return the Timeline frame, native when a read model is held."""
    spine = held(view)
    if spine is not None:
        return native_frame(view, spine)
    s, fx, w, h = view.session, view.fixture, view.w, view.h
    dv.sel_in(s, len(TL_LANES))
    rows = [
        header(view, f" Eä ▸ {fx.scope} ▸ Timeline"),
        " W26 → W32 · now W29 · 8 of 26 weeks in view",
        bar(w),
        _WEEKS,
    ]
    rows.extend(_lanes(view))
    rows.extend(_regions(view))
    keys = route_keys_bar(view, ROUTE_KEYS["timeline"])
    left = keys.rstrip()
    if cell_len(left) + 2 + cell_len(_LEGEND) <= w:
        gap = w - cell_len(left) - cell_len(_LEGEND) - 1
        foot = left + " " * gap + _LEGEND + " "
    else:
        rows.extend("" for _ in range(h - 2 - len(rows)))
        rows.append("   " + _LEGEND)
        foot = keys
    return build(view, rows, foot)
