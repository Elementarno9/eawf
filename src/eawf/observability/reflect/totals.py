"""What each listed Run spent, and the money and token totals per scope.

A Run's spend is read the way its own report reads it: the captured runtime when the
stop reading measured one, the usage readings its stream carries otherwise, and
nothing at all when the capture excluded it. A cost no reading priced stays ``None``,
so a total sums only the priced Runs and counts the rest as unpriced instead of
adding a zero for them.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict

from eawf.kernel.identity import project_code_of
from eawf.kernel.runtime.usage import UsagePayload, aggregate_usage
from eawf.kernel.state.epoch2.measurement import (
    CounterName,
    ExcludedRuntime,
    MeasuredRuntime,
    Observed,
)
from eawf.observability.reflect.runs import RunReading

logger = logging.getLogger(__name__)

#: How many microUSD one USD is.
_MICROUSD_PER_USD: Final = Decimal(1_000_000)

#: The captured counters a Run's cache tokens add up from.
_CACHE_COUNTERS: Final = (
    CounterName.CACHE_CREATION_INPUT_TOKENS,
    CounterName.CACHE_READ_INPUT_TOKENS,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RunSpend:
    """What one Run spent; a figure nothing reported is ``None``, never zero.

    Attributes:
        cost_microusd: The Run's cost, or ``None`` when no reading priced it.
        input_tokens: Its input tokens, or ``None`` when unreported.
        output_tokens: Its output tokens, or ``None`` when unreported.
        cache_tokens: Its prompt-cache tokens, or ``None`` when unreported.
    """

    cost_microusd: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_tokens: int | None = None


class TotalsRow(BaseModel):
    """The money and tokens of every listed session of one scope.

    Attributes:
        scope: The scope summed, as ``project:<code>``.
        cost_microusd: The priced sessions' cost, or ``None`` when none was priced.
        input_tokens: Their input tokens, or ``None`` when none reported any.
        output_tokens: Their output tokens, or ``None`` when none reported any.
        cache_tokens: Their prompt-cache tokens, or ``None`` when none reported any.
        priced_runs: The sessions whose cost was read.
        unpriced_runs: The sessions whose cost no reading priced; their spend is
            missing from ``cost_microusd``, so the cost is a floor whenever this is
            not zero.
        quotable: Whether the scope is the tree's own project.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: str
    cost_microusd: int | None
    input_tokens: int | None
    output_tokens: int | None
    cache_tokens: int | None
    priced_runs: int
    unpriced_runs: int
    quotable: bool


def _observed(runtime: MeasuredRuntime, name: CounterName) -> Decimal | None:
    """Return one captured counter's value, or ``None`` when it went unobserved."""
    counter = runtime.counters[name]
    return counter.value if isinstance(counter, Observed) else None


def _captured_spend(runtime: MeasuredRuntime) -> RunSpend:
    """Return the spend a measured capture states."""
    cost = _observed(runtime, CounterName.COST_USD)
    cache = [_observed(runtime, name) for name in _CACHE_COUNTERS]
    known_cache = [value for value in cache if value is not None]
    return RunSpend(
        cost_microusd=None if cost is None else int(cost * _MICROUSD_PER_USD),
        input_tokens=_whole(_observed(runtime, CounterName.INPUT_TOKENS)),
        output_tokens=_whole(_observed(runtime, CounterName.OUTPUT_TOKENS)),
        cache_tokens=int(sum(known_cache)) if known_cache else None,
    )


def _whole(value: Decimal | None) -> int | None:
    """Return a captured count as an integer, keeping ``None``."""
    return None if value is None else int(value)


def run_spend(reading: RunReading) -> RunSpend:
    """Return what one Run spent.

    Args:
        reading: The Run and its event lines.

    Returns:
        The measured capture's figures when the Run has one; otherwise its stream's
        usage readings folded so a running total is not summed twice; nothing when
        the capture excluded the Run or no reading was filed.
    """
    captured = reading.run.captured_runtime
    if isinstance(captured, MeasuredRuntime):
        return _captured_spend(captured)
    if isinstance(captured, ExcludedRuntime):
        return RunSpend()
    readings = [
        event.payload
        for event in reading.events
        if event.quarantine is None and isinstance(event.payload, UsagePayload)
    ]
    if not readings:
        return RunSpend()
    totals = aggregate_usage(readings)
    return RunSpend(
        cost_microusd=totals.cost_microusd,
        input_tokens=totals.input_tokens,
        output_tokens=totals.output_tokens,
        cache_tokens=totals.cache_tokens,
    )


def _sum(values: Iterable[int | None]) -> int | None:
    """Return the sum of the reported values, or ``None`` when none was reported."""
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def scope_totals(
    readings: Sequence[RunReading], spends: Sequence[RunSpend]
) -> tuple[TotalsRow, ...]:
    """Return one totals row per project the listed sessions belong to.

    Args:
        readings: The listed sessions.
        spends: One spend per reading, in the same order.

    Returns:
        The rows, in project code order.
    """
    grouped: dict[str, list[tuple[RunReading, RunSpend]]] = {}
    for reading, spend in zip(readings, spends, strict=True):
        code = project_code_of(reading.run.urn.project_key)
        grouped.setdefault(code, []).append((reading, spend))
    rows = []
    for code, members in sorted(grouped.items()):
        priced = sum(1 for _reading, spend in members if spend.cost_microusd is not None)
        rows.append(
            TotalsRow(
                scope=f"project:{code}",
                cost_microusd=_sum(spend.cost_microusd for _reading, spend in members),
                input_tokens=_sum(spend.input_tokens for _reading, spend in members),
                output_tokens=_sum(spend.output_tokens for _reading, spend in members),
                cache_tokens=_sum(spend.cache_tokens for _reading, spend in members),
                priced_runs=priced,
                unpriced_runs=len(members) - priced,
                quotable=all(reading.own_project for reading, _spend in members),
            )
        )
    logger.debug(f"scope_totals sessions={len(readings)} rows={len(rows)}")
    return tuple(rows)


__all__ = ["RunSpend", "TotalsRow", "run_spend", "scope_totals"]
