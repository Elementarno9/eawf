"""Snapshot test for the statusline rate-window bars.

The renderer draws one block-eighths bar per host rate-limit window, with the
window's used percentage and reset time beside it. The line is pinned against
a committed golden. Regenerate with
``EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/statusline/test_statusline_bars.py -q``
then re-run without the env var to confirm the committed file matches.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.surfaces.render.bars import BLOCK_EIGHTHS, BLOCK_EMPTY, render_block_bar
from eawf.surfaces.render.statusline import (
    RateWindow,
    SegmentSource,
    StatuslineTheme,
    rate_window_segment,
    render_segments,
    render_usage_bar,
)

_GOLDEN_DIR = Path(__file__).parent / "golden"
_GOLDEN_PATH = _GOLDEN_DIR / "bars.txt"

#: Deterministic theme: plain separator, no colour / glyph decoration.
_THEME = StatuslineTheme(name="snapshot", separator=" | ")

_SOURCE = SegmentSource(producer="snapshot", provenance="snapshot#ratio")

_RESET = datetime(2026, 9, 29, 14, 5, tzinfo=UTC)
_WINDOWS = (
    RateWindow(label="five_hour", ratio=0.42, resets_at=_RESET),
    RateWindow(label="seven_day", ratio=0.875),
)
_WIDTH = 8


def _render() -> str:
    return render_segments([rate_window_segment(_WINDOWS, _SOURCE, width=_WIDTH)], _THEME)


def test_bars_render_matches_golden() -> None:
    rendered = _render()
    if os.environ.get("EAWF_SNAPSHOT_REGEN"):
        _GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        # Brackets keep any trailing blank bar cells interior to the stored
        # line so the golden round-trips through the whitespace hooks.
        _GOLDEN_PATH.write_text(f"[{rendered}]\n", encoding="utf-8")
        pytest.skip("regenerated golden under EAWF_SNAPSHOT_REGEN=1")
    assert _GOLDEN_PATH.is_file(), "golden bars.txt missing -- regen with EAWF_SNAPSHOT_REGEN=1"
    expected = _GOLDEN_PATH.read_text(encoding="utf-8").removesuffix("\n")
    assert f"[{rendered}]" == expected


def test_rate_window_segment_draws_one_bar_per_window() -> None:
    segment = rate_window_segment(_WINDOWS, _SOURCE, width=_WIDTH)
    assert segment.module == "rate_window"
    first, second = segment.text.removeprefix("rate:").split(" · ")
    bar = first.removeprefix("five_hour ")[:_WIDTH]
    assert len(bar) == _WIDTH
    assert set(bar) <= set(BLOCK_EIGHTHS) | {BLOCK_EMPTY}
    assert first.endswith("42% ↻14:05Z")
    assert second.endswith("88%")


def test_usage_bar_matches_bars_primitive() -> None:
    assert render_usage_bar(0.42, width=_WIDTH) == render_block_bar(0.42, width=_WIDTH)


def test_single_full_window_is_all_full_blocks() -> None:
    segment = rate_window_segment([RateWindow("w", 1.0)], _SOURCE, width=_WIDTH)
    assert segment.text == f"rate:w {BLOCK_EIGHTHS[-1] * _WIDTH} 100%"


def test_rate_window_segment_refuses_no_window() -> None:
    with pytest.raises(ValueError, match="at least one window"):
        rate_window_segment([], _SOURCE, width=_WIDTH)


def test_usage_bar_rejects_out_of_range_ratio() -> None:
    with pytest.raises(ValueError, match="ratio out of range"):
        rate_window_segment([RateWindow("w", 1.5)], _SOURCE, width=_WIDTH)


def test_usage_bar_rejects_non_positive_width() -> None:
    with pytest.raises(ValueError, match="width must be positive"):
        rate_window_segment([RateWindow("w", 0.5)], _SOURCE, width=0)
