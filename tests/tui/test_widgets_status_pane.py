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
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import orjson
import pytest
from textual.app import ComposeResult

from eawf.kernel.state.enums import EffortBucket
from eawf.kernel.state.models import State
from eawf.surfaces.render.bars import BLOCK_FULL
from eawf.surfaces.tui.widgets.eu_bar import EMPTY_STATE
from eawf.surfaces.tui.widgets.heartbeat import (
    HEARTBEAT_GLYPH,
    HEARTBEAT_GLYPH_ASCII,
    HEARTBEAT_GLYPH_DIM,
)
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph
from eawf.surfaces.tui.widgets.status_pane import (
    COLUMN_GAP,
    DASH,
    DEFAULT_MAX_PARALLEL_WAVES,
    DEFAULT_PROJECT_CODE,
    DISPATCH_IDLE,
    EFFORT_AWAITING,
    GATE_FAIL,
    GATE_PASS,
    GATE_RUNNING,
    LEFT_COLUMN_WIDTH,
    TWO_COLUMN_THRESHOLD,
    VELOCITY_WINDOW_DAYS,
    DispatchSlice,
    StatusPane,
    _active_phase_id,
    _dispatch_lines,
    _effort_eu,
    build_dispatch_slice,
    build_status_columns,
    build_status_lines,
    build_velocity_eu_per_day,
    summary_counts,
)
from eawf.workflow.estimation.buckets import BUCKET_EU

from ._palette_harness import PaletteHarnessApp

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_EMPTY_REPO = _FIXTURES / "01-empty-repo.json"
_ESTIMATES_ACTUALS = _FIXTURES / "09-estimates-and-actuals.json"
_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"


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
        "track_id": None,
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


def _state_estimates_with_bucket(bucket: str = "XL") -> State:
    """Return fixture-09 with an ``effort_bucket`` spliced onto its wave.

    Fixture 09 carries a 1.2-EU actual on ``P01-I01-W01`` but no
    ``effort_bucket``. The EFFORT denominator is now the live bucket
    aggregate (not the legacy ``EstimateSummary``), so the wave needs a
    bucket for the block to render a measurable estimate. Default ``XL``
    (3.5 EU) keeps the consumed/estimate pair (1.2/3.5) well clear of the
    empty state.
    """
    payload = orjson.loads(_ESTIMATES_ACTUALS.read_bytes())
    payload["waves"]["P01-I01-W01"]["effort_bucket"] = bucket
    return State.model_validate(payload)


def _state_phase_two_iters_bucketed() -> State:
    """Return fixture-03 with a second iter so the phase spans two iters.

    The active P01 phase keeps its ``M``-bucketed in_progress wave under
    ``P01-I01`` and gains a planned ``P01-I02`` iter holding an ``S``-
    bucketed pending wave. A phase-scoped denominator sums both iters'
    waves (1.0 + 0.5 == 1.5); an iter-scoped one would see only 1.0.
    """
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    opened = payload["phases"]["P01"]["opened_at"]
    payload["waves"]["P01-I01-W01"]["effort_bucket"] = "M"
    payload["phases"]["P01"]["iter_ids"] = ["P01-I01", "P01-I02"]
    payload["iters"]["P01-I02"] = {
        "id": "P01-I02",
        "phase_id": "P01",
        "title": "second iter",
        "status": "planned",
        "wave_ids": ["P01-I02-W01"],
        "estimate_id": None,
        "audit_id": None,
        "opened_at": opened,
        "closed_at": None,
    }
    payload["waves"]["P01-I02-W01"] = {
        "id": "P01-I02-W01",
        "iter_id": "P01-I02",
        "title": "second iter wave",
        "status": "pending",
        "effort_bucket": "S",
        "deps": [],
        "blocks": [],
        "file_scopes": [],
        "success_criteria": [],
        "opened_at": opened,
        "closed_at": None,
    }
    return State.model_validate(payload)


def _state_phase_closed_plus_pending_bucketed() -> State:
    """Return fixture-03 with a closed ``M`` wave + a pending ``S`` wave.

    Both waves live under the active P01-I01 iter. The live denominator
    sums every active-phase wave regardless of status, so it counts the
    pending wave too (1.0 + 0.5 == 1.5) — proving PENDING waves now grow
    the EFFORT estimate, which the old claim-time-estimate denominator
    could not.
    """
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    opened = payload["phases"]["P01"]["opened_at"]
    payload["waves"]["P01-I01-W01"]["status"] = "closed"
    payload["waves"]["P01-I01-W01"]["closed_at"] = opened
    payload["waves"]["P01-I01-W01"]["effort_bucket"] = "M"
    payload["iters"]["P01-I01"]["wave_ids"] = ["P01-I01-W01", "P01-I01-W02"]
    payload["waves"]["P01-I01-W02"] = {
        "id": "P01-I01-W02",
        "iter_id": "P01-I01",
        "title": "pending",
        "status": "pending",
        "effort_bucket": "S",
        "deps": [],
        "blocks": [],
        "file_scopes": [],
        "success_criteria": [],
        "opened_at": opened,
        "closed_at": None,
    }
    return State.model_validate(payload)


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


def _wave_payload(
    payload: dict[str, Any],
    *,
    wave_id: str,
    status: str,
    deps: list[str] | None = None,
    agent_role: str | None = None,
    tokens_consumed: int = 0,
    token_budget: int | None = None,
) -> None:
    """Splice a :class:`Wave` row onto a fixture payload in place.

    Registers the wave under the active ``P01-I01`` iter and appends it to
    the iter's ``wave_ids`` so the DISPATCH band scopes it correctly.
    """
    opened = payload["phases"]["P01"]["opened_at"]
    payload["waves"][wave_id] = {
        "id": wave_id,
        "iter_id": "P01-I01",
        "title": f"wave {wave_id}",
        "status": status,
        "deps": list(deps or []),
        "blocks": [],
        "file_scopes": [],
        "success_criteria": [],
        "agent_role": agent_role,
        "tokens_consumed": tokens_consumed,
        "token_budget": token_budget,
        "opened_at": opened,
        "closed_at": opened if status in {"closed", "failed"} else None,
    }
    if wave_id not in payload["iters"]["P01-I01"]["wave_ids"]:
        payload["iters"]["P01-I01"]["wave_ids"].append(wave_id)


def _dispatch_scenario_state() -> State:
    """Build the §8.8 scenario-1 frontier: NOW W03/W04, NEXT W05/W07, WAIT W06.

    W01/W02 CLOSED (deps for the ready batch); W03/W04 IN_PROGRESS and on
    ``active_wave_ids`` (NOW); W05/W07 PENDING with all deps CLOSED (NEXT);
    W06 PENDING blocked on the still-running W03 (WAIT ← W03). W03 carries
    an agent role + a live token-burn budget for the NOW row.
    """
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    payload["waves"]["P01-I01-W01"]["status"] = "closed"
    payload["waves"]["P01-I01-W01"]["closed_at"] = payload["phases"]["P01"]["opened_at"]
    _wave_payload(payload, wave_id="P01-I01-W02", status="closed")
    _wave_payload(
        payload,
        wave_id="P01-I01-W03",
        status="in_progress",
        agent_role="executor",
        tokens_consumed=1400,
        token_budget=2000,
    )
    _wave_payload(payload, wave_id="P01-I01-W04", status="in_progress", agent_role="executor")
    _wave_payload(payload, wave_id="P01-I01-W05", status="pending", deps=["P01-I01-W01"])
    _wave_payload(payload, wave_id="P01-I01-W06", status="pending", deps=["P01-I01-W03"])
    _wave_payload(payload, wave_id="P01-I01-W07", status="pending", deps=["P01-I01-W02"])
    payload["current"]["active_wave_ids"] = ["P01-I01-W03", "P01-I01-W04"]
    return State.model_validate(payload)


def _section(lines: list[str], header: str) -> list[str]:
    """Return the lines under *header* up to the next blank line / header."""
    headers = {"LIFECYCLE", "EFFORT", "GATES", "DISPATCH"}
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


def _row(lines: list[str], label: str) -> str:
    """Return the rendered row whose ``label: value`` body matches *label*.

    A live row (waves / audits / worktrees / gate) carries a leading sigil
    glyph + space before its label, so a bare ``startswith(label)`` would
    miss it. This matches the *label* anywhere a single-glyph + space prefix
    could sit, so the helper finds both the un-prefixed static rows and the
    sigil-prefixed live rows.
    """
    return next(line for line in lines if line.startswith(label) or line[2:].startswith(label))


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
        "track_id": None,
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
        "track_id": None,
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


def test_build_status_lines_dispatch_band_after_gates() -> None:
    """The DISPATCH band appends after GATES with NOW / NEXT / WAIT rows."""
    lines = build_status_lines(_load(_PHASE_ITER_WAVE))
    assert "DISPATCH" in lines
    assert lines.index("GATES") < lines.index("DISPATCH")
    dispatch = _section(lines, "DISPATCH")
    assert dispatch[0] == "NOW"
    assert any(line.startswith("NEXT") for line in dispatch)
    assert any(line.startswith("WAIT") for line in dispatch)


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
    waves_line = _row(lines, "waves:")
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
# build_status_columns — responsive two-column (wide) / single (narrow)
# --------------------------------------------------------------------------


def test_build_status_columns_narrow_equals_single_column() -> None:
    """Below the threshold the layout is byte-identical to the flat list."""
    state = _load(_PHASE_ITER_WAVE)
    rows = build_status_columns(state, mode="unicode", width=TWO_COLUMN_THRESHOLD - 1)
    assert rows == build_status_lines(state, mode="unicode")


def test_build_status_columns_zero_width_falls_back_to_single() -> None:
    """A pre-layout width of 0 falls back to the single column (no crash)."""
    state = _load(_PHASE_ITER_WAVE)
    rows = build_status_columns(state, mode="unicode", width=0)
    assert rows == build_status_lines(state, mode="unicode")


def test_build_status_columns_wide_pairs_headers_side_by_side() -> None:
    """At/above the threshold LIFECYCLE + EFFORT head the first row's two cells.

    LIFECYCLE stacks on the left and EFFORT on the right, so the first row
    carries both headers: LIFECYCLE at the start and EFFORT after the
    left-column pad + gap.
    """
    state = _load(_PHASE_ITER_WAVE)
    rows = build_status_columns(state, mode="unicode", width=120)
    assert rows[0].startswith("LIFECYCLE")
    assert "EFFORT" in rows[0]
    # The right cell begins exactly at the left-column + gap offset.
    assert rows[0][LEFT_COLUMN_WIDTH + COLUMN_GAP :].startswith("EFFORT")


def test_build_status_columns_wide_left_column_carries_lifecycle_and_gates() -> None:
    """The left column stacks LIFECYCLE then GATES; the right EFFORT + DISPATCH."""
    state = _load(_PHASE_ITER_WAVE)
    rows = build_status_columns(state, mode="unicode", width=120)
    left_cells = [row[:LEFT_COLUMN_WIDTH].rstrip() for row in rows]
    right_cells = [row[LEFT_COLUMN_WIDTH + COLUMN_GAP :] for row in rows]
    assert "LIFECYCLE" in left_cells
    assert "GATES" in left_cells
    assert "EFFORT" in right_cells
    assert "DISPATCH" in right_cells
    # Sections never cross columns: no left-column header leaks to the right.
    assert "LIFECYCLE" not in right_cells
    assert "EFFORT" not in left_cells


def test_build_status_columns_wide_right_content_after_left_pad() -> None:
    """Each two-column row's right cell starts past the left-pad + gap column."""
    state = _load(_PHASE_ITER_WAVE)
    rows = build_status_columns(state, mode="unicode", width=120)
    # A row carrying right-column content must be wider than the left column.
    populated = [r for r in rows if len(r) > LEFT_COLUMN_WIDTH + COLUMN_GAP]
    assert populated  # the right column does carry content
    for row in populated:
        # The left cell never bleeds past its column boundary.
        assert len(row[:LEFT_COLUMN_WIDTH].rstrip()) <= LEFT_COLUMN_WIDTH


def test_build_status_columns_threshold_boundary() -> None:
    """``threshold - 1`` → single column; ``threshold`` → two columns."""
    state = _load(_PHASE_ITER_WAVE)
    narrow = build_status_columns(state, mode="unicode", width=TWO_COLUMN_THRESHOLD - 1)
    wide = build_status_columns(state, mode="unicode", width=TWO_COLUMN_THRESHOLD)
    assert narrow == build_status_lines(state, mode="unicode")
    assert wide != narrow
    # The wide layout pairs two headers onto its first row.
    assert wide[0].startswith("LIFECYCLE") and "EFFORT" in wide[0]


def test_build_status_columns_wide_none_state() -> None:
    """A ``None`` state still lays out two columns at a wide width (no crash)."""
    rows = build_status_columns(None, mode="unicode", width=120)
    assert rows[0].startswith("LIFECYCLE")
    assert "EFFORT" in rows[0]


# --------------------------------------------------------------------------
# EFFORT block — consumed/estimate EU + variance + sparkline + ETA
# --------------------------------------------------------------------------


def test_build_status_lines_effort_shows_consumed_estimate_eu() -> None:
    """EFFORT surfaces the active phase's consumed / bucket-estimate EU + a bar."""
    lines = build_status_lines(_state_estimates_with_bucket())
    effort = next(line for line in lines if line.startswith("effort:"))
    # Fixture 09: actual 1.2 EU vs the live bucket aggregate XL (3.5 EU).
    assert "1.2/3.5" in effort
    assert "#" in effort  # filled cells (~34% consumed)


def test_build_status_lines_effort_shows_signed_variance_pct() -> None:
    """EFFORT surfaces the signed precision % delta (under-run is negative).

    W09 relabelled the metric variance -> precision; the row now reads
    ``precision:`` with the same signed % delta.
    """
    lines = build_status_lines(_state_estimates_with_bucket())
    precision = next(line for line in lines if line.startswith("precision:"))
    # (1.2 - 3.5) / 3.5 * 100 = -65.7 % (a hard under-run so far).
    assert "-65.7%" in precision


def test_build_status_lines_effort_shows_velocity_sparkline() -> None:
    """EFFORT surfaces an EU/day velocity sparkline glyph run (Braille mode)."""
    lines = build_status_lines(_load(_ESTIMATES_ACTUALS), mode="unicode")
    velocity = next(line for line in lines if line.startswith("velocity:"))
    # One actual carrying 1.2 EU → a single populated day → not empty state.
    assert EMPTY_STATE not in velocity
    assert any(g in velocity for g in ("▁", "█", "▇"))


def test_build_status_lines_effort_shows_eta_date() -> None:
    """EFFORT surfaces an ISO ETA date projected from the current burn."""
    lines = build_status_lines(_state_estimates_with_bucket())
    eta = next(line for line in lines if line.startswith("eta:"))
    # remaining 2.3 EU at ~1.2 EU/day projects a real finish date.
    body = eta.split("eta:")[1].strip()
    assert body != DASH
    datetime.strptime(body, "%Y-%m-%d")  # parses as an ISO date


def test_build_status_lines_effort_all_absent_collapses_to_awaiting() -> None:
    """No estimate / actuals → the single dim awaiting-first-wave collapse line.

    With none of effort / precision / velocity carrying data the EFFORT block
    collapses to the one :data:`EFFORT_AWAITING` line rather than three
    stacked ``— no data`` rows -- and never a fabricated 0 % bar. The
    per-metric ``effort:`` / ``precision:`` / ``velocity:`` rows are absent.
    """
    lines = build_status_lines(_load(_PHASE_ITER_WAVE))
    effort = _section(lines, "EFFORT")
    assert effort == [EFFORT_AWAITING]
    assert "0%" not in EFFORT_AWAITING  # not a fake 0 % bar
    assert not any(line.startswith("effort:") for line in lines)
    assert not any(line.startswith("precision:") for line in lines)
    assert not any(line.startswith("velocity:") for line in lines)


def test_build_status_lines_effort_estimate_only_no_actuals_collapses() -> None:
    """Estimate present but no measured actual → the awaiting-first-wave collapse.

    The regression A42 surfaced: when the WaveSessionRollup is empty or
    telemetry is unhealthy the bar used to render ``-/<estimate>  ----- 0%``,
    which reads as "no work done" rather than "no rollup yet". An estimate
    alone (a bucketed-but-unrun wave) is not measured DATA for the three
    present-vs-absent metrics, so the EFFORT block still collapses to the one
    dim :data:`EFFORT_AWAITING` line -- never a fabricated 0 % bar against the
    live estimate.
    """
    # Splice an effort_bucket onto fixture-03's wave so the live denominator
    # is positive (XL == 3.5 EU) but no actual exists — the gap A42 flagged.
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    payload["waves"]["P01-I01-W01"]["effort_bucket"] = "XL"
    state = State.model_validate(payload)
    lines = build_status_lines(state)
    effort = _section(lines, "EFFORT")
    assert effort == [EFFORT_AWAITING]
    assert "0%" not in EFFORT_AWAITING  # not a fake 0 % bar against the live estimate
    assert "/" not in EFFORT_AWAITING  # no ``-/3.5`` prefix either
    assert not any(line.startswith(("effort:", "precision:", "velocity:")) for line in lines)


def test_build_status_lines_effort_present_metric_expands_with_selective_dash() -> None:
    """One present metric expands the block; an absent metric shows its OWN dash.

    Fixture-03's wave carries an actual (so velocity has a populated day --
    present) but no ``effort_bucket`` (so the estimate is 0 -- variance has no
    baseline). The presence of ANY metric expands the block back to its
    per-metric rows, where the selectively-absent variance still renders its
    own ``— no data`` dash rather than dragging the whole block back to the
    collapsed line.
    """
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    _add_actual(
        payload,
        actual_id="ACT-A",
        scope_id="P01-I01-W01",
        elapsed_eu=1.2,
        updated_at="2026-05-08T09:00:00Z",
    )
    lines = build_status_lines(State.model_validate(payload), mode="unicode")
    effort = _section(lines, "EFFORT")
    # The block is expanded (not the collapsed awaiting line).
    assert effort != [EFFORT_AWAITING]
    velocity = _row(lines, "velocity:")
    precision = _row(lines, "precision:")
    # Velocity is present (a populated burn day), precision shows its own dash.
    assert EMPTY_STATE not in velocity
    assert any(g in velocity for g in ("▁", "█", "▇"))
    assert EMPTY_STATE in precision  # selectively-absent metric keeps its own dash


def test_build_status_lines_effort_ascii_mode() -> None:
    """ASCII mode renders the EU bar with ``#``/``-`` and ASCII spark glyphs."""
    lines = build_status_lines(_state_estimates_with_bucket(), mode="ascii")
    effort = next(line for line in lines if line.startswith("effort:"))
    velocity = next(line for line in lines if line.startswith("velocity:"))
    assert "#" in effort
    assert "▁" not in velocity and "█" not in velocity  # no Braille block glyphs
    assert any(g in velocity for g in (".", "@", "#"))


# --------------------------------------------------------------------------
# _effort_eu — live bucket-sum denominator, phase scope
# --------------------------------------------------------------------------


def test_effort_eu_denominator_counts_pending_wave() -> None:
    """The bucket-sum denominator includes a PENDING wave (not just claimed).

    A phase with a closed ``M`` wave (1.0 EU) + a pending ``S`` wave (0.5
    EU) sums to 1.5 EU — proving PENDING waves now grow the estimate, which
    the legacy claim-time-estimate denominator could not.
    """
    _consumed, estimate = _effort_eu(_state_phase_closed_plus_pending_bucketed())
    assert estimate == pytest.approx(1.5)


def test_effort_eu_denominator_spans_multiple_iters() -> None:
    """The denominator sums bucketed waves across every iter of the phase.

    Phase scope (not iter scope): an ``M`` wave under P01-I01 (1.0 EU) plus
    an ``S`` wave under a second iter P01-I02 (0.5 EU) sum to 1.5 EU. An
    iter-scoped denominator would see only the active iter's 1.0 EU.
    """
    _consumed, estimate = _effort_eu(_state_phase_two_iters_bucketed())
    assert estimate == pytest.approx(1.5)


def test_effort_eu_no_bucket_waves_zero_denominator() -> None:
    """Waves with ``effort_bucket=None`` contribute 0 → empty-state EFFORT.

    Fixture 03's lone wave carries no bucket, so the live aggregate is 0.0
    and the EFFORT block renders its empty-state sentinel rather than a
    fabricated bar.
    """
    state = _load(_PHASE_ITER_WAVE)
    _consumed, estimate = _effort_eu(state)
    assert estimate == pytest.approx(0.0)
    lines = build_status_lines(state)
    # No bucket + no actuals → the whole EFFORT block collapses to the one
    # dim awaiting-first-wave line (no per-metric ``effort:`` row).
    assert _section(lines, "EFFORT") == [EFFORT_AWAITING]


def test_effort_eu_no_active_phase_returns_zero_pair() -> None:
    """A state with no active phase yields ``(0.0, 0.0)`` (the empty pair)."""
    state = _load(_EMPTY_REPO)
    assert _active_phase_id(state) is None
    assert _effort_eu(state) == pytest.approx((0.0, 0.0))


def test_effort_eu_consumed_from_actuals() -> None:
    """The numerator sums the active phase's wave-scoped actual elapsed EU."""
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    payload["waves"]["P01-I01-W01"]["effort_bucket"] = "M"
    _add_actual(
        payload,
        actual_id="ACT-A",
        scope_id="P01-I01-W01",
        elapsed_eu=0.7,
        updated_at="2026-05-08T09:00:00Z",
    )
    consumed, estimate = _effort_eu(State.model_validate(payload))
    assert consumed == pytest.approx(0.7)
    # Denominator is the live bucket aggregate (M == 1.0 EU).
    assert estimate == pytest.approx(BUCKET_EU[EffortBucket.M])


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
    gate = _row(lines, "gate:")
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
    lines = build_status_lines(state, mode="unicode")
    gate = _row(lines, "gate:")
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
    gate = _row(lines, "gate:")
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
    gate = _row(lines, "gate:")
    assert "2/2" in gate  # both checks reported (pass + fail)
    assert GATE_PASS not in gate and GATE_FAIL not in gate  # no Unicode glyphs
    assert "P" in gate and "x" in gate


# --------------------------------------------------------------------------
# Live-row sigils — waves / audits / worktrees / gate gain a leading sigil
# --------------------------------------------------------------------------


def _gate_audit_state() -> State:
    """Fixture-03 with a running iter-attached audit so a ``gate:`` row exists."""
    return _state_with_audit(
        status="running",
        check_results=[{"name": "ruff_clean", "passed": True, "details": None}],
        verdict=None,
    )


@pytest.mark.parametrize("label", ["waves:", "audits:", "worktrees:", "gate:"])
def test_live_rows_carry_leading_sigil_unicode(label: str) -> None:
    """Each live row opens with the unicode running sigil + a space."""
    lines = build_status_lines(_gate_audit_state(), mode="unicode")
    row = _row(lines, label)
    prefix = f"{glyph(Sigil.RUNNING, mode='unicode')} "
    assert row.startswith(prefix), f"{label!r} row missing leading sigil: {row!r}"
    assert row[len(prefix) :].startswith(label)


@pytest.mark.parametrize("label", ["waves:", "audits:", "worktrees:", "gate:"])
def test_live_rows_carry_leading_sigil_ascii(label: str) -> None:
    """ASCII mode swaps the sigil for its ASCII column glyph + a space."""
    lines = build_status_lines(_gate_audit_state(), mode="ascii")
    row = _row(lines, label)
    prefix = f"{glyph(Sigil.RUNNING, mode='ascii')} "
    assert row.startswith(prefix), f"{label!r} row missing leading sigil: {row!r}"
    assert row[len(prefix) :].startswith(label)


def test_static_pointer_rows_carry_no_sigil() -> None:
    """The static project / phase / iter / progress rows carry no leading sigil.

    The sigil is the live-row marker; the static pointer rows above it (and
    the progress bar) keep their bare ``label:`` start so the sigil reads as
    a contrast, not chrome on every row.
    """
    lines = build_status_lines(_load(_PHASE_ITER_WAVE), mode="unicode")
    for label in ("project:", "phase:", "iter:", "progress:"):
        assert any(line.startswith(label) for line in lines), label


# --------------------------------------------------------------------------
# DISPATCH band — build_dispatch_slice NOW/NEXT/WAIT frontier
# --------------------------------------------------------------------------


def test_dispatch_slice_now_next_wait() -> None:
    """NOW/NEXT/WAIT derive from the §8.8 scenario-1 frontier fixture."""
    slice_ = build_dispatch_slice(_dispatch_scenario_state())
    assert isinstance(slice_, DispatchSlice)
    assert slice_.now == ("P01-I01-W03", "P01-I01-W04")
    assert slice_.next == ("P01-I01-W05", "P01-I01-W07")
    assert slice_.wait == (("P01-I01-W06", "P01-I01-W03"),)
    assert slice_.next_overflow == 0


def test_dispatch_slice_none_state_empty() -> None:
    """A ``None`` state yields an empty frontier (no NOW/NEXT/WAIT)."""
    slice_ = build_dispatch_slice(None)
    assert slice_.now == ()
    assert slice_.next == ()
    assert slice_.wait == ()
    assert slice_.next_overflow == 0


def test_dispatch_slice_emits_no_info_on_repaint(caplog: pytest.LogCaptureFixture) -> None:
    """The slice builder logs nothing at INFO — it runs ~2 Hz on every pulse.

    An INFO line here bled onto the TUI screen on every pulse tick (the
    blinking top-left artifact); the slice builder must stay silent.
    """
    with caplog.at_level(logging.INFO, logger="eawf.surfaces.tui.widgets.status_pane"):
        build_dispatch_slice(_dispatch_scenario_state())
    info = [
        r
        for r in caplog.records
        if r.name == "eawf.surfaces.tui.widgets.status_pane" and r.levelno >= logging.INFO
    ]
    assert info == []


def test_dispatch_slice_respects_blocked_by() -> None:
    """A PENDING wave with an un-CLOSED dep is excluded from NEXT.

    W06 depends on the still-IN_PROGRESS W03, so its live ``blocked_by``
    view is non-empty — it lands in WAIT, never NEXT.
    """
    slice_ = build_dispatch_slice(_dispatch_scenario_state())
    assert "P01-I01-W06" not in slice_.next
    waiting_ids = {wid for wid, _ in slice_.wait}
    assert "P01-I01-W06" in waiting_ids


def test_dispatch_slice_frontier_advances_on_close() -> None:
    """Closing W03 moves it out of NOW and W06 from WAIT into NEXT."""
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    payload["waves"]["P01-I01-W01"]["status"] = "closed"
    payload["waves"]["P01-I01-W01"]["closed_at"] = payload["phases"]["P01"]["opened_at"]
    _wave_payload(payload, wave_id="P01-I01-W03", status="closed")
    _wave_payload(payload, wave_id="P01-I01-W06", status="pending", deps=["P01-I01-W03"])
    payload["current"]["active_wave_ids"] = []
    slice_ = build_dispatch_slice(State.model_validate(payload))
    assert "P01-I01-W03" not in slice_.now
    assert "P01-I01-W06" in slice_.next  # dep now CLOSED → ready
    assert slice_.wait == ()


def test_dispatch_next_capped_to_max_parallel() -> None:
    """More ready waves than the cap → first cap by W## + ``+N more``.

    Nine PENDING waves with deps met; the cap (``DEFAULT_MAX_PARALLEL_WAVES``)
    bounds NEXT to the lowest-``W##`` batch and ``next_overflow`` carries
    the remainder for the ``+N more`` suffix.
    """
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    payload["waves"]["P01-I01-W01"]["status"] = "closed"
    payload["waves"]["P01-I01-W01"]["closed_at"] = payload["phases"]["P01"]["opened_at"]
    payload["current"]["active_wave_ids"] = []
    for n in range(2, 11):  # W02..W10 → nine ready PENDING waves
        _wave_payload(payload, wave_id=f"P01-I01-W{n:02d}", status="pending")
    slice_ = build_dispatch_slice(State.model_validate(payload))
    assert len(slice_.next) == DEFAULT_MAX_PARALLEL_WAVES
    # Lowest W## first: W02..W(2 + cap - 1).
    expected = tuple(f"P01-I01-W{n:02d}" for n in range(2, 2 + DEFAULT_MAX_PARALLEL_WAVES))
    assert slice_.next == expected
    assert slice_.next_overflow == 9 - DEFAULT_MAX_PARALLEL_WAVES


def test_dispatch_slice_respects_explicit_max_parallel() -> None:
    """An explicit ``max_parallel`` overrides the built-in cap for NEXT."""
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    payload["waves"]["P01-I01-W01"]["status"] = "closed"
    payload["waves"]["P01-I01-W01"]["closed_at"] = payload["phases"]["P01"]["opened_at"]
    payload["current"]["active_wave_ids"] = []
    for n in range(2, 6):  # four ready PENDING waves
        _wave_payload(payload, wave_id=f"P01-I01-W{n:02d}", status="pending")
    slice_ = build_dispatch_slice(State.model_validate(payload), max_parallel=2)
    assert slice_.next == ("P01-I01-W02", "P01-I01-W03")
    assert slice_.next_overflow == 2


def test_dispatch_slice_dangling_dep_degrades_wait_no_crash() -> None:
    """A WAIT wave whose blocker is dangling renders with no edge label.

    W06 depends on the IN_PROGRESS W03 (a real active blocker → WAIT) and
    on a dangling ``P01-I01-W99``. The live ``blocked_by`` view skips the
    dangling id; the active blocker resolves to W03 so the edge survives —
    and the slice builds without raising.
    """
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    _wave_payload(payload, wave_id="P01-I01-W03", status="in_progress")
    _wave_payload(
        payload,
        wave_id="P01-I01-W06",
        status="pending",
        deps=["P01-I01-W03", "P01-I01-W99"],
    )
    payload["current"]["active_wave_ids"] = ["P01-I01-W03"]
    slice_ = build_dispatch_slice(State.model_validate(payload))
    assert slice_.wait == (("P01-I01-W06", "P01-I01-W03"),)


def test_dispatch_slice_dangling_only_blocker_drops_wait() -> None:
    """A PENDING wave blocked solely by a dangling dep is neither NEXT nor WAIT.

    Its live ``blocked_by`` view drops the dangling id, so it is treated as
    ready (NEXT) rather than crashing — degrading gracefully per §8.8.
    """
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    payload["current"]["active_wave_ids"] = []
    _wave_payload(payload, wave_id="P01-I01-W06", status="pending", deps=["P01-I01-W99"])
    slice_ = build_dispatch_slice(State.model_validate(payload))
    assert "P01-I01-W06" in slice_.next
    assert slice_.wait == ()


# --------------------------------------------------------------------------
# DISPATCH band — _dispatch_lines render (rows, idle, dot, ascii)
# --------------------------------------------------------------------------


def test_dispatch_lines_now_rows_show_role_and_dot() -> None:
    """NOW rows carry the pulsing dot, the agent role, and a burn bar."""
    lines = _dispatch_lines(_dispatch_scenario_state(), mode="unicode")
    assert lines[0] == "NOW"
    w03_row = next(line for line in lines if "W03" in line)
    assert HEARTBEAT_GLYPH in w03_row  # lit dot (default lit=True)
    assert "executor" in w03_row
    assert "#" in w03_row or BLOCK_FULL in w03_row  # token-burn fill (1400/2000)


def test_dispatch_lines_no_budget_shows_empty_state() -> None:
    """A NOW wave with no token_budget shows the burn empty-state sentinel."""
    lines = _dispatch_lines(_dispatch_scenario_state(), mode="unicode")
    w04_row = next(line for line in lines if "W04" in line)
    assert EMPTY_STATE in w04_row  # W04 carries no token_budget


def test_dispatch_lines_idle_when_no_active_waves() -> None:
    """Empty NOW renders the ``idle (no active waves)`` sentinel."""
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    payload["current"]["active_wave_ids"] = []
    payload["waves"]["P01-I01-W01"]["status"] = "pending"
    empty_lines = _dispatch_lines(State.model_validate(payload), mode="unicode")
    assert empty_lines[0] == "NOW"
    assert f"  {DISPATCH_IDLE}" in empty_lines


def test_dispatch_lines_none_state_idle() -> None:
    """A ``None`` state renders the DISPATCH band as idle, not a crash."""
    lines = _dispatch_lines(None, mode="unicode")
    assert lines[0] == "NOW"
    assert f"  {DISPATCH_IDLE}" in lines


def test_dispatch_lines_next_wait_collapsed_inline() -> None:
    """NEXT lists the ready batch; WAIT lists blocked waves with ``←`` edges."""
    lines = _dispatch_lines(_dispatch_scenario_state(), mode="unicode")
    next_line = next(line for line in lines if line.startswith("NEXT"))
    wait_line = next(line for line in lines if line.startswith("WAIT"))
    assert "W05" in next_line and "W07" in next_line
    assert "W06←W03" in wait_line


def test_dispatch_lines_next_overflow_suffix() -> None:
    """NEXT past the cap carries the ``+N more`` suffix inline."""
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    payload["waves"]["P01-I01-W01"]["status"] = "closed"
    payload["waves"]["P01-I01-W01"]["closed_at"] = payload["phases"]["P01"]["opened_at"]
    payload["current"]["active_wave_ids"] = []
    for n in range(2, 11):
        _wave_payload(payload, wave_id=f"P01-I01-W{n:02d}", status="pending")
    lines = _dispatch_lines(State.model_validate(payload), mode="unicode")
    next_line = next(line for line in lines if line.startswith("NEXT"))
    assert f"+{9 - DEFAULT_MAX_PARALLEL_WAVES} more" in next_line


def test_dispatch_dot_pulse_frame() -> None:
    """The dot phase advances on TICK_PULSE without recomputing the frontier.

    Toggling the ``lit`` flag flips the rendered dot (bright ⇄ dim) while
    the NOW/NEXT/WAIT membership is identical across frames — the pulse is
    cosmetic-only.
    """
    state = _dispatch_scenario_state()
    lit_lines = _dispatch_lines(state, mode="unicode", lit=True)
    dim_lines = _dispatch_lines(state, mode="unicode", lit=False)
    lit_row = next(line for line in lit_lines if "W03" in line)
    dim_row = next(line for line in dim_lines if "W03" in line)
    assert HEARTBEAT_GLYPH in lit_row
    assert HEARTBEAT_GLYPH_DIM in dim_row
    # Frontier membership (the non-dot content) is unchanged across frames.
    assert [line for line in lit_lines if line.startswith(("NEXT", "WAIT"))] == [
        line for line in dim_lines if line.startswith(("NEXT", "WAIT"))
    ]


def test_dispatch_lines_ascii_static_dot() -> None:
    """ASCII mode renders a single static dot (no bright/dim animation)."""
    state = _dispatch_scenario_state()
    lit_lines = _dispatch_lines(state, mode="ascii", lit=True)
    dim_lines = _dispatch_lines(state, mode="ascii", lit=False)
    lit_row = next(line for line in lit_lines if "W03" in line)
    dim_row = next(line for line in dim_lines if "W03" in line)
    assert HEARTBEAT_GLYPH_ASCII in lit_row
    assert HEARTBEAT_GLYPH not in lit_row and HEARTBEAT_GLYPH_DIM not in lit_row
    # The static dot does not change between pulse phases under ASCII.
    assert lit_row == dim_row


def test_dispatch_lines_paused_static_dot() -> None:
    """A paused pulse (SUSPEND) renders a static dot even in unicode mode."""
    state = _dispatch_scenario_state()
    paused = _dispatch_lines(state, mode="unicode", lit=False, paused=True)
    row = next(line for line in paused if "W03" in line)
    assert HEARTBEAT_GLYPH_ASCII in row
    assert HEARTBEAT_GLYPH_DIM not in row


def test_dispatch_slice_is_frozen() -> None:
    """``DispatchSlice`` is frozen — a render value object, not mutable state."""
    slice_ = build_dispatch_slice(None)
    with pytest.raises((TypeError, ValueError)):
        slice_.now = ("X",)  # type: ignore[misc]


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
            app.query_one("#sp", StatusPane).state = _state_estimates_with_bucket()
            await pilot.pause()
            await app.workers.wait_for_complete()
            rendered = app.export_screenshot()
            assert "effort:" in rendered
            assert "1.2/3.5" in rendered
            assert "precision:" in rendered

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


def test_status_pane_paints_dispatch_band_under_palette() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(60, 40)) as pilot:
            await pilot.pause()
            app.query_one("#sp", StatusPane).state = _dispatch_scenario_state()
            await pilot.pause()
            await app.workers.wait_for_complete()
            rendered = app.export_screenshot()
            assert "DISPATCH" in rendered
            assert "NOW" in rendered
            assert "NEXT" in rendered
            assert "WAIT" in rendered
            assert "executor" in rendered

    asyncio.run(body())


def test_status_pane_pulse_pause_resume() -> None:
    """``pause_pulse`` freezes the dot phase; ``resume_pulse`` re-arms it."""

    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(60, 40)) as pilot:
            await pilot.pause()
            pane = app.query_one("#sp", StatusPane)
            pane.state = _dispatch_scenario_state()
            await pilot.pause()
            await app.workers.wait_for_complete()
            pane.pause_pulse()
            await pilot.pause()
            assert pane._pulse_paused is True
            frozen_phase = pane._pulse_lit
            pane._tick_pulse()  # a tick while paused must not toggle the phase
            assert pane._pulse_lit == frozen_phase
            pane.resume_pulse()
            await pilot.pause()
            assert pane._pulse_paused is False

    asyncio.run(body())
