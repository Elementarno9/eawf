"""Snapshot test for the rows-aware multi-line statusline renderer (W38).

The renderer emits exactly the configured row count; the rendered multi-line
file is pinned against a committed golden. Regenerate the golden with
``EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/statusline/test_statusline_rows.py -q``
then re-run without the env var to confirm the committed file matches.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from eawf.surfaces.render.statusline import (
    StatuslineSegment,
    StatuslineTheme,
    render_rows,
)

_GOLDEN_DIR = Path(__file__).parent / "golden"
_GOLDEN_PATH = _GOLDEN_DIR / "three_rows.txt"

#: Deterministic theme: a plain separator and no colour / glyph decoration so
#: the golden is stable across terminals.
_THEME = StatuslineTheme(name="snapshot", separator=" | ")

#: Three rows of fixed segments -- one row per statusline line.
_ROWS_OF_SEGMENTS: list[list[StatuslineSegment]] = [
    [
        StatuslineSegment(module="state", text="P29-I13-W38"),
        StatuslineSegment(module="git", text="feature/eawf-v0.5-p29"),
    ],
    [
        StatuslineSegment(module="context_tokens", text="ctx 42%"),
        StatuslineSegment(module="token_saving", text="rate 18%"),
    ],
    [
        StatuslineSegment(module="mcp_health", text="mcp ok"),
        StatuslineSegment(module="memory", text="mem 3"),
    ],
]


def _render() -> str:
    return render_rows(_ROWS_OF_SEGMENTS, _THEME, rows=3)


def test_three_row_render_matches_golden() -> None:
    rendered = _render()
    if os.environ.get("EAWF_SNAPSHOT_REGEN"):
        _GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        # Write with one trailing newline so the repo end-of-file-fixer hook
        # is a no-op on the committed golden; the compare strips it back off.
        _GOLDEN_PATH.write_text(f"{rendered}\n", encoding="utf-8")
        pytest.skip("regenerated golden under EAWF_SNAPSHOT_REGEN=1")
    assert _GOLDEN_PATH.is_file(), (
        "golden three_rows.txt missing -- regen with EAWF_SNAPSHOT_REGEN=1"
    )
    # The render contract emits no trailing newline; the committed golden
    # carries exactly one (the end-of-file-fixer convention), so strip it.
    expected = _GOLDEN_PATH.read_text(encoding="utf-8").removesuffix("\n")
    assert rendered == expected


def test_render_emits_exactly_configured_row_count() -> None:
    # measurable_signal: the renderer emits the configured row count.
    rendered = _render()
    assert rendered.count("\n") == 2  # 3 lines -> 2 newline separators
    assert len(rendered.splitlines()) == 3


def test_render_one_row_emits_single_line() -> None:
    # boundary: a single configured row yields one line, no trailing newline.
    rendered = render_rows(_ROWS_OF_SEGMENTS, _THEME, rows=1)
    assert rendered.splitlines() == [rendered]
    assert "\n" not in rendered


def test_render_pads_when_fewer_rows_supplied_than_requested() -> None:
    # boundary: requesting more rows than supplied pads with empty lines so
    # the row count stays stable for a fixed-height reader.
    rendered = render_rows([_ROWS_OF_SEGMENTS[0]], _THEME, rows=3)
    lines = rendered.split("\n")
    assert len(lines) == 3
    assert lines[1] == "" and lines[2] == ""


def test_render_truncates_when_more_rows_supplied_than_requested() -> None:
    # boundary: extra supplied rows beyond the requested count are dropped.
    rendered = render_rows(_ROWS_OF_SEGMENTS, _THEME, rows=2)
    assert len(rendered.split("\n")) == 2


def test_render_rejects_non_positive_rows() -> None:
    # error-path: zero rows would render nothing, so it is rejected.
    with pytest.raises(ValueError, match="rows must be positive"):
        render_rows(_ROWS_OF_SEGMENTS, _THEME, rows=0)
