"""timeline: three lanes with a marker cursor of their own, then the undated and releases regions.

The legend rides the keybar where it fits and takes the row above it elsewhere. One lane
table serves the whole console: the route draws its rows, the marker cursor counts its
milestones from the label row, and the marker card reads its glyph from the same bar, so
the two can never disagree about what a marker commits to.

The native frame is the same chart over the read model: one lane per Track across the
weeks around now, then the Milestones no date places in the UNDATED region, then the
release register. A Milestone is placed on its lane only by the target date its record
states, in the ISO week of that date, and a closed one is drawn done; a date beyond the
drawn weeks is named at the lane's end rather than dropped, and a Milestone with no date
is listed as undated rather than drawn where nobody dated it.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta

from eawf.kernel.projection.spine import SpineRow, SpineView
from eawf.kernel.store.tiers import Epoch2Collection
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
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS, keybar
from eawf.surfaces.tui.console.keymap import native_keys
from eawf.surfaces.tui.console.mutation import open_target
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.renderers.children import status
from eawf.surfaces.tui.console.renderers.read_model import (
    UNKNOWN_WORD,
    label,
    native_head,
    route_crumb,
)
from eawf.surfaces.tui.console.renderers.spine import held
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
# The native chart draws solid lanes crossed by a plain keyline, so it states no dotted run.
_NATIVE_LEGEND = "● dated  ✓ done  ○ forecast  │ now  ▣ release"
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
        bar_row = ("▸" if on else " ") + pad(lane[0], _LANE - 1) + lane[1]
        if on and focus:
            bar_row = marked_lane(bar_row, s.mark)
        rows.append(Fixed(pad(bar_row, w)))
        labels = " " * _LABEL_OFFSET + lane[2]
        rows.append(Fixed(pad(labels, w)) if on and focus else labels)
    if on_lanes and focus:
        lane = TL_LANES[s.sel] if s.sel < len(TL_LANES) else TL_LANES[0]
        rows.append(marker_row(lane[0], focus, s.mark))
    return rows


def marked_lane(row: str, mark: int) -> str:
    """Return a lane row with ``[`` and ``]`` around its ``mark``-th marker.

    The marker cursor is text, never colour alone: the brackets take the cells either side
    of the marker, which are the lane's own line or the gutter, so no column moves.
    """
    found = [m.start() for m in _MARK.finditer(row, _LANE)]
    if not found:
        return row
    at = found[min(max(mark, 0), len(found) - 1)]
    return f"{row[: at - 1]}[{row[at]}]{row[at + 2 :]}"


def marker_row(lane: str, milestones: list[str], mark: int) -> str:
    """Return the ``MARKER`` row: the focused milestone, its lane and its position on it."""
    at = min(max(mark, 0), len(milestones) - 1)
    return f" MARKER    {milestones[at]} · {lane} · {at + 1} of {len(milestones)}"


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


def _with_legend(view: View, rows: list[str], keys: str, legend: str) -> list[str]:
    """Return the frame with ``legend`` on the keybar where it fits, above it elsewhere."""
    w, h = view.w, view.h
    left = keys.rstrip()
    if cell_len(left) + 2 + cell_len(legend) <= w:
        gap = w - cell_len(left) - cell_len(legend) - 1
        foot = left + " " * gap + legend + " "
    else:
        rows.extend("" for _ in range(h - 2 - len(rows)))
        rows.append("   " + legend)
        foot = keys
    return build(view, rows, foot)


#: The weeks the chart draws either side of the week now falls in.
_SPAN = 3
#: The cells one week takes on the chart.
_WEEK_W = 8
#: Where in its week's cells a marker sits: under the middle of the week's label.
_MARK_AT = 1
#: What an undated Milestone's row says about its date.
NO_DATE = "no date proposed yet"
#: The marker of a closed Milestone: it is done, whichever way it closed.
DONE = "✓"
#: The fact a Milestone row states its target date in, as ``YYYY-MM-DD``.
TARGET_FACT = "target_date"
_CLOSED = frozenset({"COMPLETED", "CANCELLED"})


def _of(spine: SpineView, collection: Epoch2Collection) -> list[SpineRow]:
    return [row for row in spine.rows if row.collection is collection]


def target_of(row: SpineRow) -> date | None:
    """Return the day a Milestone row is aimed at, or ``None`` when it states none."""
    stated = row.facts.get(TARGET_FACT)
    return None if stated is None else date.fromisoformat(stated)


def week_offset(day: date, now: datetime) -> int:
    """Return how many ISO weeks ``day`` lies after the week ``now`` falls in."""
    today = now.date()
    start = today - timedelta(days=today.weekday())
    return (day - timedelta(days=day.weekday()) - start).days // 7


def week_header(now: datetime, label_w: int) -> str:
    """Return the week row: the weeks around ``now``, a keyline after the current one."""
    weeks = [(now + timedelta(weeks=k)).isocalendar().week for k in range(-_SPAN, _SPAN + 1)]
    cells = [f"W{wk:02d}" for wk in weeks]
    before = "".join(pad(cell, _WEEK_W) for cell in cells[:_SPAN])
    after = "".join(pad(cell, _WEEK_W) for cell in cells[_SPAN + 1 :]).rstrip()
    return " " * label_w + before + f"{cells[_SPAN]}  │  " + after


def _beyond(days: list[date], arrow: str) -> str:
    """Return the edge note for dates past one end of the chart: the nearest, then the rest."""
    week = f"{arrow} W{days[0].isocalendar().week:02d}"
    return week if len(days) == 1 else f"{week} +{len(days) - 1}"


def _lane(
    line: str, dated: list[tuple[date, SpineRow]], now: datetime | None
) -> tuple[str, list[tuple[int, str]]]:
    """Return a lane's line with its markers drawn, and each drawn marker's column and key.

    A marker sits in the week of its date; two in one week share the cell, drawn open while
    either is open, and its label names the earlier with a count. A date past either end of
    the drawn weeks is named after the line, so it is stated rather than dropped.
    """
    if now is None or not dated:
        return line, []
    cells = list(line)
    keys: dict[int, list[str]] = {}
    early: list[date] = []
    late: list[date] = []
    for day, row in sorted(dated, key=lambda pair: (pair[0], pair[1].key)):
        k = week_offset(day, now)
        if k < -_SPAN:
            early.append(day)
        elif k > _SPAN:
            late.append(day)
        else:
            col = _WEEK_W * (k + _SPAN) + _MARK_AT
            if cells[col] != DATED:
                cells[col] = DONE if status(row) in _CLOSED else DATED
            keys.setdefault(col, []).append(row.key)
    edges = [_beyond(sorted(early, reverse=True), "◂")] if early else []
    edges += [_beyond(late, "▸")] if late else []
    tail = "".join(f" {edge}" for edge in edges)
    labels = [(col, f"{ks[0]}+{len(ks) - 1}" if len(ks) > 1 else ks[0]) for col, ks in keys.items()]
    return "".join(cells) + tail, sorted(labels)


def _label_row(labels: list[tuple[int, str]], note: str) -> str:
    """Return the row under a lane: each marker's key under it where it fits, then the note.

    A key that would run into the one before it is left out; the note still counts it.
    """
    out = ""
    for col, key in labels:
        if col >= cell_len(out) + (1 if out else 0):
            out += " " * (col - cell_len(out)) + key
    return f"{out}  {note}" if out else note


def _native_lanes(view: View, spine: SpineView, now: datetime | None) -> list[str]:
    """Return the week row and one lane per Track, its dated Milestones drawn on it."""
    s, w = view.session, view.w
    tracks = _of(spine, Epoch2Collection.TRACK)
    # the caret's slot, the key, and one cell before the lane
    label_w = min(20, max(_LANE, *(cell_len(t.key) + 3 for t in tracks))) if tracks else _LANE
    on_lanes = (s.tl_reg or LANES) == LANES
    dv.sel_in(s, len(tracks))
    s.mark, s.timeline_marks, s.timeline_marker = 0, 0, None
    if not tracks:
        return ["   ∅ no Track is recorded, so the chart has no lane"]
    if now is None:
        rows = [f"   the week now falls in is {UNKNOWN_WORD} · no instant is held to place it"]
        line = "─" * max(0, w - label_w - 1)
    else:
        weeks = week_header(now, label_w)
        at = weeks.index("│") - label_w
        rows = [weeks]
        line = "─" * at + "│" + "─" * max(0, cell_len(weeks) - label_w - at - 1)
    milestones = _of(spine, Epoch2Collection.MILESTONE)
    for i, track in enumerate(tracks):
        on = on_lanes and i == s.sel
        mine = [m for m in milestones if m.parent_key == track.key]
        dated = [(day, m) for m in mine if (day := target_of(m)) is not None]
        drawn, labels = _lane(line, dated, now)
        rows.append(Fixed(pad(("▸ " if on else "  ") + pad(track.key, label_w - 2) + drawn, w)))
        note = f"{len(dated)} dated · {len(mine) - len(dated)} undated below"
        rows.append(Fixed(pad(" " * label_w + _label_row(labels, note), w)))
    return rows


def _native_regions(view: View, spine: SpineView) -> list[str]:
    """Return the UNDATED and RELEASES regions and the dependency line, publishing their rows."""
    s, w = view.session, view.w
    region = s.tl_reg or LANES
    milestones = _of(spine, Epoch2Collection.MILESTONE)
    undated: list[tuple[str, ...]] = [
        (m.key, m.title or "", NO_DATE) for m in milestones if target_of(m) is None
    ]
    releases: list[tuple[str, ...]] = [
        (r.key, r.title or "", UNKNOWN_WORD, status(r))
        for r in _of(spine, Epoch2Collection.RELEASE)
    ]
    listed = undated if region == UNDATED else releases if region == RELEASES else []
    if listed:
        s.tl_sel = max(0, min(len(listed) - 1, s.tl_sel))
    if region != LANES:
        # the arrows and Enter walk the focused region's rows, not the lanes above it
        s.nav_rows = len(listed)
    wide = view.wide
    undated_table = Table([12, 11, 48 if wide else 26, 0], 2)
    rows = [thin(w), f" UNDATED     {dv.plural(len(undated), 'milestone')} with no date proposed"]
    rows.extend(
        undated_table.row(["", *u], region == UNDATED and i == s.tl_sel)
        for i, u in enumerate(undated)
    )
    tally = Counter(r[3] for r in releases)
    stated = " · ".join(f"{n} {word.lower()}" for word, n in tally.items())
    rows += [thin(w), f" RELEASES    {stated or '∅ no release is recorded'}"]
    release_table = Table([12, 13, 24 if wide else 11, 11, 0], 2)
    rows.extend(
        release_table.row(["", "▣ " + r[0], r[1], r[2], r[3]], region == RELEASES and i == s.tl_sel)
        for i, r in enumerate(releases)
    )
    rows += [thin(w), " DEPENDS     no dependency between Milestones is recorded", thin(w)]
    s.tl_regs = {UNDATED: [list(u) for u in undated], RELEASES: [list(r) for r in releases]}
    return rows


#: The edit kind of the field the menu's ``propose date`` opens over an undated Milestone.
DATE_FIELD = "date"
#: The keybar while the date field takes the typing.
DATE_KEYS: tuple[tuple[str, str], ...] = (
    ("type", "YYYY-MM-DD"),
    ("Enter", "preview"),
    ("Esc", "cancel"),
)
#: The cells an ISO calendar day takes.
_DAY_W = 10


def propose(ctx: Ctx, key: str) -> None:
    """Open the date field over the undated Milestone the cursor is on, or say why not.

    The lanes draw no marker cursor, so a date is proposed for a row of the UNDATED region.
    """
    s = ctx.s
    listed = (s.tl_regs or {}).get(UNDATED) or []
    if (s.tl_reg or LANES) != UNDATED or not listed:
        ctx.log(key, "propose date · Tab to UNDATED and choose a milestone · a lane has no cursor")
        return
    milestone = listed[min(s.tl_sel, len(listed) - 1)][0]
    s.edit = {"kind": DATE_FIELD, "key": milestone, "text": ""}
    ctx.log(key, f"propose date · type {milestone}'s target as YYYY-MM-DD · Esc cancels")


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Take the keys while the date field is open: type, preview on Enter, cancel on Escape.

    Returns:
        Whether the key was claimed; every key is while the field is open.
    """
    s = ctx.s
    edit = s.edit
    if s.overlay or edit is None or edit.get("kind") != DATE_FIELD:
        return False
    if key == "Escape":
        s.edit = None
        ctx.log("Esc", "propose date cancelled · nothing written")
    elif key == "Enter":
        try:
            day = date.fromisoformat(edit["text"])
        except ValueError:
            ctx.log("Enter", f"{edit['text'] or 'nothing'} is not a YYYY-MM-DD day")
            return True
        s.edit = None
        open_target(ctx, edit["key"], day)
    elif key == "Backspace":
        edit["text"] = edit["text"][:-1]
    elif (key.isdigit() or key == "-") and len(edit["text"]) < _DAY_W:
        edit["text"] += key
    else:
        ctx.noop(key)
    return True


def roadmap_frame(view: View, spine: SpineView) -> list[str]:
    """Return the Timeline chart drawn from the read model the daemon served.

    Args:
        view: The render being built; its instant places the week the chart centres on,
            else the instant the rows were read.
        spine: The roadmap read model: Tracks, Milestones, Batches and Releases.

    Returns:
        The full frame, the legend beside the keybar or above it.
    """
    now = view.now or spine.generated_at
    milestones = _of(spine, Epoch2Collection.MILESTONE)
    if now is None:
        span = f"now {UNKNOWN_WORD}"
    else:
        first, last = now - timedelta(weeks=_SPAN), now + timedelta(weeks=_SPAN)
        week = now.isocalendar().week
        span = f"W{first.isocalendar().week:02d} → W{last.isocalendar().week:02d} · now W{week:02d}"
    stated = sum(1 for m in milestones if target_of(m) is not None)
    dated = f"{stated} of {dv.plural(len(milestones), 'milestone')} dated"
    rows = [
        *native_head(
            view,
            spine,
            crumb_text=route_crumb(view, spine, "Timeline"),
            summary=f"{span} · {dated}",
        ),
        *_native_lanes(view, spine, now),
        *_native_regions(view, spine),
    ]
    edit = view.session.edit
    if edit is not None and edit.get("kind") == DATE_FIELD:
        field = f"{edit['key']} target {edit['text']}▏ · Enter previews · Esc cancels"
        rows.append(label("DATE", field))
        return _with_legend(view, rows, keybar(list(DATE_KEYS), view.w), _NATIVE_LEGEND)
    entries = native_keys("timeline", windowed=view.session.windowed)
    if (view.session.tl_reg or LANES) == LANES:
        # the lanes carry no marker cursor, so Enter has nothing to open until Tab moves on
        entries = tuple(e for e in entries if e.keys != ("Enter",))
    return _with_legend(view, rows, route_keys_bar(view, entries), _NATIVE_LEGEND)


def render(view: View) -> list[str]:
    """Return the Timeline frame, native when a read model is held."""
    spine = held(view)
    if spine is not None:
        return roadmap_frame(view, spine)
    s, fx, w = view.session, view.fixture, view.w
    dv.sel_in(s, len(TL_LANES))
    rows = [
        header(view, f" Eä ▸ {fx.scope} ▸ Timeline"),
        " W26 → W32 · now W29 · 8 of 26 weeks in view",
        bar(w),
        _WEEKS,
    ]
    rows.extend(_lanes(view))
    rows.extend(_regions(view))
    return _with_legend(view, rows, route_keys_bar(view, ROUTE_KEYS["timeline"]), _LEGEND)
