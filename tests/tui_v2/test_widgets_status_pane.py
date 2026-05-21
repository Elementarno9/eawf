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
    _active_phase_id,
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


def _state_with_archived_phase_pending_waves() -> State:
    """Return the active fixture plus an archived phase holding pending waves.

    Mirrors the production bug: an ``archived`` phase whose ``planned``
    iter still owns PENDING waves. The active P01 phase keeps its single
    in_progress wave; the archived P09 phase adds three PENDING zombies
    that must NOT inflate the active-phase-scoped counters.
    """
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    opened = payload["phases"]["P01"]["opened_at"]
    payload["phases"]["P09"] = {
        "id": "P09",
        "scope_id": payload["phases"]["P01"]["scope_id"],
        "subproject_id": None,
        "title": "dropped",
        "status": "archived",
        "iter_ids": ["P09-I01"],
        "outcome_ids": [],
        "depends_on": [],
        "source_brief_ids": [],
        "opened_at": opened,
        "closed_at": opened,
        "audit_id": None,
    }
    payload["iters"]["P09-I01"] = {
        "id": "P09-I01",
        "phase_id": "P09",
        "title": "dropped iter",
        "status": "planned",
        "wave_ids": ["P09-I01-W01", "P09-I01-W02", "P09-I01-W03"],
        "estimate_id": None,
        "audit_id": None,
        "opened_at": opened,
        "closed_at": None,
    }
    for n in (1, 2, 3):
        wid = f"P09-I01-W0{n}"
        payload["waves"][wid] = {
            "id": wid,
            "iter_id": "P09-I01",
            "title": f"zombie {n}",
            "status": "pending",
            "deps": [],
            "blocks": [],
            "file_scopes": [],
            "success_criteria": [],
            "opened_at": opened,
            "closed_at": None,
        }
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


def test_summary_counts_excludes_archived_phase_pending_waves() -> None:
    """Pending waves under an archived phase do not inflate the live count.

    The active P01 phase carries one in_progress wave; the archived P09
    phase adds three PENDING zombies. The wave counters must reflect only
    the active phase: 1 in_progress, 0 pending.
    """
    counts = summary_counts(_state_with_archived_phase_pending_waves())
    assert counts["waves_in_progress"] == 1
    assert counts["waves_pending"] == 0


def test_summary_counts_scopes_to_current_pointer_phase() -> None:
    """Counters scope to ``current.phase_id`` even with other active phases.

    Build a state whose pointer targets P01 (in_progress wave) while a
    second ACTIVE phase P02 carries a PENDING wave; only the pointer
    phase's waves are counted.
    """
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    opened = payload["phases"]["P01"]["opened_at"]
    payload["phases"]["P02"] = {
        "id": "P02",
        "scope_id": payload["phases"]["P01"]["scope_id"],
        "subproject_id": None,
        "title": "other active",
        "status": "active",
        "iter_ids": ["P02-I01"],
        "outcome_ids": [],
        "depends_on": [],
        "source_brief_ids": [],
        "opened_at": opened,
        "closed_at": None,
        "audit_id": None,
    }
    payload["iters"]["P02-I01"] = {
        "id": "P02-I01",
        "phase_id": "P02",
        "title": "other iter",
        "status": "active",
        "wave_ids": ["P02-I01-W01"],
        "estimate_id": None,
        "audit_id": None,
        "opened_at": opened,
        "closed_at": None,
    }
    payload["waves"]["P02-I01-W01"] = {
        "id": "P02-I01-W01",
        "iter_id": "P02-I01",
        "title": "other pending",
        "status": "pending",
        "deps": [],
        "blocks": [],
        "file_scopes": [],
        "success_criteria": [],
        "opened_at": opened,
        "closed_at": None,
    }
    state = State.model_validate(payload)
    assert state.current.phase_id == "P01"
    counts = summary_counts(state)
    assert counts["waves_in_progress"] == 1
    assert counts["waves_pending"] == 0


# --------------------------------------------------------------------------
# _active_phase_id — pointer must be ACTIVE before it is honoured
# --------------------------------------------------------------------------


def _state_with_stale_pointer_and_active_phase() -> State:
    """Pointer at a closed P01; a separate ACTIVE P02 owns the live wave.

    The pointer must NOT win (P01 is closed); resolution falls through to
    the ACTIVE-phase scan and lands on P02.
    """
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    opened = payload["phases"]["P01"]["opened_at"]
    # Demote the pointer phase to a non-ACTIVE (closed) status; its wave
    # becomes a closed leftover that must not be counted.
    payload["phases"]["P01"]["status"] = "closed"
    payload["phases"]["P01"]["closed_at"] = opened
    payload["waves"]["P01-I01-W01"]["status"] = "closed"
    payload["waves"]["P01-I01-W01"]["closed_at"] = opened
    payload["iters"]["P01-I01"]["status"] = "closed"
    payload["iters"]["P01-I01"]["closed_at"] = opened
    payload["phases"]["P02"] = {
        "id": "P02",
        "scope_id": payload["phases"]["P01"]["scope_id"],
        "subproject_id": None,
        "title": "active",
        "status": "active",
        "iter_ids": ["P02-I01"],
        "outcome_ids": [],
        "depends_on": [],
        "source_brief_ids": [],
        "opened_at": opened,
        "closed_at": None,
        "audit_id": None,
    }
    payload["iters"]["P02-I01"] = {
        "id": "P02-I01",
        "phase_id": "P02",
        "title": "active iter",
        "status": "active",
        "wave_ids": ["P02-I01-W01"],
        "estimate_id": None,
        "audit_id": None,
        "opened_at": opened,
        "closed_at": None,
    }
    payload["waves"]["P02-I01-W01"] = {
        "id": "P02-I01-W01",
        "iter_id": "P02-I01",
        "title": "live pending",
        "status": "pending",
        "deps": [],
        "blocks": [],
        "file_scopes": [],
        "success_criteria": [],
        "opened_at": opened,
        "closed_at": None,
    }
    return State.model_validate(payload)


def test_active_phase_id_ignores_non_active_pointer() -> None:
    """A pointer at a closed phase is not returned; the ACTIVE scan wins."""
    state = _state_with_stale_pointer_and_active_phase()
    assert state.current.phase_id == "P01"
    assert _active_phase_id(state) == "P02"


def test_active_phase_id_non_active_pointer_no_active_phase_returns_none() -> None:
    """A non-ACTIVE pointer with no ACTIVE phase anywhere resolves to None."""
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    opened = payload["phases"]["P01"]["opened_at"]
    payload["phases"]["P01"]["status"] = "archived"
    payload["phases"]["P01"]["closed_at"] = opened
    payload["waves"]["P01-I01-W01"]["status"] = "abandoned"
    payload["waves"]["P01-I01-W01"]["closed_at"] = opened
    payload["iters"]["P01-I01"]["status"] = "abandoned"
    payload["iters"]["P01-I01"]["closed_at"] = opened
    state = State.model_validate(payload)
    assert state.current.phase_id == "P01"
    assert _active_phase_id(state) is None


def test_summary_counts_ignores_non_active_pointer_waves() -> None:
    """Counts scope to the ACTIVE phase, not the stale (closed) pointer.

    The closed P01 leftover wave must not be counted; only P02's single
    PENDING wave is in scope.
    """
    counts = summary_counts(_state_with_stale_pointer_and_active_phase())
    assert counts["waves_pending"] == 1
    assert counts["waves_in_progress"] == 0


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


def test_build_status_lines_waves_line_scoped_to_active_phase() -> None:
    """The rendered waves line shows the active-phase count, not the global one."""
    lines = build_status_lines(_state_with_archived_phase_pending_waves())
    waves_line = next(line for line in lines if line.startswith("waves:"))
    assert "1 active · 0 pending" in waves_line


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
