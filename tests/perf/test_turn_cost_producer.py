"""Completed-unit producer tests for the turn-cost record.

The load-bearing property is *split invariance*: the unit of measurement is
a closed wave, so expressing one wave's work as three runs instead of one
must not move the record by a single byte. A producer that percentiled over
runs rather than units would fail this, which is exactly the confusion the
unit definition exists to prevent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from eawf.kernel.state.enums import AgentSessionRole, WaveStatus
from eawf.kernel.state.models import Wave
from eawf.observability.telemetry.models import RuntimeName
from eawf.observability.telemetry.turn_cost import (
    CompletedUnitRun,
    PriceSource,
    TurnCostRecord,
    build_turn_cost_record,
)

pytestmark = pytest.mark.unit

_TS = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
_PRICED = PriceSource(kind="pricing_snapshot", pricing_version="2026.05.17")


def _wave(wave_id: str, *, status: WaveStatus = WaveStatus.CLOSED) -> Wave:
    return Wave(
        id=wave_id,
        iter_id="P31-I01",
        title=f"Completed unit {wave_id}",
        status=status,
        opened_at=_TS,
    )


def _run(
    run_id: str,
    wave_id: str,
    *,
    role: AgentSessionRole | None = AgentSessionRole.EXECUTOR,
    runtime: RuntimeName = "claude",
    wall_clock_ms: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: str = "0",
    price_source: PriceSource | None = _PRICED,
) -> CompletedUnitRun:
    return CompletedUnitRun(
        run_id=run_id,
        wave_id=wave_id,
        role=role,
        runtime=runtime,
        model="claude-opus-4-7",
        wall_clock_ms=wall_clock_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=Decimal(cost_usd),
        price_source=price_source,
    )


def _build(waves: list[Wave], runs: list[CompletedUnitRun]) -> TurnCostRecord:
    return build_turn_cost_record(
        waves=waves,
        runs=runs,
        fixture_id="turn-cost-v1",
        harness_revision="rev-a1b2c3",
        runtime="claude",
        model="claude-opus-4-7",
    )


def test_build_turn_cost_record_split_invariance_across_three_runs() -> None:
    """One wave as one run and as three summing runs render byte-identically."""
    waves = [_wave("P31-I01-W01")]
    whole = [
        _run(
            "run-whole",
            "P31-I01-W01",
            wall_clock_ms=9_000,
            input_tokens=300,
            output_tokens=60,
            cost_usd="0.90",
        )
    ]
    split = [
        _run(
            "run-a",
            "P31-I01-W01",
            wall_clock_ms=4_000,
            input_tokens=100,
            output_tokens=10,
            cost_usd="0.40",
        ),
        _run(
            "run-b",
            "P31-I01-W01",
            wall_clock_ms=3_000,
            input_tokens=100,
            output_tokens=20,
            cost_usd="0.30",
        ),
        _run(
            "run-c",
            "P31-I01-W01",
            wall_clock_ms=2_000,
            input_tokens=100,
            output_tokens=30,
            cost_usd="0.20",
        ),
    ]

    whole_record = _build(waves, whole)
    split_record = _build(waves, split)

    assert whole_record.model_dump_json() == split_record.model_dump_json()


def test_build_turn_cost_record_percentiles_are_per_unit_not_per_run() -> None:
    """p50/p90 rank over the per-unit aggregates, not over the runs."""
    waves = [_wave(f"P31-I01-W{idx:02d}") for idx in range(1, 11)]
    runs = [
        _run(
            f"run-{idx}",
            f"P31-I01-W{idx:02d}",
            wall_clock_ms=idx * 1_000,
            cost_usd=f"0.{idx:02d}",
        )
        for idx in range(1, 11)
    ]

    record = _build(waves, runs)

    assert record.unit_count == 10
    assert record.p50_wall_clock_ms == 5_000
    assert record.p90_wall_clock_ms == 9_000
    assert record.p50_cost_usd == Decimal("0.05")
    assert record.p90_cost_usd == Decimal("0.09")


def test_build_turn_cost_record_single_unit_percentiles_equal_the_value() -> None:
    """A one-unit corpus reports that unit for both percentiles."""
    record = _build(
        [_wave("P31-I01-W01")],
        [_run("run-a", "P31-I01-W01", wall_clock_ms=1_234, cost_usd="0.25")],
    )

    assert record.p50_wall_clock_ms == 1_234
    assert record.p90_wall_clock_ms == 1_234
    assert record.p50_cost_usd == Decimal("0.25")


def test_build_turn_cost_record_ignores_runs_on_waves_that_are_not_closed() -> None:
    """A run on an open wave is not a completed unit."""
    waves = [
        _wave("P31-I01-W01"),
        _wave("P31-I01-W02", status=WaveStatus.IN_PROGRESS),
    ]
    runs = [
        _run("run-a", "P31-I01-W01", wall_clock_ms=1_000, cost_usd="0.10"),
        _run("run-b", "P31-I01-W02", wall_clock_ms=9_999, cost_usd="9.99"),
    ]

    record = _build(waves, runs)

    assert record.unit_count == 1
    assert record.p50_wall_clock_ms == 1_000
    assert record.execution_cost_usd == Decimal("0.10")


def test_build_turn_cost_record_wall_clock_counts_every_joined_run() -> None:
    """Cost exclusions never shorten the unit's elapsed clock."""
    waves = [_wave("P31-I01-W01")]
    runs = [
        _run("run-a", "P31-I01-W01", wall_clock_ms=1_000, cost_usd="0.10"),
        _run(
            "run-b",
            "P31-I01-W01",
            role=AgentSessionRole.OPERATOR,
            wall_clock_ms=500,
            cost_usd="5.00",
        ),
    ]

    record = _build(waves, runs)

    assert record.p50_wall_clock_ms == 1_500
    assert record.execution_cost_usd == Decimal("0.10")
    assert record.unattributed_run_count == 1


def test_build_turn_cost_record_empty_corpus_raises_value_error() -> None:
    """No closed wave with a run is not a measurable corpus."""
    with pytest.raises(ValueError, match="no completed unit of work"):
        _build([], [])


def test_build_turn_cost_record_closed_wave_without_runs_raises_value_error() -> None:
    """A closed wave with zero attributed runs yields no unit."""
    with pytest.raises(ValueError, match="no completed unit of work"):
        _build([_wave("P31-I01-W01")], [])


def test_build_turn_cost_record_unknown_wave_raises_key_error() -> None:
    """A run naming a wave outside the corpus is a join error, not a skip."""
    with pytest.raises(KeyError, match="references unknown wave"):
        _build([_wave("P31-I01-W01")], [_run("run-a", "P31-I01-W99")])


def test_build_turn_cost_record_run_without_role_raises_value_error() -> None:
    """An unlabelled run raises instead of being summed as execution."""
    with pytest.raises(ValueError, match="carries no agent role"):
        _build(
            [_wave("P31-I01-W01")],
            [_run("run-a", "P31-I01-W01", role=None, cost_usd="1.00")],
        )
