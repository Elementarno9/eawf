"""Snapshot tests for fitting the statusline to the terminal width.

A full statusline is pinned at 80 and 160 columns against committed goldens;
each line must fit its width once the colour escapes are stripped. Regenerate with
``EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/statusline/test_statusline_width.py -q``
then re-run without the env var to confirm the committed files match.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Final

import pytest

from eawf.surfaces.render.statusline import (
    SegmentSource,
    StatuslineSegment,
    StatuslineTheme,
    load_themes,
    render_segments,
    sourced_segment,
    unavailable_segment,
)

_GOLDEN_DIR: Final = Path(__file__).parent / "golden"
_ANSI: Final = re.compile(r"\x1b\[[0-9;]*m")
_SOURCE: Final = SegmentSource(producer="snapshot", provenance="snapshot#field")
_PLAIN: Final = StatuslineTheme(name="plain", separator=" | ")


def _line() -> list[StatuslineSegment]:
    """Return one segment per module, in the orchestrator's left-to-right order."""
    return [
        sourced_segment("scope", "scope", "EAWF-0403", _SOURCE),
        sourced_segment("budget", "budget", "1.2M/4.0M", _SOURCE),
        sourced_segment("git", "git", "feature/eawf-v0.7-rc2*", _SOURCE),
        sourced_segment("model_session_cwd", "model", "Opus ses:3f2a cwd:eawf", _SOURCE),
        sourced_segment("context_tokens", "ctx", "84.1k/200.0k", _SOURCE),
        unavailable_segment("rate_window", "rate", "no-rate-limits", _SOURCE),
        sourced_segment("cost", "cost", "$1.92", _SOURCE),
        sourced_segment("mcp_health", "mcp", "2/2", _SOURCE),
        sourced_segment("hooks_plugins", "hooks", "0 plugins:3", _SOURCE, status="info"),
        sourced_segment("memory", "mem", "51@69KiB", _SOURCE),
        sourced_segment("token_saving", "save", "12%", _SOURCE),
    ]


def _visible(line: str) -> str:
    return _ANSI.sub("", line)


@pytest.mark.parametrize("columns", [80, 160])
def test_line_fits_and_matches_golden(columns: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", str(columns))
    rendered = render_segments(_line(), load_themes()["default"])
    golden = _GOLDEN_DIR / f"width-{columns}.txt"
    if os.environ.get("EAWF_SNAPSHOT_REGEN"):
        golden.write_text(f"{_visible(rendered)}\n", encoding="utf-8")
        pytest.skip("regenerated golden under EAWF_SNAPSHOT_REGEN=1")
    assert len(_visible(rendered)) <= columns
    assert _visible(rendered) == golden.read_text(encoding="utf-8").removesuffix("\n")


def test_wider_terminal_keeps_every_segment_the_narrow_one_keeps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    narrow = render_segments(_line(), _PLAIN).split(" | ")
    monkeypatch.setenv("COLUMNS", "160")
    wide = render_segments(_line(), _PLAIN).split(" | ")
    assert set(narrow) < set(wide)
    assert narrow[0] == "scope:EAWF-0403"
    assert "save:12%" not in narrow


def test_no_columns_renders_the_whole_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COLUMNS", raising=False)
    assert len(render_segments(_line(), _PLAIN).split(" | ")) == len(_line())


@pytest.mark.parametrize("raw", ["", "0", "-5", "wide", " "])
def test_unusable_columns_renders_the_whole_line(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", raw)
    assert len(render_segments(_line(), _PLAIN).split(" | ")) == len(_line())


def test_exact_fit_keeps_every_segment_and_one_cell_less_drops_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COLUMNS", raising=False)
    whole = render_segments(_line(), _PLAIN)
    monkeypatch.setenv("COLUMNS", str(len(whole)))
    assert render_segments(_line(), _PLAIN) == whole
    monkeypatch.setenv("COLUMNS", str(len(whole) - 1))
    assert render_segments(_line(), _PLAIN) == whole.removesuffix(" | save:12%")


def test_empty_line_renders_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "10")
    assert render_segments([], _PLAIN) == ""


def test_lone_overflowing_segment_is_clipped_with_an_ellipsis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _line()[:1]
    monkeypatch.setenv("COLUMNS", "8")
    assert render_segments(scope, _PLAIN) == "scope:E…"
    monkeypatch.setenv("COLUMNS", "1")
    assert render_segments(scope, _PLAIN) == "…"


def test_nothing_fits_keeps_the_segment_most_worth_keeping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "6")
    assert render_segments(_line(), _PLAIN) == "scope…"


def test_a_smaller_lower_segment_fills_room_a_larger_one_cannot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "26")
    assert render_segments(_line(), _PLAIN) == "scope:EAWF-0403 | mcp:2/2"


def test_colour_escapes_do_not_count_against_the_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "15")
    coloured = StatuslineTheme(
        name="c", separator=" | ", colors={"ok": "\x1b[32m", "reset": "\x1b[0m"}
    )
    assert render_segments(_line(), coloured) == "\x1b[32mscope:EAWF-0403\x1b[0m"


def test_wide_glyphs_take_two_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    wide = sourced_segment("scope", "scope", "日本", _SOURCE)
    monkeypatch.setenv("COLUMNS", "9")
    assert render_segments([wide], _PLAIN) == "scope:日…"


def test_unlisted_module_goes_before_any_listed_one(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = unavailable_segment("boom", "boom", "render-failed", _SOURCE, status="failed")
    segments = [failed, sourced_segment("token_saving", "save", "12%", _SOURCE)]
    monkeypatch.setenv("COLUMNS", "12")
    assert render_segments(segments, _PLAIN) == "save:12%"
