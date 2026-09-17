"""The console performance harness records a latency distribution per fleet size.

This is the shape gate, not the budget gate: it holds the harness to recording a real
distribution at every declared fleet size, with percentiles in rank order and every
sampled frame proven settled, and it holds each model to refusing a reading that cannot
be true. The wall-clock budgets are gated in the perf lane
(``tests/perf/surfaces/tui/console/test_console_key_cost.py``), which is where a timing
figure belongs.

The profile here runs at a reduced sample count. A distribution's shape does not need a
hundred keys to prove, and this tier runs beside the rest of the TUI suite.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.surfaces.tui.console.perf import (
    FLEET_SIZES,
    ConsoleLatencyProfile,
    Distribution,
    FleetLatency,
    PerformanceHarness,
    _scaled_fixture,
)

TESTS_ROOT = Path(__file__).resolve().parents[4]
FIXTURE_DIR = TESTS_ROOT / "fixtures" / "console" / "golden" / "fixture"

#: Keys pressed per fleet size here. The budget lane samples far more; this tier only
#: proves the distribution is recorded, which one key per rank already shows.
SHAPE_SAMPLES = 8


@pytest.fixture(scope="module")
def harness() -> PerformanceHarness:
    """Return a harness over the tracked console fixture."""
    return PerformanceHarness(FIXTURE_DIR)


@pytest.fixture(scope="module")
def profile(harness: PerformanceHarness) -> ConsoleLatencyProfile:
    """Record one profile across every declared fleet size."""
    return asyncio.run(harness.profile(samples=SHAPE_SAMPLES))


def test_the_declared_fleet_sizes_are_one_ten_a_hundred_and_a_hundred_and_fifty() -> None:
    assert FLEET_SIZES == (1, 10, 100, 150)


def test_profile_records_a_distribution_at_every_declared_fleet_size(
    profile: ConsoleLatencyProfile,
) -> None:
    assert [record.runs for record in profile.fleet] == list(FLEET_SIZES)
    assert sorted(profile.by_runs()) == sorted(FLEET_SIZES)


def test_every_recorded_distribution_holds_every_sample(profile: ConsoleLatencyProfile) -> None:
    assert [record.key.count for record in profile.fleet] == [SHAPE_SAMPLES] * len(FLEET_SIZES)


def test_every_recorded_distribution_orders_its_percentiles_by_rank(
    profile: ConsoleLatencyProfile,
) -> None:
    for runs, seen in profile.by_runs().items():
        ranked = [seen.min_ms, seen.p50_ms, seen.p90_ms, seen.p99_ms, seen.max_ms]
        assert ranked == sorted(ranked), f"fleet {runs} percentiles out of rank order: {ranked}"


def test_every_recorded_latency_is_positive(profile: ConsoleLatencyProfile) -> None:
    assert [runs for runs, seen in profile.by_runs().items() if seen.min_ms <= 0.0] == []


def test_every_sampled_frame_was_already_the_settled_frame(
    profile: ConsoleLatencyProfile,
) -> None:
    """The captured frame is the settled frame, so the sample measured the whole paint."""
    noisy = {record.runs: record.settle_cycles for record in profile.fleet}
    assert noisy == dict.fromkeys(FLEET_SIZES, 1)


def test_a_profile_splits_into_a_floor_and_an_increment(profile: ConsoleLatencyProfile) -> None:
    cost = profile.key_cost()
    assert cost.floor_runs == min(FLEET_SIZES)
    assert cost.span_runs == max(FLEET_SIZES) - min(FLEET_SIZES)
    assert cost.floor_ms == profile.by_runs()[min(FLEET_SIZES)].p50_ms
    assert cost.floor_tail_ms == profile.by_runs()[min(FLEET_SIZES)].p99_ms


def test_scaling_the_fleet_gives_the_asked_number_of_uniquely_named_runs() -> None:
    fixture = _scaled_fixture(PerformanceHarness(FIXTURE_DIR).fixture, 150)
    assert len(fixture.proto.fleet) == 150
    assert len(fixture.fleet_by_run) == 150


def test_scaling_the_fleet_to_one_run_keeps_a_single_row() -> None:
    fixture = _scaled_fixture(PerformanceHarness(FIXTURE_DIR).fixture, 1)
    assert len(fixture.proto.fleet) == 1


def test_scaling_the_fleet_below_one_run_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one run"):
        _scaled_fixture(PerformanceHarness(FIXTURE_DIR).fixture, 0)


def test_a_harness_over_a_directory_with_no_registers_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        PerformanceHarness(tmp_path)


def test_a_measurement_with_no_samples_is_refused(harness: PerformanceHarness) -> None:
    with pytest.raises(ValueError, match="at least one sample"):
        asyncio.run(harness.key_latency(1, samples=0))


def test_a_measurement_of_an_empty_fleet_is_refused(harness: PerformanceHarness) -> None:
    with pytest.raises(ValueError, match="at least one run"):
        asyncio.run(harness.key_latency(0, samples=1))


def test_a_profile_over_no_fleet_size_is_refused(harness: PerformanceHarness) -> None:
    with pytest.raises(ValueError, match="at least one fleet size"):
        asyncio.run(harness.profile(fleet_sizes=()))


def test_a_profile_naming_one_fleet_size_twice_is_refused(harness: PerformanceHarness) -> None:
    with pytest.raises(ValidationError, match="records each fleet size once"):
        asyncio.run(harness.profile(fleet_sizes=(1, 1), samples=1))


def test_a_single_sample_is_every_percentile_of_its_distribution() -> None:
    seen = Distribution.of([2.5])
    assert seen.count == 1
    assert (seen.min_ms, seen.p50_ms, seen.p90_ms, seen.p99_ms, seen.max_ms) == (2.5,) * 5


def test_two_samples_split_at_the_nearest_rank() -> None:
    seen = Distribution.of([4.0, 1.0])
    assert (seen.p50_ms, seen.p90_ms, seen.p99_ms) == (1.0, 4.0, 4.0)
    assert seen.mean_ms == pytest.approx(2.5)


def test_a_hundred_samples_put_each_percentile_on_its_own_rank() -> None:
    seen = Distribution.of([float(value) for value in range(100)])
    assert (seen.p50_ms, seen.p90_ms, seen.p99_ms) == (49.0, 89.0, 98.0)
    assert (seen.min_ms, seen.max_ms) == (0.0, 99.0)
    assert seen.mean_ms == pytest.approx(49.5)


def test_a_distribution_of_no_samples_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one sample"):
        Distribution.of([])


def _distribution(p50: float) -> Distribution:
    return Distribution(
        count=1, min_ms=p50, p50_ms=p50, p90_ms=p50, p99_ms=p50, max_ms=p50, mean_ms=p50
    )


def test_a_distribution_of_a_negative_latency_is_refused() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        _distribution(-1.0)


def test_a_distribution_carrying_an_unknown_key_is_refused() -> None:
    payload: dict[str, Any] = _distribution(1.0).model_dump()
    payload["p95_ms"] = 1.0
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Distribution.model_validate(payload)


def test_a_fleet_record_whose_frame_never_settled_is_refused() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        FleetLatency(runs=1, key=_distribution(1.0), settle_cycles=0)


def test_a_profile_with_no_fleet_record_is_refused() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        ConsoleLatencyProfile(fleet=())


def test_a_key_cost_split_over_one_fleet_size_is_refused() -> None:
    single = ConsoleLatencyProfile(
        fleet=(FleetLatency(runs=7, key=_distribution(1.0), settle_cycles=1),)
    )
    with pytest.raises(ValueError, match="two different fleet sizes"):
        single.key_cost()
