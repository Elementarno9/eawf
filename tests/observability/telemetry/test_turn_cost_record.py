"""Strict-validation tests for :class:`TurnCostRecord` and its run row."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import AgentSessionRole
from eawf.observability.telemetry.turn_cost import (
    CompletedUnitRun,
    PriceSource,
    TurnCostRecord,
)

pytestmark = pytest.mark.unit


def _record_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "fixture_id": "turn-cost-v1",
        "harness_revision": "rev-a1b2c3",
        "runtime": "claude",
        "model": "claude-opus-4-7",
        "unit_count": 3,
        "p50_wall_clock_ms": 5_000,
        "p90_wall_clock_ms": 9_000,
        "p50_cost_usd": Decimal("0.05"),
        "p90_cost_usd": Decimal("0.09"),
        "verification_cost_usd": Decimal("0.20"),
        "execution_cost_usd": Decimal("0.80"),
        "token_total": 4_000,
        "unattributed_run_count": 0,
        "unpriced_run_count": 0,
        "reasoning_summand_unsettled": False,
        "reasoning_summand_unsettled_run_count": 0,
    }
    kwargs.update(overrides)
    return kwargs


def test_turn_cost_record_accepts_the_full_declared_shape() -> None:
    """The fixture, harness revision, runtime/model tuple and split costs land."""
    record = TurnCostRecord(**_record_kwargs())

    assert record.fixture_id == "turn-cost-v1"
    assert record.harness_revision == "rev-a1b2c3"
    assert (record.runtime, record.model) == ("claude", "claude-opus-4-7")
    assert record.p50_wall_clock_ms == 5_000
    assert record.p90_wall_clock_ms == 9_000
    assert record.p50_cost_usd == Decimal("0.05")
    assert record.p90_cost_usd == Decimal("0.09")
    assert record.verification_cost_usd == Decimal("0.20")
    assert record.execution_cost_usd == Decimal("0.80")


@pytest.mark.parametrize(
    "missing",
    [
        "harness_revision",
        "fixture_id",
        "verification_cost_usd",
        "execution_cost_usd",
        "p50_wall_clock_ms",
        "p90_cost_usd",
    ],
)
def test_turn_cost_record_missing_required_field_raises(missing: str) -> None:
    """Every declared field is required; none silently defaults."""
    kwargs = _record_kwargs()
    del kwargs[missing]

    with pytest.raises(ValidationError, match=missing):
        TurnCostRecord(**kwargs)


@pytest.mark.parametrize(
    "combined",
    ["cost_usd", "total_cost_usd", "combined_cost_usd"],
)
def test_turn_cost_record_combined_cost_field_raises(combined: str) -> None:
    """A combined cost field is forbidden; the split is the contract."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TurnCostRecord(**_record_kwargs(**{combined: Decimal("1.00")}))


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("unit_count", 0),
        ("p50_wall_clock_ms", -1),
        ("p50_cost_usd", Decimal("-0.01")),
        ("verification_cost_usd", Decimal("-1")),
        ("token_total", -1),
        ("unpriced_run_count", -1),
        ("fixture_id", ""),
        ("harness_revision", ""),
    ],
)
def test_turn_cost_record_out_of_range_field_raises(field_name: str, value: object) -> None:
    """Boundary values below the declared floor are rejected."""
    with pytest.raises(ValidationError, match=field_name):
        TurnCostRecord(**_record_kwargs(**{field_name: value}))


def test_turn_cost_record_unknown_runtime_raises() -> None:
    """The runtime half of the tuple is a closed set."""
    with pytest.raises(ValidationError, match="runtime"):
        TurnCostRecord(**_record_kwargs(runtime="gemini"))


def test_turn_cost_record_is_frozen() -> None:
    """A produced record is immutable, so no consumer can re-add a total."""
    record = TurnCostRecord(**_record_kwargs())

    with pytest.raises(ValidationError, match="frozen"):
        record.execution_cost_usd = Decimal("2")  # type: ignore[misc]


def test_turn_cost_record_unit_count_one_is_the_lower_boundary() -> None:
    """A single-unit record is valid; zero units is not."""
    assert TurnCostRecord(**_record_kwargs(unit_count=1)).unit_count == 1


def test_completed_unit_run_rejects_unknown_field() -> None:
    """The run row is strict too, so a stray column never rides along."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CompletedUnitRun(
            run_id="run-a",
            wave_id="P31-I01-W01",
            role=AgentSessionRole.EXECUTOR,
            runtime="claude",
            model="claude-opus-4-7",
            wall_clock_ms=10,
            combined_tokens=5,  # type: ignore[call-arg]
        )


def test_completed_unit_run_defaults_are_zero_and_unpriced() -> None:
    """A minimal run carries no tokens, no cost and no price source."""
    run = CompletedUnitRun(
        run_id="run-a",
        wave_id="P31-I01-W01",
        runtime="claude",
        model="claude-opus-4-7",
        wall_clock_ms=0,
    )

    assert run.role is None
    assert run.reasoning_tokens == 0
    assert run.cost_usd == Decimal("0")
    assert run.price_source is None


def test_completed_unit_run_negative_wall_clock_raises() -> None:
    """Elapsed time cannot run backwards."""
    with pytest.raises(ValidationError, match="wall_clock_ms"):
        CompletedUnitRun(
            run_id="run-a",
            wave_id="P31-I01-W01",
            runtime="claude",
            model="claude-opus-4-7",
            wall_clock_ms=-1,
        )


def test_price_source_requires_a_non_empty_kind() -> None:
    """An empty provenance string is not a price source."""
    with pytest.raises(ValidationError, match="kind"):
        PriceSource(kind="")
