"""Tests for ``eawf.estimation.segments``.

Covers:

- Happy path open/close round-trip with elapsed_eu == raw_minutes / eu_minutes.
- Boundary case: zero-length segment (started_at == ended_at) yields elapsed_eu = 0.
- Error path: closing a non-active segment raises.
- Error path: ended_at earlier than started_at raises.
- Helper: is_open_for / latest_open_segment.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from eawf.estimation import segments
from eawf.state.enums import ActualStatus


def test_open_segment_starts_active_with_zero_duration() -> None:
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    seg = segments.open_segment(session_id="SES-001", started_at=started)
    assert seg.status == ActualStatus.ACTIVE
    assert seg.started_at == started
    assert seg.ended_at == started  # sentinel: ended_at == started_at while open
    assert seg.eu == 0.0
    assert seg.active_minutes == 0.0


def test_close_segment_computes_elapsed_eu() -> None:
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    ended = started + timedelta(minutes=60)  # 60 active minutes
    seg = segments.open_segment(session_id="SES-001", started_at=started)
    closed = segments.close_segment(seg, ended_at=ended, eu_minutes=Decimal("30"))
    assert closed.status == ActualStatus.DONE
    assert closed.ended_at == ended
    # 60 minutes / 30 minutes-per-EU = 2 EU.
    assert closed.eu == 2.0
    assert closed.active_minutes == 60.0


def test_close_segment_zero_duration_yields_zero_eu() -> None:
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    seg = segments.open_segment(session_id="SES-001", started_at=started)
    closed = segments.close_segment(seg, ended_at=started, eu_minutes=Decimal("30"))
    assert closed.eu == 0.0
    assert closed.active_minutes == 0.0
    assert closed.status == ActualStatus.DONE


def test_close_segment_rejects_non_active() -> None:
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    seg = segments.open_segment(session_id="SES-001", started_at=started)
    closed_once = segments.close_segment(seg, ended_at=started, eu_minutes=Decimal("30"))
    with pytest.raises(ValueError, match="status=active"):
        segments.close_segment(closed_once, ended_at=started, eu_minutes=Decimal("30"))


def test_close_segment_rejects_ended_before_started() -> None:
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    earlier = started - timedelta(minutes=5)
    seg = segments.open_segment(session_id="SES-001", started_at=started)
    with pytest.raises(ValueError, match="earlier than"):
        segments.close_segment(seg, ended_at=earlier, eu_minutes=Decimal("30"))


def test_close_segment_status_coercion_to_abandoned() -> None:
    """The status= parameter accepts ActualStatus.ABANDONED for recovery."""
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    ended = started + timedelta(minutes=15)
    seg = segments.open_segment(session_id="SES-001", started_at=started)
    closed = segments.close_segment(
        seg,
        ended_at=ended,
        eu_minutes=Decimal("30"),
        status=ActualStatus.ABANDONED,
    )
    assert closed.status == ActualStatus.ABANDONED


def test_is_open_for_detects_active_segment() -> None:
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    open_seg = segments.open_segment(session_id="SES-001", started_at=started)
    closed_other = segments.close_segment(
        segments.open_segment(session_id="SES-002", started_at=started),
        ended_at=started + timedelta(minutes=5),
        eu_minutes=Decimal("30"),
    )
    segs = [closed_other, open_seg]
    assert segments.is_open_for(segs, session_id="SES-001") is True
    assert segments.is_open_for(segs, session_id="SES-002") is False
    assert segments.is_open_for(segs, session_id="SES-other") is False


def test_is_open_for_empty_list() -> None:
    """Boundary: empty segments list returns False."""
    assert segments.is_open_for([], session_id="SES-001") is False


def test_latest_open_segment_returns_most_recent_active() -> None:
    earlier = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    later = earlier + timedelta(minutes=30)
    seg_old = segments.open_segment(session_id="SES-OLD", started_at=earlier)
    seg_new = segments.open_segment(session_id="SES-NEW", started_at=later)
    result = segments.latest_open_segment([seg_old, seg_new])
    assert result is not None
    assert result.session_id == "SES-NEW"


def test_latest_open_segment_skips_closed() -> None:
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    closed = segments.close_segment(
        segments.open_segment(session_id="SES-X", started_at=started),
        ended_at=started + timedelta(minutes=5),
        eu_minutes=Decimal("30"),
    )
    assert segments.latest_open_segment([closed]) is None


def test_close_segment_high_precision_decimal() -> None:
    """Sub-quantum durations preserve precision via Decimal arithmetic."""
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    # 7.5 minutes -> 0.25 EU exactly.
    ended = started + timedelta(seconds=450)
    seg = segments.open_segment(session_id="SES-001", started_at=started)
    closed = segments.close_segment(seg, ended_at=ended, eu_minutes=Decimal("30"))
    assert closed.eu == 0.25
    assert closed.active_minutes == 7.5
