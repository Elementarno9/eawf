"""A session whose requests keep rewriting the prompt cache raises ``cache_miss_storm``.

A miss reads nothing from the cache and writes at least 200,000 tokens into it;
three misses in one session raise the finding, which lists each miss with its gap
since the lane's previous request, flags gaps past the 5-minute and 1-hour cache
lifetimes and states their API-equivalent cost.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from eawf.observability.reflect.analysis import (
    CACHE_MISS_STORM_COUNT,
    CACHE_MISS_WRITE_TOKENS,
    CacheMiss,
    Finding,
    HistorySession,
    Metric,
    Severity,
    analyze_sessions,
)
from eawf.observability.reflect.rates import PricingQuality
from eawf.observability.telemetry.models import ObservedRequest, PriceSourceKind

_T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
_NOW = datetime(2026, 11, 10, tzinfo=UTC)
_REF = f"vsid-{1:032x}"
_LANE = f"agent-{7:032x}"


def _session() -> HistorySession:
    return HistorySession(
        session_ref=_REF,
        runtime="claude",
        project_key="cwd-a",
        project_label="SYN",
        started_at=_T0,
        ended_at=_T0 + timedelta(hours=3),
        model="claude-opus-4-7",
        model_family="claude-opus",
        orchestrated=False,
        metrics={Metric.TOTAL_TOKENS: 1_000_000.0, Metric.MODEL_STEPS: 10.0},
        cost_usd=None,
        pricing_quality=PricingQuality.VERIFIED,
    )


def _request(
    ordinal: int,
    at: timedelta | None,
    *,
    read: int = 0,
    write: int = 700_000,
    lane: str = "main",
    cost: Decimal | None = Decimal("3.50"),
) -> ObservedRequest:
    return ObservedRequest(
        vendor_session_ref=_REF,
        runtime="claude",
        request_ref=f"req-{ordinal + (1_000 if lane != 'main' else 0):032x}",
        lane=lane,
        ordinal=ordinal,
        ts=None if at is None else _T0 + at,
        model="claude-opus-4-7",
        input_tokens=10,
        output_tokens=10,
        cache_read_tokens=read,
        cache_write_tokens=write,
        total_tokens=20 + read + write,
        cost_usd=cost,
        price_source=(
            PriceSourceKind.UNPRICED if cost is None else PriceSourceKind.LIST_RECONSTRUCTED
        ),
        rate_table_version=None if cost is None else "v",
    )


def _storm(requests: list[ObservedRequest]) -> Finding | None:
    analysis = analyze_sessions([_session()], requests=requests, now=_NOW)
    (row,) = analysis.sessions
    return next((item for item in row.findings if item.finding_type == "cache_miss_storm"), None)


def _warm(ordinal: int, at: timedelta) -> ObservedRequest:
    return _request(ordinal, at, read=500_000, write=1_000)


def test_storm_threshold() -> None:
    two = [
        _warm(1, timedelta(0)),
        _request(2, timedelta(minutes=2)),
        _request(3, timedelta(minutes=10)),
    ]
    assert _storm(two) is None

    three = [*two, _request(4, timedelta(hours=2))]
    finding = _storm(three)
    assert finding is not None
    assert finding.severity is Severity.INVESTIGATE
    assert finding.value == pytest.approx(3.0)
    assert [miss.ordinal for miss in finding.misses] == [2, 3, 4]
    gaps = [miss.gap_seconds for miss in finding.misses]
    assert gaps == pytest.approx([120.0, 480.0, 6_600.0])
    assert [miss.gap_flag for miss in finding.misses] == [None, "over_5m", "over_1h"]
    assert [miss.cost_usd for miss in finding.misses] == [Decimal("3.50")] * 3
    assert "$10.50 API-equivalent" in finding.summary
    assert "3 requests" in finding.summary
    assert "2 came after a pause" in finding.summary
    assert finding.recommendation is not None


def test_threshold_constants() -> None:
    assert CACHE_MISS_WRITE_TOKENS == 200_000
    assert CACHE_MISS_STORM_COUNT == 3


def test_no_requests_raise_nothing() -> None:
    assert _storm([]) is None


def test_write_boundary_and_any_cache_read() -> None:
    below = [
        _request(n, timedelta(minutes=n), write=CACHE_MISS_WRITE_TOKENS - 1) for n in (1, 2, 3)
    ]
    assert _storm(below) is None
    at = [_request(n, timedelta(minutes=n), write=CACHE_MISS_WRITE_TOKENS) for n in (1, 2, 3)]
    finding = _storm(at)
    assert finding is not None
    assert len(finding.misses) == 3
    one_read = [_request(n, timedelta(minutes=n), read=1) for n in (1, 2, 3)]
    assert _storm(one_read) is None


def test_gap_boundaries_and_first_request() -> None:
    requests = [
        _request(1, timedelta(0)),
        _request(2, timedelta(minutes=5)),
        _request(3, timedelta(minutes=10, seconds=1)),
        _request(4, timedelta(hours=1, minutes=10, seconds=1)),
        _request(5, timedelta(hours=2, minutes=10, seconds=2)),
    ]
    finding = _storm(requests)
    assert finding is not None
    flags = {miss.ordinal: (miss.gap_seconds, miss.gap_flag) for miss in finding.misses}
    assert flags[1] == (None, None)
    assert flags[2] == (pytest.approx(300.0), None)
    assert flags[3] == (pytest.approx(301.0), "over_5m")
    assert flags[4] == (pytest.approx(3_600.0), "over_5m")
    assert flags[5] == (pytest.approx(3_601.0), "over_1h")


def test_gaps_are_per_lane_and_missing_timestamps_leave_none() -> None:
    requests = [
        _warm(1, timedelta(0)),
        _request(2, timedelta(minutes=1)),
        _request(1, timedelta(minutes=30), lane=_LANE),
        _request(2, None, lane=_LANE),
    ]
    finding = _storm(requests)
    assert finding is not None
    by_lane = {(miss.lane, miss.ordinal): miss for miss in finding.misses}
    assert by_lane[("main", 2)].gap_seconds == pytest.approx(60.0)
    assert by_lane[(_LANE, 1)].gap_seconds is None
    assert by_lane[(_LANE, 2)].gap_seconds is None
    assert by_lane[(_LANE, 2)].ts is None
    assert finding.misses[-1].ts is None


def test_unpriced_misses_are_named() -> None:
    requests = [
        _request(1, timedelta(0)),
        _request(2, timedelta(minutes=1)),
        _request(3, timedelta(minutes=2), cost=None),
    ]
    finding = _storm(requests)
    assert finding is not None
    assert finding.misses[-1].cost_usd is None
    assert "at least $7.00 API-equivalent, 1 unpriced" in finding.summary


def test_noise_session_raises_no_storm() -> None:
    noisy = _session().model_copy(update={"model": "codex-auto-review"})
    requests = [_request(n, timedelta(minutes=n)) for n in (1, 2, 3)]
    analysis = analyze_sessions([noisy], requests=requests, now=_NOW)
    assert analysis.sessions[0].findings == ()


def test_cache_miss_rejects_small_writes_and_unknown_flags() -> None:
    fields = {
        "lane": "main",
        "ordinal": 1,
        "ts": None,
        "cache_write_tokens": CACHE_MISS_WRITE_TOKENS,
        "gap_seconds": None,
        "gap_flag": None,
        "cost_usd": None,
    }
    assert CacheMiss.model_validate(fields).cache_write_tokens == CACHE_MISS_WRITE_TOKENS
    with pytest.raises(ValidationError):
        CacheMiss.model_validate(fields | {"cache_write_tokens": CACHE_MISS_WRITE_TOKENS - 1})
    with pytest.raises(ValidationError):
        CacheMiss.model_validate(fields | {"gap_flag": "over_1d"})
    with pytest.raises(ValidationError):
        CacheMiss.model_validate(fields | {"ordinal": 0})
    with pytest.raises(ValidationError):
        CacheMiss.model_validate(fields | {"extra": 1})
