"""SURF-117: the statusline reads the host rate-limit block and keeps no idle helper.

The powerline draws ``five_hour`` and ``seven_day`` as the ``5h`` and ``7d`` bar rows
with their used percentage and the time left to their reset. A window the block omits
or states without a usable fraction has no row rather than a zero, and a reset time
that cannot be read drops only that row's countdown. Every public render helper of
the statusline has a production caller, and the orchestrator's per-session line cache
is the only cache the statusline keeps.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.runtimes.claude import statusline as orchestrator
from eawf.runtime.runtimes.claude.statusline_modules import rate_window
from eawf.surfaces.render import statusline

_SRC = Path(statusline.__file__).resolve().parents[2]
_NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def _payload(**windows: Any) -> dict[str, Any]:
    return {"rate_limits": windows}


def test_bar_rows_draw_five_hour_then_seven_day_with_their_countdowns() -> None:
    rows = rate_window.bar_rows(
        _payload(
            seven_day={
                "utilization": 0.41,
                "resets_at": (_NOW + timedelta(days=6, hours=14)).isoformat(),
            },
            five_hour={
                "used_percentage": 23,
                "resets_at": (_NOW + timedelta(minutes=125)).timestamp(),
            },
        ),
        _NOW,
    )
    assert [(row.label, row.percent, row.detail) for row in rows] == [
        ("5h", 23, "2h05m"),
        ("7d", 41, "6d14h"),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"rate_limits": "full"},
        {"rate_limits": {}},
        _payload(five_hour={"used_percentage": 101}, seven_day=[1]),
        _payload(opus_week={"used_percentage": 5}),
    ],
)
def test_bar_rows_draw_nothing_for_an_absent_or_unusable_window(payload: dict[str, Any]) -> None:
    # boundary / error-path: no block, a malformed block, out-of-range, unknown window.
    assert rate_window.bar_rows(payload, _NOW) == []


def test_bar_rows_draw_only_the_window_the_plan_has() -> None:
    rows = rate_window.bar_rows(_payload(seven_day={"used_percentage": 100}), _NOW)
    assert [(row.label, row.ratio, row.detail) for row in rows] == [("7d", 1.0, None)]


def test_bar_rows_count_a_passed_reset_down_from_zero() -> None:
    [row] = rate_window.bar_rows(
        _payload(
            five_hour={"used_percentage": 0, "resets_at": (_NOW - timedelta(minutes=3)).isoformat()}
        ),
        _NOW,
    )
    assert (row.percent, row.detail) == (0, "0m")


@pytest.mark.parametrize(
    "resets_at",
    [
        pytest.param(1e300, id="past-the-time-range"),
        pytest.param(-1e300, id="before-the-time-range"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinite"),
        pytest.param("soon", id="unparseable-text"),
        pytest.param([1], id="wrong-type"),
    ],
)
def test_bar_rows_an_unreadable_reset_drops_only_its_countdown(resets_at: object) -> None:
    rows = rate_window.bar_rows(
        _payload(
            five_hour={"used_percentage": 23, "resets_at": resets_at},
            seven_day={"used_percentage": 41, "resets_at": (_NOW + timedelta(hours=3)).isoformat()},
        ),
        _NOW,
    )
    assert [(row.label, row.percent, row.detail) for row in rows] == [
        ("5h", 23, None),
        ("7d", 41, "3h00m"),
    ]


@pytest.mark.parametrize(
    ("offset_seconds", "expected"),
    [
        pytest.param(125 * 60, "2h05m", id="milliseconds"),
        pytest.param(-60, "0m", id="milliseconds-already-passed"),
    ],
)
def test_bar_rows_read_a_reset_in_epoch_milliseconds(offset_seconds: int, expected: str) -> None:
    resets_at_ms = int((_NOW.timestamp() + offset_seconds) * 1000)
    [row] = rate_window.bar_rows(
        _payload(five_hour={"used_percentage": 5, "resets_at": resets_at_ms}), _NOW
    )
    assert row.detail == expected


def test_bar_rows_read_seconds_just_under_the_milliseconds_threshold() -> None:
    # boundary: the largest epoch-seconds value still read as seconds lies in year 5138.
    [row] = rate_window.bar_rows(
        _payload(five_hour={"used_percentage": 5, "resets_at": 99_999_999_999}), _NOW
    )
    assert row.detail is not None and row.detail.endswith("h")


def test_the_orchestrator_keeps_the_rate_rows_when_the_context_row_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eawf.runtime.runtimes.claude.statusline_modules import context_tokens

    def boom(payload: dict[str, Any]) -> None:
        raise RuntimeError("context producer failed")

    monkeypatch.setattr(context_tokens, "bar_row", boom)
    bars = orchestrator._build_bars(
        _payload(five_hour={"used_percentage": 5}, seven_day={"used_percentage": 6}), _NOW
    )
    assert [bar.label for bar in bars] == ["5h", "7d"]


def test_surf_117_the_orchestrator_draws_the_rate_windows_as_bar_rows() -> None:
    bars = orchestrator._build_bars(_payload(five_hour={"used_percentage": 5}), _NOW)
    assert [bar.label for bar in bars] == ["5h"]


def _referenced_names(root: Path, *, skip: Path) -> set[str]:
    names: set[str] = set()
    for path in root.rglob("*.py"):
        if path == skip:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == statusline.__name__:
                names.update(alias.name for alias in node.names)
    return names


def test_surf_117_every_public_render_helper_has_a_production_caller() -> None:
    functions = {
        name for name in statusline.__all__ if inspect.isfunction(getattr(statusline, name))
    }
    used = _referenced_names(_SRC, skip=Path(statusline.__file__).resolve())
    internal = Path(statusline.__file__).read_text(encoding="utf-8")
    idle = sorted(name for name in functions if name not in used and internal.count(f"{name}(") < 2)
    assert idle == []
    for retired in (
        "render_rows",
        "context_usage_segment",
        "terminal_supports_color",
        "render_segments",
        "load_themes",
        "resolve_theme",
    ):
        assert not hasattr(statusline, retired)


def test_surf_117_the_line_cache_is_the_only_statusline_cache() -> None:
    package = Path(rate_window.__file__).parent
    for path in [*package.glob("*.py"), Path(statusline.__file__)]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        decorators = {
            ast.unparse(decorator)
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            for decorator in node.decorator_list
        }
        assert not {d for d in decorators if "cache" in d}, path.name
    assert orchestrator.cache_path_for("s").parent == orchestrator._cache_root()
