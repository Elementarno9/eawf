"""The transcript scrollbar shows where the window sits, not only that it was cut.

A cut window ends every block-section row on a scrollbar cell. The thumb, ``█`` in the
accent, is as long as the share of the transcript on screen and starts at the share above
it; every other row of the track is a thin ``│`` in the rail's tone. A window that shows
the whole transcript draws no column at all.
"""

from __future__ import annotations

from typing import Final

import pytest

from eawf.surfaces.tui.console.frame import Receded
from eawf.surfaces.tui.console.paint import Part, Stroke, paint
from eawf.surfaces.tui.console.renderers.transcript import Line, Window, _scrollbar, _window
from tests.tui.surfaces.tui.console import test_transcript_blocks as tb

THUMB: Final = "█"
TRACK: Final = "│"


def _flat(total: int) -> list[Line]:
    """Return ``total`` transcript lines, each the head of its own block."""
    return [Line(n, True, f" line {n}") for n in range(total)]


def _column(rows: list[str]) -> str:
    """Return the scrollbar column of ``rows``, top to bottom."""
    return "".join(row[-1] for row in rows)


def _expected(track: int, win: Window, total: int) -> str:
    """Return the column the criterion's formula draws for ``win``."""
    size = max(1, round(track * len(win.lines) / total))
    start = max(0, min(track - size, round(track * win.top / total)))
    return TRACK * start + THUMB * size + TRACK * (track - start - size)


def _section(frame: str) -> list[str]:
    """Return the block section's rows: those between the state row's rule and the outcome."""
    rows = tb._rows(frame)
    outcome = max(i for i, row in enumerate(rows) if row.startswith(" OUTCOME"))
    first = next(i for i, row in enumerate(rows) if row.startswith(" STATE"))
    # the state row may wrap a reason under it, so the section starts past the first thin rule
    rule = next(i for i in range(first, outcome) if set(rows[i]) == {"─"})
    return rows[rule + 1 : outcome - 1]


def _long_run() -> tb.TranscriptReadModel:
    events = tuple(tb._event(n) if n % 2 else tb._summarized(n) for n in range(1, 61))
    return tb._view(events)


def _last(row: str) -> Stroke:
    return paint(row, Part.BODY)[-1]


# ---- size and position: the thumb is the visible share, at the share above ----------


@pytest.mark.parametrize(
    ("total", "track", "sel"),
    [(100, 20, 50), (40, 12, 0), (40, 12, 39), (25, 10, 13), (1000, 8, 500), (21, 20, 10)],
)
def test_thumb_size_and_position_follow_the_visible_share(total: int, track: int, sel: int) -> None:
    flat = _flat(total)
    win = _window(flat, sel, track)
    body = [f" row {i}" for i in range(track)]
    drawn = _scrollbar(body, win, total)
    assert [row[:-2] for row in drawn] == body
    assert all(row[-2] == " " for row in drawn)
    assert _column(drawn) == _expected(track, win, total)


def test_thumb_size_and_position_never_shrink_below_one_row() -> None:
    """Max length: a window of one line in a thousand still draws one thumb row."""
    win = Window(_flat(1)[:1], top=400, take=1, above=400, below=599)
    assert _column(_scrollbar(["a", "b", "c"], win, 1000)) == f"{TRACK}{THUMB}{TRACK}"


def test_thumb_size_and_position_are_clamped_inside_the_track() -> None:
    """Off by one: two rounded halves would end the thumb one row past the track."""
    win = Window(_flat(4)[2:], top=2, take=2, above=2, below=0)
    assert _column(_scrollbar(["a", "b", "c"], win, 4)) == f"{TRACK}{THUMB}{THUMB}"


def test_thumb_size_and_position_on_a_single_row_track() -> None:
    win = Window(_flat(5)[:1], top=0, take=1, above=0, below=4)
    assert _scrollbar(["a"], win, 5) == [f"a {THUMB}"]


def test_thumb_size_and_position_in_the_native_frame() -> None:
    """The frame's column is the formula's, for the window it cut."""
    for sel in (0, None):
        section = _section(tb._frame(_long_run(), sel=sel))
        column = _column(section)
        assert set(column) == {THUMB, TRACK}
        thumb = column.strip(TRACK)
        assert set(thumb) == {THUMB}
        assert column.startswith(THUMB) if sel == 0 else column.endswith(THUMB)


# ---- accent: the thumb is painted as the brand, the track as the rail ------------------


def test_thumb_accent_paints_the_thumb_brand_and_the_track_rail() -> None:
    section = _section(tb._frame(_long_run(), sel=0))
    thumb = next(row for row in section if row.endswith(THUMB))
    track = next(row for row in section if row.endswith(TRACK))
    assert _last(thumb) == Stroke(THUMB, surface="brand", bold=_last(thumb).bold)
    assert _last(track).text == TRACK and _last(track).surface == "rail"


def test_thumb_accent_survives_a_block_head_and_the_caret() -> None:
    """A block head and the caret row skip the body rules, yet the column is still drawn."""
    section = _section(tb._frame(_long_run(), sel=0))
    caret = next(row for row in section if row.startswith("▸"))
    stroke = _last(caret)
    assert (stroke.text, stroke.surface, stroke.ground) == (THUMB, "brand", None)


def test_thumb_accent_survives_a_receded_row() -> None:
    row = Receded(f" waiting on tool bash · 15s {THUMB}")
    assert _last(row).surface == "brand"
    assert paint(row, Part.BODY)[0].surface == "recede"


def test_thumb_accent_absent_when_the_window_is_not_cut() -> None:
    section = _section(tb._frame(tb._view((tb._event(1),))))
    assert not any(row.endswith((THUMB, TRACK)) for row in section)


@pytest.mark.parametrize("row", ["", THUMB, f"x{THUMB}", "│ boxed card │", "a │ b │"])
def test_thumb_accent_leaves_rows_without_a_scrollbar_cell(row: str) -> None:
    """Empty, single-cell, glued and boxed rows carry no scrollbar cell to paint."""
    strokes = paint(row, Part.BODY)
    assert all(s.surface != "brand" for s in strokes)


def test_thumb_accent_only_in_the_body_band() -> None:
    assert all(s.surface != "brand" for s in paint(f" ↑↓ block {THUMB}", Part.KEYBAR))


# ---- edges: a hundred lines in a twenty-row track --------------------------------------


@pytest.mark.parametrize(("sel", "start"), [(0, 0), (99, 16)])
def test_thumb_edges_are_flush_with_the_track_ends(sel: int, start: int) -> None:
    flat = _flat(100)
    win = _window(flat, sel, 20)
    assert (win.top == 0) if sel == 0 else (win.top + len(win.lines) == 100)
    column = _column(_scrollbar(["x"] * 20, win, 100))
    assert column.index(THUMB) == start
    assert column.count(THUMB) == 4
    assert column[start : start + 4] == THUMB * 4


def test_thumb_edges_rejects_a_window_cut_from_nothing() -> None:
    """Error path: a window is only cut from a transcript with lines in it."""
    with pytest.raises(ZeroDivisionError):
        _scrollbar(["x"], Window([], 0, 0, 0, 0), 0)
