"""Unit + Pilot tests for the C06 ``StatusPane`` widget.

Covers the pure counters (:func:`summary_counts`), the grouped rendered
line set (:func:`build_status_lines` — LIFECYCLE / EFFORT / GATES), the
per-day EU velocity series (:func:`build_velocity_eu_per_day`), and a
Pilot-driven paint under the real palette. The None-state placeholder
frame, the blocked-wave line, the EFFORT empty-state sentinels, and the
GATES audit-progress collapse are all asserted against the pure builders.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import orjson
import pytest
from textual.app import ComposeResult

from eawf.state.models import State
from eawf.tui.widgets.eu_bar import EMPTY_STATE
from eawf.tui.widgets.status_pane import (
    DASH,
    DEFAULT_PROJECT_CODE,
    GATE_FAIL,
    GATE_PASS,
    GATE_RUNNING,
    VELOCITY_WINDOW_DAYS,
    StatusPane,
    _active_phase_id,
    build_status_lines,
    build_velocity_eu_per_day,
    summary_counts,
)

from ._palette_harness import PaletteHarnessApp

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_EMPTY_REPO = _FIXTURES / "01-empty-repo.json"
_ESTIMATES_ACTUALS = _FIXTURES / "09-estimates-and-actuals.json"
_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "tui" / "theme.tcss"


class _Harness(PaletteHarnessApp):
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


def _state_half_closed() -> State:
    """Return the fixture with the active iter holding 2 waves, 1 CLOSED.

    Gives the active-phase completion bar a deterministic ``1/2``.
    """
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    opened = payload["phases"]["P01"]["opened_at"]
    payload["iters"]["P01-I01"]["wave_ids"] = ["P01-I01-W01", "P01-I01-W02"]
    payload["waves"]["P01-I01-W01"]["status"] = "closed"
    payload["waves"]["P01-I01-W01"]["closed_at"] = opened
    payload["waves"]["P01-I01-W02"] = {
        "id": "P01-I01-W02",
        "iter_id": "P01-I01",
        "title": "second",
        "status": "in_progress",
        "deps": [],
        "blocks": [],
        "file_scopes": [],
        "success_criteria": [],
        "opened_at": opened,
        "closed_at": None,
    }
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


def _add_actual(
    payload: dict[str, Any],
    *,
    actual_id: str,
    scope_id: str,
    elapsed_eu: float,
    updated_at: str,
    status: str = "active",
) -> None:
    """Splice an :class:`ActualSummary` row onto a fixture payload in place."""
    payload.setdefault("actuals", {})[actual_id] = {
        "id": actual_id,
        "scope_id": scope_id,
        "status": status,
        "elapsed_eu": elapsed_eu,
        "attention_eu": None,
        "agent_runtime_eu": None,
        "current_store_record_id": f"{actual_id}-REC",
        "updated_at": updated_at,
    }


def _state_with_audit(
    *,
    status: str,
    check_results: list[dict[str, Any]],
    verdict: str | None,
    audit_id: str = "A07-P01",
) -> State:
    """Return the active fixture with an iter-attached audit row."""
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    payload["iters"]["P01-I01"]["audit_id"] = audit_id
    payload["audits"] = {
        audit_id: {
            "id": audit_id,
            "scope_id": "P01-I01",
            "kind": "evaluation",
            "status": status,
            "report_artifact_id": None,
            "check_results": check_results,
            "integrity_results": [],
            "created_at": payload["phases"]["P01"]["opened_at"],
            "verdict": verdict,
        }
    }
    return State.model_validate(payload)


def _section(lines: list[str], header: str) -> list[str]:
    """Return the lines under *header* up to the next blank line / header."""
    headers = {"LIFECYCLE", "EFFORT", "GATES"}
    out: list[str] = []
    collecting = False
    for line in lines:
        if line == header:
            collecting = True
            continue
        if collecting:
            if line == "" or line in headers:
                break
            out.append(line)
    return out


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
        "waves_closed",
        "waves_total",
        "waves_failed",
        "audits_running",
        "audits_total",
        "worktrees_active",
    } <= set(counts)


def test_summary_counts_closed_and_total_scoped_to_active_phase() -> None:
    counts = summary_counts(_state_half_closed())
    assert counts["waves_closed"] == 1
    assert counts["waves_total"] == 2


def test_summary_counts_archived_pending_excluded_from_total() -> None:
    """Zombie pending waves under an archived phase do not inflate the total."""
    counts = summary_counts(_state_with_archived_phase_pending_waves())
    assert counts["waves_total"] == 1  # only the active P01 wave
    assert counts["waves_closed"] == 0


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
# build_status_lines — grouped LIFECYCLE / EFFORT / GATES structure
# --------------------------------------------------------------------------


def test_build_status_lines_groups_into_three_sections() -> None:
    """The render carries LIFECYCLE / EFFORT / GATES headers in order."""
    lines = build_status_lines(_load(_PHASE_ITER_WAVE))
    assert "LIFECYCLE" in lines
    assert "EFFORT" in lines
    assert "GATES" in lines
    assert lines.index("LIFECYCLE") < lines.index("EFFORT") < lines.index("GATES")


def test_build_status_lines_no_dispatch_band() -> None:
    """The DISPATCH band is W05's scope — it must not appear yet."""
    lines = build_status_lines(_load(_ESTIMATES_ACTUALS))
    assert "DISPATCH" not in lines
    assert not any(line.lstrip().startswith(("NOW", "NEXT", "WAIT")) for line in lines)


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
    lifecycle = _section(lines, "LIFECYCLE")
    joined = "\n".join(lifecycle)
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


def test_build_status_lines_progress_line_shows_completion_bar() -> None:
    """The progress line carries the active-phase completion bar (closed/total)."""
    lines = build_status_lines(_state_half_closed())
    progress_line = next(line for line in lines if line.startswith("progress:"))
    assert "1/2" in progress_line
    assert "#" in progress_line  # at least one filled cell


def test_build_status_lines_progress_line_empty_state_when_no_waves() -> None:
    """A scope with no child waves shows the bar's empty-state, not ``0/0``."""
    lines = build_status_lines(_load(_EMPTY_REPO))
    progress_line = next(line for line in lines if line.startswith("progress:"))
    assert EMPTY_STATE in progress_line


def test_build_status_lines_progress_line_none_state_empty_state() -> None:
    """The None-state frame renders the progress bar's empty-state sentinel."""
    lines = build_status_lines(None)
    progress_line = next(line for line in lines if line.startswith("progress:"))
    assert EMPTY_STATE in progress_line


# --------------------------------------------------------------------------
# EFFORT block — consumed/estimate EU + variance + sparkline + ETA
# --------------------------------------------------------------------------


def test_build_status_lines_effort_shows_consumed_estimate_eu() -> None:
    """EFFORT surfaces the active iter's consumed/estimate EU + a fill bar."""
    lines = build_status_lines(_load(_ESTIMATES_ACTUALS))
    effort = next(line for line in lines if line.startswith("effort:"))
    # Fixture 09: actual elapsed 1.2 EU vs estimate 4.5 EU on P01-I01-W01.
    assert "1.2/4.5" in effort
    assert "#" in effort  # filled cells (~27% consumed)


def test_build_status_lines_effort_shows_signed_variance_pct() -> None:
    """EFFORT surfaces the signed M26 variance % (under-run is negative)."""
    lines = build_status_lines(_load(_ESTIMATES_ACTUALS))
    variance = next(line for line in lines if line.startswith("variance:"))
    # (1.2 - 4.5) / 4.5 * 100 = -73.3 % (a hard under-run so far).
    assert "-73.3%" in variance


def test_build_status_lines_effort_shows_velocity_sparkline() -> None:
    """EFFORT surfaces an EU/day velocity sparkline glyph run (Braille mode)."""
    lines = build_status_lines(_load(_ESTIMATES_ACTUALS), mode="braille")
    velocity = next(line for line in lines if line.startswith("velocity:"))
    # One actual carrying 1.2 EU → a single populated day → not empty state.
    assert EMPTY_STATE not in velocity
    assert any(g in velocity for g in ("▁", "█", "▇"))


def test_build_status_lines_effort_shows_eta_date() -> None:
    """EFFORT surfaces an ISO ETA date projected from the current burn."""
    lines = build_status_lines(_load(_ESTIMATES_ACTUALS))
    eta = next(line for line in lines if line.startswith("eta:"))
    # remaining 3.3 EU at ~1.2 EU/day projects a real finish date.
    body = eta.split("eta:")[1].strip()
    assert body != DASH
    datetime.strptime(body, "%Y-%m-%d")  # parses as an ISO date


def test_build_status_lines_effort_no_data_empty_state() -> None:
    """No estimate / actuals → ``— no data``, never a fabricated 0 % bar."""
    lines = build_status_lines(_load(_PHASE_ITER_WAVE))
    effort = next(line for line in lines if line.startswith("effort:"))
    variance = next(line for line in lines if line.startswith("variance:"))
    velocity = next(line for line in lines if line.startswith("velocity:"))
    eta = next(line for line in lines if line.startswith("eta:"))
    assert EMPTY_STATE in effort
    assert "0%" not in effort  # not a fake 0 % bar
    assert EMPTY_STATE in variance
    assert EMPTY_STATE in velocity
    assert eta.split("eta:")[1].strip() == DASH


def test_build_status_lines_effort_ascii_mode() -> None:
    """ASCII mode renders the EU bar with ``#``/``-`` and ASCII spark glyphs."""
    lines = build_status_lines(_load(_ESTIMATES_ACTUALS), mode="ascii")
    effort = next(line for line in lines if line.startswith("effort:"))
    velocity = next(line for line in lines if line.startswith("velocity:"))
    assert "#" in effort
    assert "▁" not in velocity and "█" not in velocity  # no Braille block glyphs
    assert any(g in velocity for g in (".", "@", "#"))


# --------------------------------------------------------------------------
# build_velocity_eu_per_day — per-day EU series over the window
# --------------------------------------------------------------------------


def test_velocity_eu_per_day_sums_actuals_per_day() -> None:
    """The series sums in-scope actuals' elapsed_eu into per-day buckets."""
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    # Two actuals on the same scope, two distinct days within the window.
    _add_actual(
        payload,
        actual_id="ACT-A",
        scope_id="P01-I01-W01",
        elapsed_eu=2.0,
        updated_at="2026-05-06T09:00:00Z",
    )
    _add_actual(
        payload,
        actual_id="ACT-B",
        scope_id="P01-I01-W01",
        elapsed_eu=1.5,
        updated_at="2026-05-08T15:00:00Z",
    )
    state = State.model_validate(payload)
    series = build_velocity_eu_per_day(state, days=7)
    assert len(series) == 7
    # Anchor day = 2026-05-08 (the latest). Window is 05-02..05-08.
    # 05-06 carries 2.0, 05-08 carries 1.5, everything else 0.0.
    assert series == pytest.approx([0.0, 0.0, 0.0, 0.0, 2.0, 0.0, 1.5])


def test_velocity_eu_per_day_same_day_actuals_summed() -> None:
    """Two actuals on the same day collapse into one summed bucket."""
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    _add_actual(
        payload,
        actual_id="ACT-A",
        scope_id="P01-I01-W01",
        elapsed_eu=1.0,
        updated_at="2026-05-08T09:00:00Z",
    )
    _add_actual(
        payload,
        actual_id="ACT-B",
        scope_id="P01-I01",
        elapsed_eu=0.5,
        updated_at="2026-05-08T18:00:00Z",
    )
    state = State.model_validate(payload)
    series = build_velocity_eu_per_day(state, days=1)
    assert series == pytest.approx([1.5])


def test_velocity_eu_per_day_none_state_all_zero() -> None:
    """A ``None`` state yields a fixed-length all-zero window."""
    series = build_velocity_eu_per_day(None, days=VELOCITY_WINDOW_DAYS)
    assert series == [0.0] * VELOCITY_WINDOW_DAYS


def test_velocity_eu_per_day_no_actuals_all_zero() -> None:
    """A state with no in-scope actuals yields an all-zero window."""
    series = build_velocity_eu_per_day(_load(_PHASE_ITER_WAVE), days=7)
    assert series == [0.0] * 7


def test_velocity_eu_per_day_single_day_window() -> None:
    """A 1-day window returns exactly the anchor day's EU sum."""
    series = build_velocity_eu_per_day(_load(_ESTIMATES_ACTUALS), days=1)
    assert series == pytest.approx([1.2])


def test_velocity_eu_per_day_seven_day_edge() -> None:
    """A 7-day window returns exactly seven entries (boundary length)."""
    series = build_velocity_eu_per_day(_load(_ESTIMATES_ACTUALS), days=7)
    assert len(series) == 7
    assert series[-1] == pytest.approx(1.2)  # anchor day carries the burn
    assert series[:-1] == pytest.approx([0.0] * 6)


def test_velocity_eu_per_day_excludes_out_of_window_actual() -> None:
    """An actual older than the window contributes nothing to the series."""
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    _add_actual(
        payload,
        actual_id="ACT-OLD",
        scope_id="P01-I01-W01",
        elapsed_eu=9.0,
        updated_at="2026-01-01T00:00:00Z",
    )
    _add_actual(
        payload,
        actual_id="ACT-NEW",
        scope_id="P01-I01-W01",
        elapsed_eu=3.0,
        updated_at="2026-05-08T00:00:00Z",
    )
    state = State.model_validate(payload)
    series = build_velocity_eu_per_day(state, days=3)
    # Anchor 05-08; window 05-06..05-08. The Jan actual is outside it.
    assert series == pytest.approx([0.0, 0.0, 3.0])
    assert 9.0 not in series


def test_velocity_eu_per_day_excludes_other_iter_actuals() -> None:
    """Only the active iter's subtree actuals are summed, not siblings'."""
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    _add_actual(
        payload,
        actual_id="ACT-MINE",
        scope_id="P01-I01-W01",
        elapsed_eu=2.0,
        updated_at="2026-05-08T00:00:00Z",
    )
    # An actual scoped to a wave under a different (non-active) iter.
    _add_actual(
        payload,
        actual_id="ACT-OTHER",
        scope_id="P02-I01-W01",
        elapsed_eu=5.0,
        updated_at="2026-05-08T00:00:00Z",
    )
    state = State.model_validate(payload)
    series = build_velocity_eu_per_day(state, days=1)
    assert series == pytest.approx([2.0])  # the foreign 5.0 is excluded


def test_velocity_eu_per_day_rejects_non_positive_days() -> None:
    """A window of < 1 day is a programmer error and raises."""
    with pytest.raises(ValueError, match="days must be >= 1"):
        build_velocity_eu_per_day(_load(_ESTIMATES_ACTUALS), days=0)


# --------------------------------------------------------------------------
# GATES block — live audit_check_* N/M progress + verdict collapse
# --------------------------------------------------------------------------


def test_build_status_lines_gate_empty_state_when_no_audit() -> None:
    """No audit on the active iter → the GATES block shows the empty state."""
    lines = build_status_lines(_load(_PHASE_ITER_WAVE))
    gate = next(line for line in lines if line.startswith("gate:"))
    assert EMPTY_STATE in gate


def test_build_status_lines_gate_shows_n_of_m_progress() -> None:
    """A running audit shows ``N/M`` with per-check glyphs (✓ ✗ ⏳, Braille)."""
    state = _state_with_audit(
        status="running",
        check_results=[
            {"name": "ruff_clean", "passed": True, "details": None},
            {"name": "mypy_strict", "passed": False, "details": "3 errors"},
            {"name": "pytest_pass", "passed": None, "details": None},
        ],
        verdict=None,
    )
    lines = build_status_lines(state, mode="braille")
    gate = next(line for line in lines if line.startswith("gate:"))
    assert "2/3" in gate  # two reported (pass + fail), one still running
    assert GATE_PASS in gate
    assert GATE_FAIL in gate
    assert GATE_RUNNING in gate


def test_build_status_lines_gate_collapses_to_verdict() -> None:
    """A completed audit collapses the N/M progress to ``A<id> <verdict>``."""
    state = _state_with_audit(
        status="complete",
        check_results=[
            {"name": "ruff_clean", "passed": True, "details": None},
            {"name": "mypy_strict", "passed": True, "details": None},
        ],
        verdict="pass",
        audit_id="A12-P01",
    )
    lines = build_status_lines(state)
    gate = next(line for line in lines if line.startswith("gate:"))
    assert "A12-P01 pass" in gate
    assert "/" not in gate.split("gate:")[1]  # no N/M once collapsed


def test_build_status_lines_gate_ascii_mode() -> None:
    """ASCII mode renders the per-check glyphs as plain fallbacks."""
    state = _state_with_audit(
        status="running",
        check_results=[
            {"name": "a", "passed": True, "details": None},
            {"name": "b", "passed": False, "details": None},
        ],
        verdict=None,
    )
    lines = build_status_lines(state, mode="ascii")
    gate = next(line for line in lines if line.startswith("gate:"))
    assert "2/2" in gate  # both checks reported (pass + fail)
    assert GATE_PASS not in gate and GATE_FAIL not in gate  # no Unicode glyphs
    assert "P" in gate and "x" in gate


# --------------------------------------------------------------------------
# Pilot paint — renders under the real palette
# --------------------------------------------------------------------------


def test_status_pane_paints_pointers() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(60, 20)) as pilot:
            await pilot.pause()
            app.query_one("#sp", StatusPane).state = _load(_PHASE_ITER_WAVE)
            await pilot.pause()
            await app.workers.wait_for_complete()
            rendered = app.export_screenshot()
            assert "P01" in rendered
            assert "QR" in rendered
            assert "LIFECYCLE" in rendered
            assert "EFFORT" in rendered
            assert "GATES" in rendered

    asyncio.run(body())


def test_status_pane_paints_blocked_line_under_palette() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(60, 20)) as pilot:
            await pilot.pause()
            app.query_one("#sp", StatusPane).state = _state_with_failed_wave()
            await pilot.pause()
            await app.workers.wait_for_complete()
            rendered = app.export_screenshot()
            assert "blocked:" in rendered

    asyncio.run(body())


def test_status_pane_paints_effort_block_under_palette() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(60, 20)) as pilot:
            await pilot.pause()
            app.query_one("#sp", StatusPane).state = _load(_ESTIMATES_ACTUALS)
            await pilot.pause()
            await app.workers.wait_for_complete()
            rendered = app.export_screenshot()
            assert "effort:" in rendered
            assert "1.2/4.5" in rendered
            assert "variance:" in rendered

    asyncio.run(body())


def test_status_pane_paints_gate_progress_under_palette() -> None:
    async def body() -> None:
        app = _Harness()
        state = _state_with_audit(
            status="running",
            check_results=[
                {"name": "ruff_clean", "passed": True, "details": None},
                {"name": "pytest_pass", "passed": None, "details": None},
            ],
            verdict=None,
        )
        async with app.run_test(size=(60, 20)) as pilot:
            await pilot.pause()
            app.query_one("#sp", StatusPane).state = state
            await pilot.pause()
            await app.workers.wait_for_complete()
            rendered = app.export_screenshot()
            assert "gate:" in rendered
            assert "1/2" in rendered

    asyncio.run(body())
