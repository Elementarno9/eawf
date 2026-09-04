"""Price-provenance and token-summand tests for the turn-cost producer.

Two independent filters guard the cost sums. A row with no price source is
counted as unpriced and never summed as zero, because "cost nothing" and
"cost unknown" are different facts and collapsing them understates spend.
A row from a runtime whose reported output tokens already absorb reasoning
tokens is flagged and excluded, because its token total is not comparable
with the runtimes that report the two separately.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from eawf.kernel.state.enums import AgentSessionRole, WaveStatus
from eawf.kernel.state.models import Wave
from eawf.observability.telemetry.models import RuntimeName
from eawf.observability.telemetry.turn_cost import (
    REASONING_UNSETTLED_RUNTIMES,
    CompletedUnitRun,
    PriceSource,
    TurnCostRecord,
    build_turn_cost_record,
)

pytestmark = pytest.mark.unit

_TS = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
_SNAPSHOT = PriceSource(kind="pricing_snapshot", pricing_version="2026.05.17")
_VENDOR = PriceSource(kind="vendor_reported")


def _closed_wave(wave_id: str) -> Wave:
    return Wave(
        id=wave_id,
        iter_id="P31-I01",
        title=f"Completed unit {wave_id}",
        status=WaveStatus.CLOSED,
        opened_at=_TS,
    )


def _run(
    run_id: str,
    *,
    runtime: RuntimeName = "claude",
    cost_usd: str = "0",
    price_source: PriceSource | None = _SNAPSHOT,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> CompletedUnitRun:
    return CompletedUnitRun(
        run_id=run_id,
        wave_id="P31-I01-W01",
        role=AgentSessionRole.EXECUTOR,
        runtime=runtime,
        model="claude-opus-4-7",
        wall_clock_ms=1_000,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        cost_usd=Decimal(cost_usd),
        price_source=price_source,
    )


def _build(runs: list[CompletedUnitRun]) -> TurnCostRecord:
    return build_turn_cost_record(
        waves=[_closed_wave("P31-I01-W01")],
        runs=runs,
        fixture_id="turn-cost-v1",
        harness_revision="rev-a1b2c3",
        runtime="claude",
        model="claude-opus-4-7",
    )


def test_unpriced_rows_are_counted_and_never_summed() -> None:
    """A row without a price source is excluded, not folded in as zero."""
    record = _build(
        [
            _run("run-priced", cost_usd="0.40"),
            _run("run-unpriced", cost_usd="9.99", price_source=None),
        ]
    )

    assert record.execution_cost_usd == Decimal("0.40")
    assert record.unpriced_run_count == 1


def test_unpriced_row_does_not_dilute_the_cost_percentiles() -> None:
    """Excluding an unpriced run leaves the priced unit cost intact."""
    record = _build([_run("run-unpriced", cost_usd="9.99", price_source=None)])

    assert record.p50_cost_usd == Decimal("0")
    assert record.p90_cost_usd == Decimal("0")
    assert record.unpriced_run_count == 1
    assert record.token_total == 0


@pytest.mark.parametrize("source", [_SNAPSHOT, _VENDOR])
def test_every_price_source_kind_is_summable(source: PriceSource) -> None:
    """Both provenance kinds carry a price, so both rows sum."""
    record = _build([_run("run-priced", cost_usd="0.40", price_source=source)])

    assert record.execution_cost_usd == Decimal("0.40")
    assert record.unpriced_run_count == 0


def test_token_total_is_the_four_class_sum_without_reasoning_tokens() -> None:
    """Reasoning tokens are never a summand of the token total."""
    record = _build(
        [
            _run(
                "run-priced",
                cost_usd="0.10",
                input_tokens=100,
                output_tokens=20,
                cache_read_tokens=300,
                cache_write_tokens=40,
                reasoning_tokens=5_000,
            )
        ]
    )

    assert record.token_total == 460


def test_reasoning_tokens_alone_contribute_nothing_to_the_total() -> None:
    """A row whose only tokens are reasoning tokens totals zero."""
    record = _build([_run("run-priced", cost_usd="0.10", reasoning_tokens=9_999)])

    assert record.token_total == 0


def test_codex_rows_are_flagged_unsettled_and_excluded() -> None:
    """A codex row is counted, flagged and kept out of the sums."""
    record = _build(
        [
            _run("run-claude", cost_usd="0.40", input_tokens=100),
            _run("run-codex", runtime="codex", cost_usd="5.00", input_tokens=999),
        ]
    )

    assert record.reasoning_summand_unsettled is True
    assert record.reasoning_summand_unsettled_run_count == 1
    assert record.execution_cost_usd == Decimal("0.40")
    assert record.token_total == 100


def test_record_is_not_flagged_when_no_unsettled_runtime_appears() -> None:
    """The flag is off by default and only a real row turns it on."""
    record = _build([_run("run-claude", cost_usd="0.40")])

    assert record.reasoning_summand_unsettled is False
    assert record.reasoning_summand_unsettled_run_count == 0


def test_unsettled_row_is_not_double_counted_as_unpriced() -> None:
    """The exclusion ladder puts each run in exactly one bucket."""
    record = _build(
        [
            _run("run-claude", cost_usd="0.40"),
            _run("run-codex", runtime="codex", cost_usd="5.00", price_source=None),
        ]
    )

    assert record.reasoning_summand_unsettled_run_count == 1
    assert record.unpriced_run_count == 0


def test_unsettled_runtime_set_names_codex() -> None:
    """The exclusion set is the single declared source of the runtime list."""
    assert set(REASONING_UNSETTLED_RUNTIMES) == {"codex"}
