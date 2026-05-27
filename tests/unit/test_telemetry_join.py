"""Tests for telemetry-to-wave session joins."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import SessionAttempt, Wave
from eawf.observability.telemetry.join import rollup_wave_sessions
from eawf.observability.telemetry.models import TelemetrySession


def _attempt(attempt: int, session_id: str) -> SessionAttempt:
    now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
    return SessionAttempt(
        attempt=attempt,
        runtime="codex",
        session_id=session_id,
        session_log_handle=f"urn:eawf:v1:session-log:codex:{session_id}",
        started_at=now,
        ended_at=now + timedelta(minutes=30),
        exit_status=0,
    )


def _wave() -> Wave:
    now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
    return Wave(
        id="P28-I03-W30",
        iter_id="P28-I03",
        title="derive actuals",
        status=WaveStatus.CLAIMED,
        opened_at=now,
        sessions={
            1: _attempt(1, "sess-1"),
            2: _attempt(2, "sess-2"),
        },
    )


def _telemetry(session_id: str, duration_ms: int | None) -> TelemetrySession:
    now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
    return TelemetrySession(
        session_id=session_id,
        project_id="repo/eawf",
        runtime="codex",
        wave_id="P28-I03-W30",
        attempt_id=session_id,
        session_log_path=f"opaque://{session_id}",
        started_at=now,
        ended_at=now + timedelta(minutes=30),
        duration_ms=duration_ms,
        model_primary=None,
        total_input_tokens=100,
        total_output_tokens=25,
        total_cache_read=10,
        total_cache_write=5,
        total_cost_usd=Decimal("0.42"),
        end_marker="clean_stop",
    )


def test_rollup_wave_sessions_joins_attempts_and_converts_duration_to_eu() -> None:
    rollup = rollup_wave_sessions(
        _wave(),
        [
            _telemetry("sess-1", 1_800_000),
            _telemetry("unrelated", 9_999),
            _telemetry("sess-2", 900_000),
        ],
    )

    assert [attempt.attempt for attempt in rollup.attempts] == [1, 2]
    assert rollup.duration_ms == 2_700_000
    assert rollup.attention_eu == pytest.approx(1.5)
    assert rollup.input_tokens == 200
    assert rollup.output_tokens == 50
    assert rollup.cost_usd == Decimal("0.84")


def test_rollup_wave_sessions_returns_no_attention_without_durations() -> None:
    rollup = rollup_wave_sessions(_wave(), [_telemetry("sess-1", None)])

    assert len(rollup.attempts) == 1
    assert rollup.duration_ms is None
    assert rollup.attention_eu is None


def test_rollup_wave_sessions_rejects_non_positive_eu_minutes() -> None:
    with pytest.raises(ValueError, match="eu_minutes must be positive"):
        rollup_wave_sessions(_wave(), [], eu_minutes=0.0)
