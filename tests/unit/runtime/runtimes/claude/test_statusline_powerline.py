"""The powerline layout: line 1 segments over the ctx, 5h and 7d bar rows.

Line 1 shows model, project, session time, cost, MCP and, when it carries a
failure token, hooks, as filled segments joined by separators. Under it each
window the host reports gets a meter row with its used percentage, pace or
cache-read share, and reset countdown. The byte-exact goldens live in
``test_statusline_goldens``.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest

from eawf.runtime.runtimes.claude import statusline as orchestrator
from eawf.runtime.runtimes.claude.statusline_modules import context_tokens, cost, model_session_cwd
from eawf.surfaces.render.statusline import (
    ColourTheme,
    LineStyle,
    PowerlineBar,
    SegmentSource,
    StatuslineSegment,
    format_context_tokens,
    format_countdown,
    load_theme,
    render_powerline,
    sourced_segment,
)

_SGR: Final = re.compile(r"\x1b\[[0-9;]*m")
_ARROW: Final = ""
_NOW: Final = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
_SOURCE: Final = SegmentSource(producer="test-producer", provenance="test#field")

PAYLOAD: Final[dict[str, Any]] = {
    "session_id": "ses-powerline",
    "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
    "workspace": {"project_dir": "/work/sample-project"},
    "cwd": "/work/sample-project/src",
    "cost": {
        "total_cost_usd": 1.2216,
        "total_api_duration_ms": 754_000,
        "total_duration_ms": 1_216_000,
    },
    "context_window": {
        "context_window_size": 1_000_000,
        "current_usage": {
            "input_tokens": 4_000,
            "cache_creation_input_tokens": 10_000,
            "cache_read_input_tokens": 50_000,
        },
    },
    "rate_limits": {
        "five_hour": {
            "used_percentage": 23,
            "resets_at": (_NOW + timedelta(hours=2, minutes=5, seconds=30)).isoformat(),
        },
        "seven_day": {
            "used_percentage": 41,
            "resets_at": (_NOW + timedelta(days=6, hours=14, minutes=10)).isoformat(),
        },
    },
}

#: What the registry and the hook health record state for the fixture session.
_MCP: Final = sourced_segment("mcp_health", "mcp", "3/6", _SOURCE, status="warn")
_HOOKS: Final = sourced_segment(
    "hooks_plugins", "hooks", "4 plugins:2 hooks!2", _SOURCE, status="degraded"
)
_HOOKS_CLEAN: Final = sourced_segment(
    "hooks_plugins", "hooks", "4 plugins:2", _SOURCE, status="info"
)


def _cells(row: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        for char in _SGR.sub("", row)
        if not unicodedata.combining(char)
    )


def _segments(hooks: StatuslineSegment = _HOOKS) -> list[StatuslineSegment]:
    """Return the fixture's line-1 segments as the builders state them, tree values filled."""
    stated = {"mcp_health": _MCP, "hooks_plugins": hooks}
    return [
        stated.get(segment.module, segment)
        for segment in orchestrator._build_segments(PAYLOAD, None)
    ]


def _bars(payload: dict[str, Any] = PAYLOAD) -> list[PowerlineBar]:
    return orchestrator._build_bars(payload, _NOW)


def _render(
    theme: ColourTheme | None = "catppuccin-mocha",
    width: int | None = None,
    *,
    segments: list[StatuslineSegment] | None = None,
    bars: list[PowerlineBar] | None = None,
) -> list[str]:
    return render_powerline(
        _segments() if segments is None else segments,
        _bars() if bars is None else bars,
        LineStyle(theme=None if theme is None else load_theme(theme)),
        width=width,
    )


def _plain(rows: list[str]) -> list[str]:
    return [_SGR.sub("", row) for row in rows]


def _line_one_texts(rows: list[str]) -> list[str]:
    return [part.strip() for part in _plain(rows)[0].split(_ARROW) if part.strip()]


# ---- CR-732: line 1 ---------------------------------------------------------


def test_line_one() -> None:
    texts = _line_one_texts(_render())
    assert texts == [
        "Opus 5.5",
        "sample-project",
        "20m · 62% active",
        "$1.22 API-equivalent",
        "MCP: 3/6",
        "hooks!2",
    ]
    # Without a failure token the hooks segment is left out.
    clean = _render(segments=_segments(_HOOKS_CLEAN))
    assert _line_one_texts(clean) == texts[:-1]


# ---- CR-733: bar rows -------------------------------------------------------


def test_bar_rows() -> None:
    # ctx: 50K of 64K read from cache is 78%; 5h: 23% used 58.2% in is -35%;
    # 7d: 41% used 5.9% in is +35%, its marker inside the fill.
    rows = _render(None)[2:]
    assert rows == [
        f"ctx █▎{'░' * 18}   6%  78%c 64K/1.0M",
        f"5h  ████▋{'░' * 6}│{'░' * 8}  23%  -35% 2h05m",
        f"7d  █│██████▎{'░' * 11}  41%  +35% 6d14h",
    ]

    partial = {**PAYLOAD, "rate_limits": {"seven_day": PAYLOAD["rate_limits"]["seven_day"]}}
    assert [bar.label for bar in _bars(partial)] == ["ctx", "7d"]
    absent = {key: value for key, value in PAYLOAD.items() if key != "rate_limits"}
    assert [bar.label for bar in _bars(absent)] == ["ctx"]
    assert orchestrator._build_bars({}, _NOW) == []


def test_bar_rows_show_on_pace_and_end_an_unknown_one_at_its_percent() -> None:
    bars = [
        PowerlineBar(label="5h", ratio=0.2, pace=-3),
        PowerlineBar(label="7d", ratio=0.2),
    ]
    rows = _render(None, segments=[], bars=bars)
    track = "░" * 16
    assert rows == [f"5h  ████{track}  20%     ~", f"7d  ████{track}  20%"]


def test_context_bar_row_needs_a_size_and_every_input_class() -> None:
    no_size = {"context_window": {"current_usage": PAYLOAD["context_window"]["current_usage"]}}
    assert context_tokens.bar_row(no_size) is None
    partial_usage = {
        "context_window": {"context_window_size": 1_000, "current_usage": {"input_tokens": 10}}
    }
    assert context_tokens.bar_row(partial_usage) is None
    assert context_tokens.bar_row({}) is None
    over = {
        "context_window": {
            "context_window_size": 1_000,
            "current_usage": {
                "input_tokens": 900,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 500,
            },
        }
    }
    bar = context_tokens.bar_row(over)
    assert bar is not None
    assert (bar.ratio, bar.detail) == (1.0, "2K/1K")


# ---- CR-736: goldens and width ----------------------------------------------


def test_rows_fit_the_width() -> None:
    for theme in ("catppuccin-mocha", "catppuccin-latte"):
        for width in (80, 120):
            rows = _render(theme, width)
            assert all("\n" not in row and _cells(row) <= width for row in rows)

    # Line 1 narrows the cost label, then drops MCP, then active time, then
    # project, and keeps model and cost.
    kept: list[list[str]] = []
    for width in range(120, 13, -1):
        rows = _render(width=width)
        assert all(_cells(row) <= width for row in rows), width
        texts = _line_one_texts(rows)
        if not kept or texts != kept[-1]:
            kept.append(texts)
    session, wide, narrow = "20m · 62% active", "$1.22 API-equivalent", "$1.22 API-eq"
    assert kept[:6] == [
        ["Opus 5.5", "sample-project", session, wide, "MCP: 3/6", "hooks!2"],
        ["Opus 5.5", "sample-project", session, narrow, "MCP: 3/6", "hooks!2"],
        ["Opus 5.5", "sample-project", session, narrow, "hooks!2"],
        ["Opus 5.5", "sample-project", narrow, "hooks!2"],
        ["Opus 5.5", narrow, "hooks!2"],
        ["Opus 5.5", narrow],
    ]
    assert any(step[0].endswith("…") and step[-1] == narrow for step in kept)

    # A meter row shrinks its bar to ten cells, then drops the detail, then the pace.
    def ctx_row(width: int) -> str | None:
        rows = _render(None, width=width, segments=[], bars=_bars()[:1])
        return rows[0] if rows else None

    assert ctx_row(44) == f"ctx █▎{'░' * 18}   6%  78%c 64K/1.0M"
    assert ctx_row(43) == f"ctx █▎{'░' * 17}   6%  78%c 64K/1.0M"
    assert ctx_row(34) == f"ctx ▋{'░' * 9}   6%  78%c 64K/1.0M"
    assert ctx_row(33) == f"ctx █▏{'░' * 16}   6%  78%c"
    assert ctx_row(24) == f"ctx █{'░' * 14}   6%"
    assert ctx_row(18) is None


def test_a_width_too_narrow_for_anything_renders_no_row() -> None:
    # boundary: one column fits no segment and no bar row.
    assert _render(width=1) == []
    assert _render(segments=[], bars=[]) == []


# ---- the builders behind line 1 ---------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ({"id": "claude-x", "display_name": "X"}, "model:X"),
        ({"id": "claude-x"}, "model:claude-x"),
        ("haiku", "model:haiku"),
        ({"name": "Opus"}, "model:Opus"),
        (None, "model:n/a(no-model)"),
        ({"display_name": 7}, "model:n/a(no-model)"),
    ],
)
def test_model_segment_reads_the_display_name(model: Any, expected: str) -> None:
    assert model_session_cwd.build_model({"model": model}, None).text == expected


@pytest.mark.parametrize(
    ("workspace", "expected"),
    [
        ({"project_dir": "/work/alpha"}, "project:alpha"),
        ({"project_dir": "/work/alpha/"}, "project:alpha"),
        ({"project_dir": ""}, "project:n/a(no-project-dir)"),
        ({"project_dir": "/"}, "project:n/a(no-project-dir)"),
        ({}, "project:n/a(no-project-dir)"),
        ("alpha", "project:n/a(no-project-dir)"),
    ],
)
def test_project_segment_reads_the_workspace_basename(workspace: Any, expected: str) -> None:
    assert model_session_cwd.build_project({"workspace": workspace}, None).text == expected


@pytest.mark.parametrize(
    ("cost_block", "expected"),
    [
        ({"total_api_duration_ms": 754_000, "total_duration_ms": 1_216_000}, "20m · 62% active"),
        ({"total_api_duration_ms": 59_999, "total_duration_ms": 60_000}, "1m · 100% active"),
        ({"total_api_duration_ms": 0, "total_duration_ms": 1}, "0m"),
        ({"total_api_duration_ms": 9_000, "total_duration_ms": 1_000}, "0m"),
        ({"total_api_duration_ms": 1_000, "total_duration_ms": 0}, None),
        ({"total_duration_ms": 61_000}, "1m"),
        ({"total_api_duration_ms": 1_000}, None),
        ({"total_api_duration_ms": "1000", "total_duration_ms": 120_000}, "2m"),
    ],
)
def test_active_time_is_wall_time_and_the_api_share(
    cost_block: dict[str, Any], expected: str | None
) -> None:
    segment = cost.build_active({"cost": cost_block}, None)
    assert segment.truth.value == expected
    if expected is None:
        assert segment.text == "active:n/a(no-duration)"


# ---- public render helpers --------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0m"),
        (59.9, "0m"),
        (60, "1m"),
        (3_599, "59m"),
        (3_600, "1h00m"),
        (7_530, "2h05m"),
        (86_399, "23h59m"),
        (86_400, "1d0h"),
        (568_200, "6d13h"),
    ],
)
def test_format_countdown(seconds: float, expected: str) -> None:
    assert format_countdown(seconds) == expected


def test_format_countdown_refuses_a_negative_time() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        format_countdown(-1)


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, "0"),
        (999, "999"),
        (1_000, "1K"),
        (64_000, "64K"),
        (999_499, "999K"),
        (999_500, "1.0M"),
        (1_000_000, "1.0M"),
        (1_250_000, "1.2M"),
    ],
)
def test_format_context_tokens(count: int, expected: str) -> None:
    assert format_context_tokens(count) == expected


def test_format_context_tokens_refuses_a_negative_count() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        format_context_tokens(-1)


@pytest.mark.parametrize(
    ("label", "ratio", "match"),
    [("ctx", -0.01, "out of range"), ("ctx", 1.01, "out of range"), ("", 0.5, "needs a label")],
)
def test_powerline_bar_refuses_what_it_cannot_draw(label: str, ratio: float, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        PowerlineBar(label=label, ratio=ratio)


@pytest.mark.parametrize(("ratio", "percent"), [(0.0, 0), (0.004, 0), (0.005, 1), (1.0, 100)])
def test_powerline_bar_percent_rounds_half_up(ratio: float, percent: int) -> None:
    assert PowerlineBar(label="ctx", ratio=ratio).percent == percent
