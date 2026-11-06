"""The powerline layout: line 1 segments over the ctx, 5h and 7d bar rows.

Line 1 shows model, project, active time, cost, MCP and, when it carries a
failure token, hooks, as arrow-ended segments. Under it each window the host
reports gets a bar row with its used percentage, trend and reset countdown.
Four goldens pin the rows byte for byte in both palettes at 80 and 120
columns. Regenerate them by running this file with ``EAWF_SNAPSHOT_REGEN=1``
set, then re-run it without the variable to confirm the committed files match.
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

import pytest

from eawf.runtime.runtimes.claude import statusline as orchestrator
from eawf.runtime.runtimes.claude.statusline_modules import context_tokens, cost, model_session_cwd
from eawf.surfaces.render.statusline import (
    PowerlineBar,
    SegmentSource,
    StatuslineSegment,
    format_context_tokens,
    format_countdown,
    load_palette,
    render_powerline,
    sourced_segment,
)

_GOLDEN_DIR: Final = Path(__file__).resolve().parents[4] / "golden" / "statusline"
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
    "hooks_plugins", "hooks", "4 plugins:2 hook-errors:2", _SOURCE, status="degraded"
)
_HOOKS_CLEAN: Final = sourced_segment(
    "hooks_plugins", "hooks", "4 plugins:2", _SOURCE, status="info"
)

#: The used percentages of the fixture session's previous render.
_PREVIOUS: Final = {"ctx": 3, "5h": 23, "7d": 46}


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
    return orchestrator._with_trends(orchestrator._build_bars(payload, _NOW), _PREVIOUS)


def _render(
    palette: Literal["light", "dark"] = "dark",
    width: int | None = None,
    *,
    segments: list[StatuslineSegment] | None = None,
    bars: list[PowerlineBar] | None = None,
) -> list[str]:
    return render_powerline(
        _segments() if segments is None else segments,
        _bars() if bars is None else bars,
        load_palette(palette),
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
        "12m · 62% active",
        "$1.22",
        "MCP: 3/6",
        "hook-errors:2",
    ]
    # Without a failure token the hooks segment is left out.
    clean = _render(segments=_segments(_HOOKS_CLEAN))
    assert _line_one_texts(clean) == texts[:-1]


# ---- CR-733: bar rows -------------------------------------------------------


def test_bar_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _plain(_render())[1:]
    assert rows == [
        f" ctx {_ARROW} ▋░░░░░░░░░   6% ↑+3 64K/1.0M",
        f" 5h  {_ARROW} ██▎░░░░░░░  23% 2h05m",
        f" 7d  {_ARROW} ████▏░░░░░  41% ↓-5 6d14h",
    ]

    partial = {**PAYLOAD, "rate_limits": {"seven_day": PAYLOAD["rate_limits"]["seven_day"]}}
    assert [bar.label for bar in _bars(partial)] == ["ctx", "7d"]
    absent = {key: value for key, value in PAYLOAD.items() if key != "rate_limits"}
    assert [bar.label for bar in _bars(absent)] == ["ctx"]
    assert orchestrator._build_bars({}, _NOW) == []

    # The trend counts from the session's previous render, through the sidecar.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(tmp_path / "cache"))
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.chdir(tmp_path)
    levels = orchestrator._levels_path_for("ses-powerline")
    first = orchestrator.render_pipeline(
        PAYLOAD, workspace=None, palette="dark", levels_path=levels
    )
    assert "↑" not in first and "↓" not in first
    busier = {
        **PAYLOAD,
        "rate_limits": {"five_hour": {"used_percentage": 30}, "seven_day": {"used_percentage": 41}},
    }
    second = _plain(
        orchestrator.render_pipeline(
            busier, workspace=None, palette="dark", levels_path=levels
        ).split("\n")
    )
    assert second[2].endswith(" 30% ↑+7")
    assert second[3].endswith(" 41%")


def test_bar_rows_show_a_falling_trend_and_omit_a_zero_one() -> None:
    bars = [
        PowerlineBar(label="5h", ratio=0.2, trend=-3),
        PowerlineBar(label="7d", ratio=0.2, trend=0),
    ]
    rows = _plain(_render(segments=[], bars=bars))
    assert rows == [f" 5h  {_ARROW} ██░░░░░░░░  20% ↓-3", f" 7d  {_ARROW} ██░░░░░░░░  20%"]


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


def test_goldens() -> None:
    regen = bool(os.environ.get("EAWF_SNAPSHOT_REGEN"))
    for palette in ("light", "dark"):
        for width in (80, 120):
            rows = _render(palette, width)
            path = _GOLDEN_DIR / f"{palette}-{width}.ansi"
            rendered = "\n".join(rows) + "\n"
            if regen:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(rendered.encode("utf-8"))
            assert path.read_bytes() == rendered.encode("utf-8"), path.name
            assert all("\n" not in row and _cells(row) <= width for row in rows)

    # Line 1 drops MCP, then active time, then project, and keeps model and cost.
    kept: list[list[str]] = []
    for width in range(120, 13, -1):
        rows = _render(width=width)
        assert all(_cells(row) <= width for row in rows), width
        texts = _line_one_texts(rows)
        if not kept or texts != kept[-1]:
            kept.append(texts)
    order = [[text.split()[0] for text in step] for step in kept]
    assert order[:5] == [
        ["Opus", "sample-project", "12m", "$1.22", "MCP:", "hook-errors:2"],
        ["Opus", "sample-project", "12m", "$1.22", "hook-errors:2"],
        ["Opus", "sample-project", "$1.22", "hook-errors:2"],
        ["Opus", "$1.22", "hook-errors:2"],
        ["Opus", "$1.22"],
    ]
    assert kept[-1][-1] == "$1.22" and kept[-1][0].endswith("…")

    # A bar row shrinks its bar to five cells, then drops the trend, then the reset.
    def ctx_row(width: int) -> str | None:
        rows = _plain(_render(width=width, segments=[], bars=_bars()[:1]))
        return rows[0] if rows else None

    assert ctx_row(35) == f" ctx {_ARROW} ▋░░░░░░░░░   6% ↑+3 64K/1.0M"
    assert ctx_row(34) == f" ctx {_ARROW} ▍░░░░   6% ↑+3 64K/1.0M"
    assert ctx_row(29) == f" ctx {_ARROW} ▍░░░░   6% 64K/1.0M"
    assert ctx_row(25) == f" ctx {_ARROW} ▍░░░░   6%"
    assert ctx_row(16) is None


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
        ({"total_api_duration_ms": 754_000, "total_duration_ms": 1_216_000}, "12m · 62% active"),
        ({"total_api_duration_ms": 59_999, "total_duration_ms": 60_000}, "0m · 100% active"),
        ({"total_api_duration_ms": 0, "total_duration_ms": 1}, "0m · 0% active"),
        ({"total_api_duration_ms": 9_000, "total_duration_ms": 1_000}, "0m · 100% active"),
        ({"total_api_duration_ms": 1_000, "total_duration_ms": 0}, None),
        ({"total_duration_ms": 1_000}, None),
        ({"total_api_duration_ms": 1_000}, None),
        ({"total_api_duration_ms": "1000", "total_duration_ms": 2_000}, None),
    ],
)
def test_active_time_is_api_minutes_and_their_share(
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
