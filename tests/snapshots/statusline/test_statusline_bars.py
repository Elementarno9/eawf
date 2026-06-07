"""Snapshot test for the statusline context-usage + rate-window bars (W39).

The renderer surfaces a context-usage bar and a rate-window bar, each as a
block-eighths progress glyph (reusing the W20 bars primitive). The combined
line is pinned against a committed golden. Regenerate with
``EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/statusline/test_statusline_bars.py -q``
then re-run without the env var to confirm the committed file matches.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from eawf.surfaces.render.bars import BLOCK_EIGHTHS, BLOCK_EMPTY
from eawf.surfaces.render.statusline import (
    StatuslineTheme,
    context_usage_segment,
    rate_window_segment,
    render_segments,
    render_usage_bar,
)

_GOLDEN_DIR = Path(__file__).parent / "golden"
_GOLDEN_PATH = _GOLDEN_DIR / "bars.txt"

#: Deterministic theme: plain separator, no colour / glyph decoration.
_THEME = StatuslineTheme(name="snapshot", separator=" | ")

#: The set of valid block-eighths cells a rendered bar may contain.
_BAR_CELLS = set(BLOCK_EIGHTHS) | {BLOCK_EMPTY}

_CONTEXT_RATIO = 0.42
_RATE_RATIO = 0.875
_WIDTH = 8


def _render() -> str:
    segments = [
        context_usage_segment(_CONTEXT_RATIO, width=_WIDTH),
        rate_window_segment(_RATE_RATIO, width=_WIDTH),
    ]
    return render_segments(segments, _THEME)


def test_bars_render_matches_golden() -> None:
    rendered = _render()
    if os.environ.get("EAWF_SNAPSHOT_REGEN"):
        _GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        # A bar ends in blank (space) cells, so the rendered line can carry
        # trailing whitespace the trailing-whitespace pre-commit hook would
        # strip. Brackets keep that whitespace interior to the stored line so
        # the golden round-trips through the hook unchanged; the trailing
        # newline keeps the end-of-file-fixer a no-op too.
        _GOLDEN_PATH.write_text(f"[{rendered}]\n", encoding="utf-8")
        pytest.skip("regenerated golden under EAWF_SNAPSHOT_REGEN=1")
    assert _GOLDEN_PATH.is_file(), "golden bars.txt missing -- regen with EAWF_SNAPSHOT_REGEN=1"
    expected = _GOLDEN_PATH.read_text(encoding="utf-8").removesuffix("\n")
    assert f"[{rendered}]" == expected


def test_context_usage_segment_is_block_eighths_bar() -> None:
    # measurable_signal: the context-usage segment is a block-eighths bar.
    segment = context_usage_segment(_CONTEXT_RATIO, width=_WIDTH)
    assert segment.module == "context_usage"
    assert len(segment.text) == _WIDTH
    assert set(segment.text) <= _BAR_CELLS


def test_rate_window_segment_is_block_eighths_bar() -> None:
    # measurable_signal: the rate-window segment is a block-eighths bar.
    segment = rate_window_segment(_RATE_RATIO, width=_WIDTH)
    assert segment.module == "rate_window"
    assert len(segment.text) == _WIDTH
    assert set(segment.text) <= _BAR_CELLS


def test_usage_bar_matches_bars_primitive() -> None:
    # The wrapper threads straight through the W20 bars primitive.
    from eawf.surfaces.render.bars import render_block_bar

    assert render_usage_bar(_CONTEXT_RATIO, width=_WIDTH) == render_block_bar(
        _CONTEXT_RATIO, width=_WIDTH
    )


def test_full_bar_is_all_full_blocks() -> None:
    # boundary: a fully-used window renders every cell as the full block.
    segment = context_usage_segment(1.0, width=_WIDTH)
    assert segment.text == BLOCK_EIGHTHS[-1] * _WIDTH


def test_empty_bar_is_all_blank_cells() -> None:
    # boundary: a zero-fill window renders every cell blank.
    segment = rate_window_segment(0.0, width=_WIDTH)
    assert segment.text == BLOCK_EMPTY * _WIDTH


def test_usage_bar_rejects_out_of_range_ratio() -> None:
    # error-path: a ratio outside [0, 1] is rejected by the bar primitive.
    with pytest.raises(ValueError, match="ratio out of range"):
        context_usage_segment(1.5, width=_WIDTH)


def test_usage_bar_rejects_non_positive_width() -> None:
    # error-path: a non-positive width is rejected by the bar primitive.
    with pytest.raises(ValueError, match="width must be positive"):
        rate_window_segment(0.5, width=0)
