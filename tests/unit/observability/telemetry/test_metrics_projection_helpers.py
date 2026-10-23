"""Direct unit tests for the private scope/window helpers in
:mod:`eawf.observability.telemetry.metrics_projection`.

The public :func:`compute_metrics_projection` composes these helpers and
the integration test in :mod:`test_metrics_projection` exercises the
typical happy path. This module pins the branch coverage of the
individual predicates so a regression in the scope filtering (which the
TUI ``/metrics`` dashboard depends on for correct drill-down rows)
fails at the helper boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from eawf.kernel.state.models import EstimateSummary
from eawf.observability.telemetry.metrics_projection import (
    _estimate_for_wave,
    _in_window,
    _session_in_scope,
    _wave_in_scope,
)
from eawf.observability.telemetry.models import TelemetrySession

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


def _session(
    *,
    project_id: str = "QR",
    wave_id: str | None = "P01-I01-W01",
    started_at: datetime = _NOW,
) -> TelemetrySession:
    return TelemetrySession(
        session_id="sid",
        project_id=project_id,
        runtime="claude",
        wave_id=wave_id,
        attempt_id="a1",
        session_log_path="claude/sid.jsonl",
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=5),
        duration_ms=300000,
        model_primary="claude-model",
        total_input_tokens=0,
        total_output_tokens=0,
        total_cache_read=0,
        total_cache_write=0,
        total_cost_usd=Decimal("0"),
        turn_count=1,
        tool_call_count=0,
        error_count=0,
        denial_count=0,
        interrupt_count=0,
        compaction_count=0,
        subagent_dispatch_count=0,
        end_marker="clean_stop",
    )


# ---- _in_window -------------------------------------------------------------


def test_in_window_returns_true_for_window_all() -> None:
    """Pins line 236 — the ``delta is None`` short-circuit for ``all``."""
    assert _in_window(_NOW - timedelta(days=365), "all", _NOW) is True


def test_in_window_returns_false_when_ts_is_none() -> None:
    """Pins line 238 — ``ts is None`` rejects bounded windows."""
    assert _in_window(None, "7d", _NOW) is False


def test_in_window_returns_false_when_ts_outside_window() -> None:
    """Pins the ``return now - delta <= ts <= now`` False branch."""
    assert _in_window(_NOW - timedelta(days=14), "7d", _NOW) is False


def test_in_window_returns_true_when_ts_inside_window() -> None:
    """Pins the ``return now - delta <= ts <= now`` True branch."""
    assert _in_window(_NOW - timedelta(days=1), "7d", _NOW) is True


# ---- _session_in_scope ------------------------------------------------------


def test_session_in_scope_workspace_short_circuits_true() -> None:
    """Pins line 245 — ``workspace``/``user``/``all`` always match."""
    assert _session_in_scope(_session(), "workspace") is True
    assert _session_in_scope(_session(), "user") is True
    assert _session_in_scope(_session(), "all") is True


def test_session_in_scope_project_id_match() -> None:
    """Pins the ``session.project_id == scope`` True branch."""
    assert _session_in_scope(_session(project_id="QR"), "QR") is True


def test_session_in_scope_urn_scope_rejected() -> None:
    """Pins the ``scope.startswith('urn:')`` rejection."""
    assert _session_in_scope(_session(project_id=""), "urn:eawf:v1:state:QR") is False


def test_session_in_scope_returns_false_when_wave_id_is_none() -> None:
    """Pins line 251 — sessions without a wave id never match a wave scope."""
    assert _session_in_scope(_session(project_id="", wave_id=None), "P01-I01-W01") is False


def test_session_in_scope_falls_back_to_wave_in_scope() -> None:
    """Pins line 252 — falls through to ``_wave_in_scope`` when wave id present."""
    assert _session_in_scope(_session(project_id="", wave_id="P01-I01-W01"), "P01") is True


# ---- _wave_in_scope ---------------------------------------------------------


def test_wave_in_scope_workspace_short_circuits_true() -> None:
    """Pins line 258 — ``workspace`` is the catch-all wide scope."""
    assert _wave_in_scope("P01-I01-W01", "workspace") is True


def test_wave_in_scope_urn_scope_rejected() -> None:
    """Pins line 260 — URN scope is not a wave-prefix match."""
    assert _wave_in_scope("P01-I01-W01", "urn:eawf:v1:state:QR") is False


def test_wave_in_scope_exact_match() -> None:
    """Pins line 262 — wave id equals the scope."""
    assert _wave_in_scope("P01-I01-W01", "P01-I01-W01") is True


def test_wave_in_scope_iter_match() -> None:
    """Pins line 265 — first two segments match an iter scope."""
    assert _wave_in_scope("P01-I01-W01", "P01-I01") is True


def test_wave_in_scope_phase_match() -> None:
    """Pins line 266 — first segment matches a phase scope."""
    assert _wave_in_scope("P01-I01-W01", "P01") is True


def test_wave_in_scope_unrelated_phase_rejected() -> None:
    """Pins the catch-all ``False`` return path."""
    assert _wave_in_scope("P01-I01-W01", "P99") is False


# ---- _state_wave_in_scope ---------------------------------------------------


# ---- _state_wave_id_in_scope ------------------------------------------------


# ---- _state_for_scope filters actuals + estimates --------------------------


# ---- _estimate_for_wave / _actual_for_wave ---------------------------------


def test_estimate_for_wave_returns_none_when_no_match() -> None:
    """Pins line 354 — no key match AND no ``scope_id`` match returns None."""
    other = EstimateSummary(
        id="EST-other",
        scope_id="other-wave",
        expected_eu=1.0,
        pessimistic_eu=2.0,
        expected_minutes=60.0,
        pessimistic_minutes=120.0,
        display="1.0",
        confidence="medium",
        current_store_record_id="EST-other",
        updated_at=_NOW,
    )
    rows = {"EST-other": other}
    assert _estimate_for_wave(rows, "P99-I99-W99") is None


def test_estimate_for_wave_falls_back_to_scope_id_search() -> None:
    """Direct key miss + scope_id match returns the matching estimate."""
    est = EstimateSummary(
        id="EST-foo",
        scope_id="P01-I01-W01",
        expected_eu=1.0,
        pessimistic_eu=2.0,
        expected_minutes=60.0,
        pessimistic_minutes=120.0,
        display="1.0",
        confidence="medium",
        current_store_record_id="EST-foo",
        updated_at=_NOW,
    )
    rows = {"EST-foo": est}
    found = _estimate_for_wave(rows, "P01-I01-W01")
    assert found is est
