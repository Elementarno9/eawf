"""Tests for the telemetry session-duration aggregator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import pytest

from eawf.observability.telemetry.aggregator import percentile_ms, session_durations_ms
from eawf.observability.telemetry.models import TelemetrySession

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
_SCOPE = "urn:eawf:v1:state:QR"


def _session(
    session_id: str,
    *,
    runtime: Literal["claude", "codex", "opencode"],
    started_at: datetime,
    project_id: str,
) -> TelemetrySession:
    """Return one telemetry session row."""
    return TelemetrySession(
        session_id=session_id,
        project_id=project_id,
        runtime=runtime,
        wave_id="P01-I01-W01",
        attempt_id="a1",
        session_log_path=f"{runtime}/{session_id}.jsonl",
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=10),
        duration_ms=600000,
        model_primary=f"{runtime}-model",
        total_input_tokens=100,
        total_output_tokens=50,
        total_cache_read=80,
        total_cache_write=20,
        total_cost_usd=Decimal("0.01"),
        turn_count=3,
        tool_call_count=4,
        error_count=0,
        denial_count=0,
        interrupt_count=0,
        compaction_count=0,
        subagent_dispatch_count=1,
        end_marker="clean_stop",
    )


def test_session_durations_ms_derives_span_from_stamped_ended_at() -> None:
    """A row without ``duration_ms`` still counts once ``ended_at`` is stamped."""
    stamped = _session(
        "s5", runtime="claude", started_at=_NOW - timedelta(minutes=30), project_id=_SCOPE
    ).model_copy(update={"duration_ms": None})

    assert session_durations_ms([stamped]) == [600_000]


def test_session_durations_ms_drops_a_session_with_no_end() -> None:
    """Boundary: a still-running row is dropped, never counted as a zero duration."""
    running = _session(
        "s6", runtime="claude", started_at=_NOW - timedelta(minutes=30), project_id=_SCOPE
    ).model_copy(update={"duration_ms": None, "ended_at": None})

    assert session_durations_ms([running]) == []


def test_percentile_ms_on_empty_and_single_sample() -> None:
    """Boundary: empty yields None; a single sample is its own p50 and p95."""
    assert percentile_ms([], 0.5) is None
    assert percentile_ms([42], 0.5) == 42
    assert percentile_ms([42], 0.95) == 42


def test_percentile_ms_selects_nearest_rank_off_by_one() -> None:
    """Off-by-one: nearest-rank p50 of four samples is the 2nd, not the 3rd."""
    assert percentile_ms([1, 2, 3, 4], 0.5) == 2
    assert percentile_ms([1, 2, 3, 4], 0.0) == 1
    assert percentile_ms([1, 2, 3, 4], 1.0) == 4


@pytest.mark.parametrize("quantile", [-0.1, 1.1])
def test_percentile_ms_rejects_out_of_range_quantile(quantile: float) -> None:
    """Error path: a quantile outside [0.0, 1.0] fails fast at the boundary."""
    with pytest.raises(ValueError, match=r"within \[0.0, 1.0\]"):
        percentile_ms([1, 2, 3], quantile)
