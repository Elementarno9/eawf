"""Unit + Pilot tests for the C06 ``StatusPane`` widget (P26-W17).

Covers the pure counters (:func:`summary_counts`), the rendered line set
(:func:`build_status_lines`) including the None-state placeholder frame
and the blocked-wave line, and a Pilot-driven paint under the real
palette.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson
from textual.app import App, ComposeResult

from eawf.state.models import State
from eawf.tui_v2.widgets.status_pane import (
    DASH,
    DEFAULT_PROJECT_CODE,
    StatusPane,
    build_status_lines,
    summary_counts,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_EMPTY_REPO = _FIXTURES / "01-empty-repo.json"
_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "tui_v2" / "theme.tcss"


class _Harness(App[None]):
    """Production-style host loading the real palette CSS."""

    CSS_PATH = str(_THEME)

    def compose(self) -> ComposeResult:
        yield StatusPane(id="sp")


def _load(path: Path) -> State:
    return State.model_validate(orjson.loads(path.read_bytes()))


def _state_with_failed_wave() -> State:
    """Return the active fixture with its wave flipped to ``failed``."""
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    payload["waves"]["P01-I01-W01"]["status"] = "failed"
    return State.model_validate(payload)


# --------------------------------------------------------------------------
# summary_counts — boundary (None / empty) + populated
# --------------------------------------------------------------------------


def test_summary_counts_none_state_all_zero() -> None:
    counts = summary_counts(None)
    assert all(value == 0 for value in counts.values())
    # Every documented key is present even on the empty frame.
    assert {
        "phases_active",
        "iters_active",
        "waves_pending",
        "waves_in_progress",
        "waves_failed",
        "audits_running",
        "audits_total",
        "worktrees_active",
    } <= set(counts)


def test_summary_counts_active_phase_iter_wave() -> None:
    counts = summary_counts(_load(_PHASE_ITER_WAVE))
    assert counts["phases_active"] == 1
    assert counts["iters_active"] == 1
    assert counts["waves_in_progress"] == 1
    assert counts["waves_pending"] == 0


def test_summary_counts_failed_wave_counted_as_blocked() -> None:
    counts = summary_counts(_state_with_failed_wave())
    assert counts["waves_failed"] == 1


# --------------------------------------------------------------------------
# build_status_lines — placeholder frame + populated + blocked
# --------------------------------------------------------------------------


def test_build_status_lines_none_state_placeholder_frame() -> None:
    lines = build_status_lines(None)
    joined = "\n".join(lines)
    assert DEFAULT_PROJECT_CODE in joined
    assert f"phase:     {DASH}" in joined
    assert f"iter:      {DASH}" in joined
    # No blocked line when nothing failed.
    assert not any(line.startswith("blocked:") for line in lines)


def test_build_status_lines_populated_pointers() -> None:
    lines = build_status_lines(_load(_PHASE_ITER_WAVE))
    joined = "\n".join(lines)
    assert "project:   QR" in joined
    assert "phase:     P01" in joined
    assert "iter:      P01-I01" in joined


def test_build_status_lines_blocked_line_present_on_failed_wave() -> None:
    lines = build_status_lines(_state_with_failed_wave())
    assert any(line.startswith("blocked:") and "1 failed" in line for line in lines)


def test_build_status_lines_empty_repo_no_blocked_line() -> None:
    lines = build_status_lines(_load(_EMPTY_REPO))
    assert not any(line.startswith("blocked:") for line in lines)


# --------------------------------------------------------------------------
# Pilot paint — renders under the real palette
# --------------------------------------------------------------------------


def test_status_pane_paints_pointers() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(60, 12)) as pilot:
            await pilot.pause()
            app.query_one("#sp", StatusPane).state = _load(_PHASE_ITER_WAVE)
            await pilot.pause()
            rendered = app.export_screenshot()
            assert "P01" in rendered
            assert "QR" in rendered

    asyncio.run(body())


def test_status_pane_paints_blocked_line_under_palette() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(60, 12)) as pilot:
            await pilot.pause()
            app.query_one("#sp", StatusPane).state = _state_with_failed_wave()
            await pilot.pause()
            rendered = app.export_screenshot()
            assert "blocked:" in rendered

    asyncio.run(body())
