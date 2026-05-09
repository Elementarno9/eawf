"""Tests for ``eawf.estimation.recovery`` — stale-segment promotion."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from eawf.estimation import recovery
from eawf.estimation.segments import open_segment
from eawf.lock.stale import STALE_HEARTBEAT_SECONDS
from eawf.state.enums import ActualStatus
from eawf.state.models import ActualSummary
from eawf.store.kinds.actual import ActualPayload


def _summary(scope: str, status: ActualStatus = ActualStatus.ACTIVE) -> ActualSummary:
    return ActualSummary(
        id=f"ACT-{scope}",
        scope_id=scope,
        status=status,
        elapsed_eu=0.0,
        attention_eu=None,
        agent_runtime_eu=None,
        current_store_record_id=f"ACT-{scope}",
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _write_dead_lock(path: Path) -> None:
    """Write a lockfile whose holder PID is guaranteed dead."""
    long_ago = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": 999_999_999,  # well beyond max_pid_t on every platform
                "hostname": "ghost",
                "started_at": long_ago,
                "heartbeat_at": long_ago,
            }
        )
    )


def _write_live_lock(path: Path) -> None:
    """Write a lockfile that is fresh (heartbeat == now, PID is current)."""
    import os

    now = datetime.now(UTC).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "hostname": "live",
                "started_at": now,
                "heartbeat_at": now,
            }
        )
    )


def test_find_stale_actuals_picks_up_dead_lock(tmp_path: Path) -> None:
    actuals = {"P01-I01-W01": _summary("P01-I01-W01")}
    _write_dead_lock(tmp_path / "actual-P01-I01-W01.lock")
    stale = recovery.find_stale_actuals(actuals, lock_dir=tmp_path)
    assert [s.scope_id for s in stale] == ["P01-I01-W01"]


def test_find_stale_actuals_skips_live_lock(tmp_path: Path) -> None:
    actuals = {"P01-I01-W01": _summary("P01-I01-W01")}
    _write_live_lock(tmp_path / "actual-P01-I01-W01.lock")
    stale = recovery.find_stale_actuals(actuals, lock_dir=tmp_path)
    assert stale == []


def test_find_stale_actuals_skips_inactive_status(tmp_path: Path) -> None:
    actuals = {"P01-I01-W01": _summary("P01-I01-W01", status=ActualStatus.DONE)}
    _write_dead_lock(tmp_path / "actual-P01-I01-W01.lock")
    stale = recovery.find_stale_actuals(actuals, lock_dir=tmp_path)
    assert stale == []


def test_find_stale_actuals_filters_by_scope(tmp_path: Path) -> None:
    actuals = {
        "P01-I01-W01": _summary("P01-I01-W01"),
        "P01-I01-W02": _summary("P01-I01-W02"),
    }
    _write_dead_lock(tmp_path / "actual-P01-I01-W01.lock")
    _write_dead_lock(tmp_path / "actual-P01-I01-W02.lock")
    stale = recovery.find_stale_actuals(actuals, lock_dir=tmp_path, scope="P01-I01-W02")
    assert [s.scope_id for s in stale] == ["P01-I01-W02"]


def test_cap_elapsed_caps_at_stale_heartbeat_seconds() -> None:
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    # Wall-clock has ticked far past the cap (e.g. 8 hours).
    far_later = started + timedelta(hours=8)
    capped_ended_at, elapsed_eu = recovery.cap_elapsed(
        started, now=far_later, eu_minutes=Decimal("30")
    )
    expected_capped = started + timedelta(seconds=STALE_HEARTBEAT_SECONDS)
    assert capped_ended_at == expected_capped
    # 60 seconds / 60 = 1 minute / 30 minutes per EU = 1/30 EU.
    expected_eu_value = Decimal(STALE_HEARTBEAT_SECONDS) / Decimal(60) / Decimal(30)
    assert elapsed_eu == expected_eu_value


def test_cap_elapsed_short_segment_passes_through_uncapped() -> None:
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    short_later = started + timedelta(seconds=15)
    capped_ended_at, elapsed_eu = recovery.cap_elapsed(
        started, now=short_later, eu_minutes=Decimal("30")
    )
    assert capped_ended_at == short_later
    expected = Decimal("15") / Decimal("60") / Decimal("30")
    assert elapsed_eu == expected


def test_cap_elapsed_negative_now_clamped_to_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If wall clock somehow went backwards, return zero elapsed and warn."""
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    earlier = started - timedelta(seconds=5)
    with caplog.at_level("WARNING", logger="eawf.estimation.recovery"):
        capped_ended_at, elapsed_eu = recovery.cap_elapsed(
            started, now=earlier, eu_minutes=Decimal("30")
        )
    assert capped_ended_at == started
    assert elapsed_eu == Decimal(0)
    skew_records = [r for r in caplog.records if "clock skew" in r.message]
    assert skew_records, f"expected a clock-skew WARNING, got {[r.message for r in caplog.records]}"
    assert skew_records[0].levelname == "WARNING"


def test_recover_segment_payload_promotes_open_to_abandoned() -> None:
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    payload = ActualPayload(
        segments=[open_segment(session_id="SES-001", started_at=started)],
        elapsed_eu=0.0,
        attention_eu=None,
        agent_runtime_eu=None,
        ratio_actual_over_estimate=None,
        inside_pessimistic=None,
        calibration_eligible=False,
        outcome="active",
        idle_policy="D30_non_agent_gap",
    )
    far_later = started + timedelta(hours=8)
    new_payload, recovered = recovery.recover_segment_payload(
        payload, now=far_later, eu_minutes=Decimal("30")
    )
    assert recovered is not None
    assert new_payload.segments[0].status == ActualStatus.ABANDONED
    assert new_payload.outcome == "abandoned"
    # Cap should have been applied.
    elapsed = (new_payload.segments[0].ended_at - started).total_seconds()
    assert elapsed == STALE_HEARTBEAT_SECONDS


def test_recover_segment_payload_no_open_segment_is_noop() -> None:
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    closed = open_segment(session_id="SES-001", started_at=started).model_copy(
        update={
            "ended_at": started + timedelta(minutes=5),
            "eu": 0.1666,
            "status": ActualStatus.DONE,
            "active_minutes": 5.0,
            "agent_runtime_minutes": 5.0,
        }
    )
    payload = ActualPayload(
        segments=[closed],
        elapsed_eu=0.1666,
        attention_eu=None,
        agent_runtime_eu=None,
        ratio_actual_over_estimate=None,
        inside_pessimistic=None,
        calibration_eligible=False,
        outcome="done",
        idle_policy="D30_non_agent_gap",
    )
    new_payload, recovered = recovery.recover_segment_payload(
        payload, now=datetime(2026, 1, 2, tzinfo=UTC), eu_minutes=Decimal("30")
    )
    assert recovered is None
    assert new_payload.segments[0].status == ActualStatus.DONE
