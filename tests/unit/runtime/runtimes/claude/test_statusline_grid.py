"""The meter rows' column grid, its shrink to ``COLUMNS`` and the bars drawn in it.

Each row is label (3, left), bar (W), percent (4, right), pace or cache (5,
right) and detail (left), one space apart, so the detail starts at offset
W + 16 on every row. With a width the bar shrinks to fit the widest detail;
below 10 cells the detail drops, then the pace column, then every meter row.
A bar is W cells filled in eighth-cell units on a track.
"""

from __future__ import annotations

import re
from typing import Final

import pytest

from eawf.surfaces.render.statusline import (
    GlyphSet,
    LineStyle,
    PowerlineBar,
    load_theme,
    render_powerline,
)

_SGR: Final = re.compile(r"\x1b\[([0-9;]*)m")
_BAR_GLYPHS: Final = frozenset("█▏▎▍▌▋▊▉░│")

#: Three rows as the producers state them: a context row and two limit windows.
_BARS: Final = (
    PowerlineBar(label="ctx", ratio=0.064, detail="64K/1.0M", cache_share=92),
    PowerlineBar(label="5h", ratio=0.23, detail="2h05m"),
    PowerlineBar(label="7d", ratio=0.41, detail="6d14h", pace=-5, expected=46.0),
)


def _plain(rows: list[str]) -> list[str]:
    return [_SGR.sub("", row) for row in rows]


def _rows(
    bars: tuple[PowerlineBar, ...] = _BARS,
    *,
    width: int | None = None,
    bar_width: int = 20,
    colour: bool = True,
    glyphs: GlyphSet = "nerd",
) -> list[str]:
    style = LineStyle(
        theme=load_theme("ansi") if colour else None, glyphs=glyphs, bar_width=bar_width
    )
    return render_powerline([], list(bars), style, width=width)


# ---- the grid -----------------------------------------------------------------


@pytest.mark.parametrize("bar_width", [10, 20, 40])
def test_detail_offset(bar_width: int) -> None:
    offset = bar_width + 16
    cases = {
        "all rows": _BARS,
        "pace unknown everywhere": tuple(
            PowerlineBar(label=bar.label, ratio=bar.ratio, detail=bar.detail) for bar in _BARS
        ),
        "pace known everywhere": tuple(
            PowerlineBar(label=bar.label, ratio=bar.ratio, detail=bar.detail, pace=17)
            for bar in _BARS
        ),
        "ctx absent": _BARS[1:],
        "5h absent": (_BARS[0], _BARS[2]),
        "one row": _BARS[2:],
    }
    for case, bars in cases.items():
        for colour in (True, False):
            rows = _plain(_rows(bars, bar_width=bar_width, colour=colour))
            assert len(rows) == len(bars), case
            for bar, row in zip(bars, rows, strict=True):
                assert bar.detail is not None
                assert row.index(bar.detail) == offset, (case, colour, row)
                assert row[:3] == bar.label.ljust(3)
                assert row[bar_width + 5 : bar_width + 9] == f"{bar.percent:>3}%"
    # A row whose detail is blank ends after its percent or pace; nothing pads past it.
    half = bar_width // 2
    bare = _plain(_rows((PowerlineBar(label="7d", ratio=0.5),), bar_width=bar_width, colour=False))
    assert bare == [f"7d  {'█' * half}{'░' * half}  50%"]
    paced = _plain(_rows((PowerlineBar(label="7d", ratio=0.5, pace=4),), bar_width=bar_width))
    assert paced[0].endswith("  50%   +4%")
    assert len(paced[0]) == bar_width + 15


# ---- shrinking to COLUMNS -----------------------------------------------------


def _layout(rows: list[str]) -> tuple[int, bool, bool]:
    """Return stripped colourless rows' one bar width and whether pace and detail print.

    A row with no pace or cache share and no shown detail ends at its percent, so the pace
    and detail columns are read across all rows of the render.
    """
    [cells] = {sum(1 for char in row if char in _BAR_GLYPHS) for row in rows}
    tails = [row[4 + cells + 5 :] for row in rows]
    return cells, any(len(tail) >= 6 for tail in tails), any(len(tail) > 6 for tail in tails)


def _expected(columns: int, configured: int, widest: int) -> tuple[int, bool, bool] | None:
    for cells, pace, detail in (
        (min(configured, columns - 16 - widest), True, True),
        (min(configured, columns - 15), True, False),
        (min(configured, columns - 9), False, False),
    ):
        if cells >= 10:
            return cells, pace, detail
    return None


def test_shrink_boundaries() -> None:
    widest = max(len(bar.detail or "") for bar in _BARS)
    assert widest == 8
    # COLUMNS unset never shrinks, even past any terminal.
    for configured in (10, 20, 40):
        rows = _plain(_rows(colour=False, bar_width=configured))
        assert _layout(rows) == (configured, True, True)

    # The boundaries at the default width 20: each drop happens one column below its edge.
    pinned = {
        44: (20, True, True),
        43: (19, True, True),
        34: (10, True, True),
        33: (18, True, False),
        25: (10, True, False),
        24: (15, False, False),
        19: (10, False, False),
        18: None,
        1: None,
    }
    for columns, expected in pinned.items():
        assert _expected(columns, 20, widest) == expected, columns
    for configured in (10, 20, 40):
        for columns in range(1, 80):
            rows = _plain(_rows(width=columns, colour=False, bar_width=configured))
            expected = _expected(columns, configured, widest)
            if expected is None:
                assert rows == [], (configured, columns)
                continue
            assert len(rows) == len(_BARS), (configured, columns)
            # every row of one render shares one layout and fits the width
            assert _layout(rows) == expected, (configured, columns, rows)
            assert all(len(row) <= columns for row in rows), (configured, columns)


# ---- bars ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ratio", "bar_width", "drawn"),
    [
        (0.0, 20, "░" * 20),
        (0.003, 20, "░" * 20),
        (0.004, 20, "▏" + "░" * 19),
        (0.07, 20, "█▍" + "░" * 18),
        (0.5, 20, "█" * 10 + "░" * 10),
        (0.999, 20, "█" * 20),
        (1.0, 20, "█" * 20),
        (0.07, 10, "▊" + "░" * 9),
        (0.07, 40, "██▊" + "░" * 37),
        (1.0, 40, "█" * 40),
    ],
)
def test_bar_units(ratio: float, bar_width: int, drawn: str) -> None:
    row = _plain(
        _rows((PowerlineBar(label="ctx", ratio=ratio),), colour=False, bar_width=bar_width)
    )
    assert row[0][4 : 4 + bar_width] == drawn


def _bar_sgr(row: str, bar_width: int) -> list[tuple[str, str]]:
    """Return ``(params, cells)`` for each escape-led run inside the bar field of *row*."""
    after_label = row.split("\x1b[0m ", 1)[1]
    runs: list[tuple[str, str]] = []
    for match in _SGR.finditer(after_label):
        end = after_label.find("\x1b[", match.end())
        cells = after_label[match.end() : end]
        if match.group(1) == "0":
            break
        runs.append((match.group(1), cells))
    assert sum(len(cells) for _, cells in runs) == bar_width
    return runs


def test_bar_units_and_fills() -> None:
    # Units are int(ratio * W * 8 + 0.5): full cells, one eighth cell, then track.
    for ratio, units in ((0.0, 0), (0.07, 11), (0.0315, 5), (0.5, 80), (1.0, 160)):
        assert int(ratio * 20 * 8 + 0.5) == units
    runs = _bar_sgr(_rows((PowerlineBar(label="ctx", ratio=0.07),))[0], 20)
    assert runs == [("46", " "), ("36;100", "▍"), ("100", " " * 18)]
    # A whole bar has no partial cell and no track; an empty one is all track.
    assert _bar_sgr(_rows((PowerlineBar(label="ctx", ratio=1.0),))[0], 20) == [("41", " " * 20)]
    assert _bar_sgr(_rows((PowerlineBar(label="ctx", ratio=0.0),))[0], 20) == [("100", " " * 20)]
    # The fill is 46 below half, 43 below four fifths, 41 from there.
    for ratio, fill in ((0.49, "46"), (0.5, "43"), (0.79, "43"), (0.8, "41"), (0.95, "41")):
        runs = _bar_sgr(_rows((PowerlineBar(label="ctx", ratio=ratio),))[0], 20)
        assert runs[0][0] == fill, ratio
    # ascii has no eighth glyph: the partial cell is left as track.
    assert (
        _plain(_rows((PowerlineBar(label="ctx", ratio=0.07),), colour=False, glyphs="ascii"))[0][
            4:24
        ]
        == "#" + "." * 19
    )
    runs = _bar_sgr(_rows((PowerlineBar(label="ctx", ratio=0.07),), glyphs="ascii")[0], 20)
    assert runs == [("46", " "), ("100", " " * 19)]


@pytest.mark.parametrize("bar_width", [9, 41, 0, -1])
def test_a_bar_width_outside_ten_to_forty_is_refused(bar_width: int) -> None:
    with pytest.raises(ValueError, match="bar_width out of range"):
        LineStyle(theme=None, bar_width=bar_width)


def test_no_bars_draw_no_rows() -> None:
    # boundary: no meter row and no segment draw nothing at all.
    assert _rows(()) == []
    assert _rows((), width=1) == []
