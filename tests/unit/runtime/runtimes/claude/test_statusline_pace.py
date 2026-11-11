"""Pace, its bar marker and limit fills; the cache-read share; session and cost texts.

A limit window's pace is how far its used share runs ahead of an even spend
across the window: ``expected = 100 * clamp(L - (resets_at - now), 0, L) / L``
and ``pace = round_half_up(used - expected)``, printed ``+N%`` above 3,
``-N%`` below -3 and ``~`` between. The bar carries a marker at the expected
share. The ``ctx`` row shows the last call's cache-read share as ``92%c`` in
the same column. The payloads below mirror the host's statusline stdin.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.runtime.runtimes.claude import statusline as orchestrator
from eawf.runtime.runtimes.claude.statusline_modules import context_tokens, cost, rate_window
from eawf.surfaces.render.statusline import (
    LineStyle,
    PowerlineBar,
    SegmentSource,
    load_theme,
    render_powerline,
    round_half_up,
    sourced_segment,
)

_NOW: Final = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
_SGR: Final = re.compile(r"\x1b\[([0-9;]*)m")
_FIVE_HOUR: Final = 18_000
_SEVEN_DAY: Final = 604_800


def _plain(rows: list[str]) -> list[str]:
    return [_SGR.sub("", row) for row in rows]


def _window(used: float, remaining: float | None) -> dict[str, Any]:
    window: dict[str, Any] = {"used_percentage": used}
    if remaining is not None:
        window["resets_at"] = _NOW.timestamp() + remaining
    return window


def _row(key: str, used: float, remaining: float | None) -> PowerlineBar:
    [row] = rate_window.bar_rows({"rate_limits": {key: _window(used, remaining)}}, _NOW)
    return row


def _pace_text(row: PowerlineBar) -> str:
    [drawn] = _plain(render_powerline([], [row], LineStyle(theme=None), width=None))
    # label 3, bar 20 and percent 4 with their spaces put the pace column at 30.
    return drawn[30:35].strip()


def _fill(row: PowerlineBar) -> str:
    """Return the SGR background of the bar's first cell in the ansi theme."""
    [drawn] = render_powerline([], [row], LineStyle(theme=load_theme("ansi")), width=None)
    after_label = drawn.split("\x1b[0m ", 1)[1]
    first = _SGR.match(after_label)
    assert first is not None
    return first.group(1).split(";")[-1]


# ---- the pace formula -----------------------------------------------------------


def test_pace_formula() -> None:
    # The worked example: 31% used with the reset in 2h05m prints -27%.
    row = _row("five_hour", 31, 2 * 3600 + 5 * 60)
    assert row.expected == pytest.approx(100 * (18_000 - 7_500) / 18_000)
    assert row.pace == -27
    assert _pace_text(row) == "-27%"
    assert row.detail == "2h05m"
    # Both windows take their own length L.
    for key, length in (("five_hour", _FIVE_HOUR), ("seven_day", _SEVEN_DAY)):
        half = _row(key, 80, length / 2)
        assert half.expected == pytest.approx(50.0)
        assert (half.pace, _pace_text(half)) == (30, "+30%")
        quarter = _row(key, 10, length * 3 / 4)
        assert quarter.expected == pytest.approx(25.0)
        assert (quarter.pace, _pace_text(quarter)) == (-15, "-15%")
    # utilization (0-1) is read as the same used share.
    [ratio_row] = rate_window.bar_rows(
        {
            "rate_limits": {
                "seven_day": {"utilization": 0.8, "resets_at": _NOW.timestamp() + 302_400}
            }
        },
        _NOW,
    )
    assert ratio_row.pace == 30


# ---- boundaries and fills --------------------------------------------------------


#: Used percent at half the five-hour window (expected 50), and the pace text
#: and limit fill each prints.
_BOUNDARIES: Final = (
    (46, "-4%", "42"),
    (47, "~", "46"),
    (53, "~", "46"),
    (54, "+4%", "43"),
    (60, "+10%", "43"),
    (61, "+11%", "41"),
    (46.5, "-4%", "42"),
    (53.5, "+4%", "43"),
    (46.4, "-4%", "42"),
    (47.5, "~", "46"),
    (52.5, "~", "46"),
)


def test_pace_boundaries_and_fills() -> None:
    for used, text, fill in _BOUNDARIES:
        row = _row("five_hour", used, _FIVE_HOUR / 2)
        assert (_pace_text(row), _fill(row)) == (text, fill), used
    # Half points round away from zero: -3.5 is -4 and +3.5 is +4.
    assert [round_half_up(value) for value in (-3.5, -2.5, -0.5, 0.5, 2.5, 3.5)] == [
        -4,
        -3,
        -1,
        1,
        3,
        4,
    ]
    # 50.5 expected at 9,090 seconds in: 47 used is -3.5, 54 used is +3.5.
    remaining = _FIVE_HOUR - 9_090
    assert _pace_text(_row("five_hour", 47, remaining)) == "-4%"
    assert _pace_text(_row("five_hour", 54, remaining)) == "+4%"
    # A passed reset prints ~ and counts down from 0m; its marker sits at the end.
    passed = _row("five_hour", 10, -120)
    assert (_pace_text(passed), passed.detail, passed.pace) == ("~", "0m", 0)
    # The first 2% of a window prints ~, from 2% on the pace counts.
    early = _row("five_hour", 40, _FIVE_HOUR - 300)
    assert (_pace_text(early), early.pace) == ("~", 0)
    counted = _row("seven_day", 40, _SEVEN_DAY - 12_096)
    assert counted.pace == 38
    assert _pace_text(_row("seven_day", 40, _SEVEN_DAY - 12_000)) == "~"
    # A reset further away than the window's length prints ~.
    beyond = _row("five_hour", 70, _FIVE_HOUR + 60)
    assert (_pace_text(beyond), beyond.pace) == ("~", 0)
    # Used of 90% or more fills red whatever the pace.
    assert _fill(_row("five_hour", 90, 60)) == "41"
    assert _fill(_row("five_hour", 89, 60)) == "42"
    # error-path: a non-finite value has no rounding.
    with pytest.raises(ValueError):
        round_half_up(float("nan"))
    with pytest.raises(OverflowError):
        round_half_up(float("inf"))


# ---- the bar marker ---------------------------------------------------------------


def _bar_cells(row: str) -> list[tuple[str, str]]:
    """Return ``(params, glyph)`` per bar cell of a coloured meter row."""
    after_label = row.split("\x1b[0m ", 1)[1]
    cells: list[tuple[str, str]] = []
    params = ""
    for match in re.finditer(r"\x1b\[([0-9;]*)m|(.)", after_label):
        if match.group(1) is not None:
            if match.group(1) == "0":
                break
            params = match.group(1)
            continue
        cells.append((params, match.group(2)))
    return cells


#: Used percent, seconds to the reset, and the marker's cell in a 20-cell bar:
#: the worked example, the window's start, a passed reset clamped to the last
#: cell, and a marker inside the fill.
_MARKERS: Final = (
    (31, 7_500, 11),
    (5, _FIVE_HOUR - 9, 0),
    (95, -60, 19),
    (60, _FIVE_HOUR / 2, 10),
)


def test_marker_position() -> None:
    for used, remaining, index in _MARKERS:
        _assert_marker(_row("five_hour", used, remaining), index)
    # absent when the pace is unknown
    row = _row("seven_day", 40, None)
    assert (row.pace, row.expected, row.detail) == (None, None, None)
    [plain] = _plain(render_powerline([], [row], LineStyle(theme=None), width=None))
    assert plain == f"7d  {'█' * 8}{'░' * 12}  40%"
    [drawn] = render_powerline([], [row], LineStyle(theme=load_theme("ansi")), width=None)
    assert "│" not in drawn and "95;" not in drawn


def _assert_marker(row: PowerlineBar, index: int) -> None:
    assert row.expected is not None
    assert min(int(row.expected * 20 / 100), 19) == index
    [plain] = _plain(render_powerline([], [row], LineStyle(theme=None), width=None))
    assert plain[4:24].index("│") == index
    [ascii_row] = _plain(
        render_powerline([], [row], LineStyle(theme=None, glyphs="ascii"), width=None)
    )
    assert ascii_row[4:24].index("|") == index
    [drawn] = render_powerline([], [row], LineStyle(theme=load_theme("ansi")), width=None)
    cells = _bar_cells(drawn)
    assert len(cells) == 20
    params, glyph = cells[index]
    assert glyph == "│"
    # foreground 95 on the cell's own background: the fill inside it, the track past it.
    full = int(row.ratio * 20 * 8 + 0.5) // 8
    own = _fill(row) if index < full else "100"
    assert params == f"95;{own}"
    assert all(glyph != "│" for i, (_, glyph) in enumerate(cells) if i != index)
    # Catppuccin draws it pink.
    [mocha] = render_powerline(
        [], [row], LineStyle(theme=load_theme("catppuccin-mocha")), width=None
    )
    assert _bar_cells(mocha)[index][0].startswith("38;2;245;194;231;")


@pytest.mark.parametrize("expected", [-0.1, 100.1, float("nan")])
def test_marker_position_refuses_an_expected_share_out_of_range(expected: float) -> None:
    with pytest.raises(ValueError, match="expected out of range"):
        PowerlineBar(label="5h", ratio=0.5, pace=0, expected=expected)


# ---- the cache-read share --------------------------------------------------------


def _ctx(input_tokens: int, creation: int, read: int, size: int = 1_000_000) -> PowerlineBar | None:
    return context_tokens.bar_row(
        {
            "context_window": {
                "context_window_size": size,
                "current_usage": {
                    "input_tokens": input_tokens,
                    "cache_creation_input_tokens": creation,
                    "cache_read_input_tokens": read,
                },
            }
        }
    )


def test_cache_share() -> None:
    row = _ctx(2_000, 3_440, 62_560)
    assert row is not None
    assert row.cache_share == 92
    assert row.ratio == pytest.approx(0.068)
    assert (row.percent, row.detail) == (7, "68K/1.0M")
    [plain] = _plain(render_powerline([], [row], LineStyle(theme=None), width=None))
    assert plain == f"ctx █▍{'░' * 18}   7%  92%c 68K/1.0M"
    # The share is dim 39 on the terminal background.
    [drawn] = render_powerline([], [row], LineStyle(theme=load_theme("ansi")), width=None)
    assert "\x1b[2;39;49m 92%c\x1b[0m" in drawn
    # boundary: 0% and 100%, and half a percent rounds up.
    assert (row0 := _ctx(10, 0, 0)) is not None and row0.cache_share == 0
    assert (row100 := _ctx(0, 0, 10)) is not None and row100.cache_share == 100
    assert (half := _ctx(199, 0, 1)) is not None and half.cache_share == 1
    # A zero denominator leaves the column blank.
    empty = _ctx(0, 0, 0)
    assert empty is not None and empty.cache_share is None
    [blank] = _plain(render_powerline([], [empty], LineStyle(theme=None), width=None))
    assert blank == f"ctx {'░' * 20}   0%       0/1.0M"
    # ctx used sums the three classes and caps at the window.
    over = _ctx(900, 100, 500, size=1_000)
    assert over is not None and over.ratio == 1.0 and over.cache_share == 33


@pytest.mark.parametrize("share", [-1, 101])
def test_cache_share_refuses_a_share_out_of_range(share: int) -> None:
    with pytest.raises(ValueError, match="cache_share out of range"):
        PowerlineBar(label="ctx", ratio=0.5, cache_share=share)


# ---- session and cost texts; no trend sidecar ------------------------------------


#: The host's API and session durations in ms, and the session text each prints.
_SESSIONS: Final[tuple[tuple[dict[str, Any], str | None], ...]] = (
    ({"total_api_duration_ms": 1_478_000, "total_duration_ms": 3_900_000}, "1h05m · 38% active"),
    ({"total_api_duration_ms": 30_000, "total_duration_ms": 59_999}, "0m"),
    ({"total_api_duration_ms": 30_000, "total_duration_ms": 60_000}, "1m · 50% active"),
    ({"total_api_duration_ms": 90_000, "total_duration_ms": 60_000}, "1m · 100% active"),
    ({"total_api_duration_ms": 0, "total_duration_ms": 754_000}, "12m · 0% active"),
    ({"total_api_duration_ms": 375, "total_duration_ms": 75_000}, "1m · 1% active"),
    ({"total_duration_ms": 3_599_999}, "59m"),
    ({"total_api_duration_ms": 1, "total_duration_ms": 3_600_000}, "1h00m · 0% active"),
    ({"total_api_duration_ms": 1_000, "total_duration_ms": 0}, None),
    ({"total_api_duration_ms": 1_000}, None),
    ({"total_api_duration_ms": 1_000, "total_duration_ms": "2000"}, None),
)  # fmt: skip


def test_session_and_cost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for cost_block, expected in _SESSIONS:
        segment = cost.build_active({"cost": cost_block}, None)
        assert segment.truth.value == expected, cost_block
        if expected is None:
            assert segment.text == "active:n/a(no-duration)"

    assert cost.build({"cost": {"total_cost_usd": 0.5249}}, None).truth.value == (
        "$0.52 API-equivalent"
    )
    assert cost.build({"cost": {"total_cost_usd": 0}}, None).truth.value == "$0.00 API-equivalent"
    # Narrow, the cost reads API-eq before any segment drops.
    source = SegmentSource(producer="test-producer", provenance="test#field")
    segments = [
        sourced_segment("model", "model", "Opus 4.7", source),
        sourced_segment("active_time", "active", "1h05m · 38% active", source),
        cost.build({"cost": {"total_cost_usd": 0.52}}, None),
    ]
    wide = _plain(render_powerline(segments, [], LineStyle(theme=None), width=None))
    assert wide == ["Opus 4.7 | 1h05m · 38% active | $0.52 API-equivalent"]
    narrowed = _plain(render_powerline(segments, [], LineStyle(theme=None), width=50))
    assert narrowed == ["Opus 4.7 | 1h05m · 38% active | $0.52 API-eq"]
    dropped = _plain(render_powerline(segments, [], LineStyle(theme=None), width=30))
    assert dropped == ["Opus 4.7 | $0.52 API-eq"]

    # A session render writes no trend sidecar and prints no trend arrow.
    cache = tmp_path / "cache"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(cache))
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.chdir(tmp_path)
    payload = {
        "session_id": "ses-pace",
        "cost": {"total_cost_usd": 0.52},
        "rate_limits": {"five_hour": _window(31, 7_500)},
    }
    for _ in range(2):
        rendered = orchestrator._render_session(
            payload, "ses-pace", workspace=None, theme_name="none"
        )
    assert not list(cache.glob("*.levels.json"))
    assert "↑" not in rendered and "↓" not in rendered
    assert not hasattr(orchestrator, "_levels_path_for")
    assert "trend" not in PowerlineBar.__dataclass_fields__
