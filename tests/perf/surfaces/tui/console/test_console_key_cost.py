"""The per-key floor and the per-row increment are budgeted and gated apart.

One key costs a fixed amount plus whatever the fleet adds. The two halves regress for
different reasons -- a heavier dispatch or paint path lifts the floor, an accidental scan
over the runs lifts the increment -- so each carries its own budget and its own gate. A
single combined number would let a doubled floor hide behind a fleet that got cheaper, or
a per-row scan hide behind a floor with slack.

Both gated figures are medians, and both ceilings carry large headroom, because this lane
runs on shared CI runners and on developer machines with other work on them. The p99 of a
sub-millisecond operation on such a host is a scheduler spike, not a property of the
console: measured across sixteen profiles of this console the median held between 0.451ms
and 0.582ms while the p99 of the same runs swung between 0.49ms and 14.1ms. The tail is
recorded in the profile and reported on failure; it is not what the gate reads.

The last four tests are the gate's own proof. They feed synthetic profiles through the
same split and assert that a per-keystroke regression trips the floor gate alone and a
per-row regression trips the increment gate alone, so neither gate is a rubber stamp and
neither absorbs the other's defect.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from eawf.surfaces.tui.console.perf import (
    DEFAULT_KEY_SAMPLES,
    FLEET_SIZES,
    KEY_FLOOR_CEILING_MS,
    ROW_INCREMENT_CEILING_US,
    ConsoleLatencyProfile,
    Distribution,
    FleetLatency,
    KeyCost,
    PerformanceHarness,
)

TESTS_ROOT = Path(__file__).resolve().parents[4]
FIXTURE_DIR = TESTS_ROOT / "fixtures" / "console" / "golden" / "fixture"

#: Local escape hatch -- ``EAWF_SKIP_PERF=1 uv run pytest`` skips the measured gates.
_skip_perf = pytest.mark.skipif(
    os.environ.get("EAWF_SKIP_PERF") == "1",
    reason="EAWF_SKIP_PERF=1 - perf gate skipped for local dev",
)


#: How many independent profiles the increment gate may read before it fails.
_INCREMENT_ATTEMPTS = 3


def _measure() -> KeyCost:
    """Record one profile across every declared fleet size and split it."""
    harness = PerformanceHarness(FIXTURE_DIR)
    profile = asyncio.run(harness.profile(samples=DEFAULT_KEY_SAMPLES))
    return profile.key_cost()


@pytest.fixture(scope="module")
def measured() -> KeyCost:
    """Return one measured key-cost split."""
    return _measure()


def _least_increment(first: KeyCost) -> KeyCost:
    """Return the split with the smallest increment, re-measuring only while over budget.

    The increment is the gap between two medians taken in separate windows, divided by a
    span of tens of runs, so one scheduler burst in either window moves it by several
    microseconds per run. A per-row regression lifts every profile; a burst lifts one.
    Reading the least of a few independent profiles keeps the first and leaves only the
    second behind.
    """
    least = first
    for _ in range(_INCREMENT_ATTEMPTS - 1):
        if least.increment_us < ROW_INCREMENT_CEILING_US:
            break
        again = _measure()
        if again.increment_us < least.increment_us:
            least = again
    return least


@_skip_perf
def test_per_key_floor_within_budget(measured: KeyCost) -> None:
    assert measured.floor_runs == min(FLEET_SIZES)
    assert measured.floor_ms < KEY_FLOOR_CEILING_MS, (
        f"per-key floor {measured.floor_ms:.3f}ms at a fleet of {measured.floor_runs} exceeds "
        f"the budget {KEY_FLOOR_CEILING_MS:.1f}ms (tail at that fleet "
        f"{measured.floor_tail_ms:.3f}ms) - likely a per-keystroke regression"
    )


@_skip_perf
def test_per_row_increment_within_budget(measured: KeyCost) -> None:
    assert measured.span_runs == sorted(FLEET_SIZES)[-1] - sorted(FLEET_SIZES)[-2]
    least = _least_increment(measured)
    assert least.increment_us < ROW_INCREMENT_CEILING_US, (
        f"per-row increment {least.increment_us:.3f}us/run over {least.span_runs} runs "
        f"in each of up to {_INCREMENT_ATTEMPTS} profiles "
        f"exceeds the budget {ROW_INCREMENT_CEILING_US:.1f}us/run - likely a new scan over "
        "the fleet register"
    )


def test_the_two_budgets_are_separate_figures_in_separate_units() -> None:
    """One ceiling in milliseconds per key, one in microseconds per run: never one number."""
    assert KEY_FLOOR_CEILING_MS != ROW_INCREMENT_CEILING_US
    assert KEY_FLOOR_CEILING_MS > 0.0
    assert ROW_INCREMENT_CEILING_US > 0.0


def _flat(latency_ms: float) -> Distribution:
    """Return a distribution whose every percentile is ``latency_ms``."""
    return Distribution(
        count=DEFAULT_KEY_SAMPLES,
        min_ms=latency_ms,
        p50_ms=latency_ms,
        p90_ms=latency_ms,
        p99_ms=latency_ms,
        max_ms=latency_ms,
        mean_ms=latency_ms,
    )


def _profile(*, floor_ms: float, increment_us: float) -> KeyCost:
    """Return the split of a synthetic profile with the asked floor and slope."""
    smallest, largest = min(FLEET_SIZES), max(FLEET_SIZES)
    top_ms = floor_ms + increment_us * (largest - smallest) / 1000.0
    return ConsoleLatencyProfile(
        fleet=(
            FleetLatency(runs=smallest, key=_flat(floor_ms), settle_cycles=1),
            FleetLatency(runs=largest, key=_flat(top_ms), settle_cycles=1),
        )
    ).key_cost()


def test_a_healthy_console_passes_both_gates() -> None:
    healthy = _profile(floor_ms=0.5, increment_us=0.7)
    assert healthy.floor_ms < KEY_FLOOR_CEILING_MS
    assert healthy.increment_us < ROW_INCREMENT_CEILING_US


def test_a_per_keystroke_regression_trips_the_floor_gate_alone() -> None:
    """A dispatch path that re-reads the world every key lifts the floor, not the slope."""
    regressed = _profile(floor_ms=KEY_FLOOR_CEILING_MS * 5.0, increment_us=0.7)
    assert regressed.floor_ms > KEY_FLOOR_CEILING_MS
    assert regressed.increment_us == pytest.approx(0.7)
    assert regressed.increment_us < ROW_INCREMENT_CEILING_US


def test_a_per_row_regression_trips_the_increment_gate_alone() -> None:
    """A new scan over the fleet lifts the slope while the floor stays where it was."""
    regressed = _profile(floor_ms=0.5, increment_us=ROW_INCREMENT_CEILING_US * 20.0)
    assert regressed.increment_us > ROW_INCREMENT_CEILING_US
    assert regressed.floor_ms < KEY_FLOOR_CEILING_MS


def test_a_console_that_does_no_per_row_work_reads_as_a_flat_slope() -> None:
    """The boundary case: identical medians at both ends are a zero increment, not a fault."""
    flat = _profile(floor_ms=0.5, increment_us=0.0)
    assert flat.increment_us == pytest.approx(0.0)
    assert flat.span_runs == max(FLEET_SIZES) - min(FLEET_SIZES)


def _remeasured(monkeypatch: pytest.MonkeyPatch, increments: list[float]) -> list[float]:
    """Make each re-measure answer the next of ``increments``; return what is left unread."""
    queue = list(increments)

    def fake() -> KeyCost:
        return _profile(floor_ms=0.5, increment_us=queue.pop(0))

    monkeypatch.setattr(sys.modules[__name__], "_measure", fake)
    return queue


def test_an_increment_within_budget_is_not_measured_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    left = _remeasured(monkeypatch, [0.1])
    first = _profile(floor_ms=0.5, increment_us=0.7)
    assert _least_increment(first) is first
    assert left == [0.1]


def test_a_burst_in_one_profile_is_outvoted_by_the_next(monkeypatch: pytest.MonkeyPatch) -> None:
    """One noisy window over budget, then a quiet one: the gate reads the quiet one."""
    left = _remeasured(monkeypatch, [0.4, 9.0])
    noisy = _profile(floor_ms=0.5, increment_us=ROW_INCREMENT_CEILING_US + 1.0)
    assert _least_increment(noisy).increment_us == pytest.approx(0.4)
    assert left == [9.0]


def test_a_per_row_regression_fails_every_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real regression lifts every profile, so the least of them is still over budget."""
    over = ROW_INCREMENT_CEILING_US * 20.0
    left = _remeasured(monkeypatch, [over + 1.0, over + 2.0, 0.1])
    least = _least_increment(_profile(floor_ms=0.5, increment_us=over))
    assert least.increment_us == pytest.approx(over)
    assert least.increment_us > ROW_INCREMENT_CEILING_US
    assert left == [0.1], f"read {_INCREMENT_ATTEMPTS} profiles at most"


def test_an_increment_just_over_the_ceiling_is_measured_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    left = _remeasured(monkeypatch, [0.2])
    over = _profile(floor_ms=0.5, increment_us=ROW_INCREMENT_CEILING_US + 0.01)
    assert _least_increment(over).increment_us == pytest.approx(0.2)
    assert left == []
