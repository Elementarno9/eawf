"""The usage and budget payloads a Run event carries, and how usage adds up.

A usage reading is either a running session total or the delta since the
previous reading, and which one depends on what the source reports -- a
per-provider fact, so the payload states it in ``is_cumulative`` rather
than leaving a reader to infer it from the token class. Summing running
totals inflates every figure built on them, so :func:`aggregate_usage`
is the one fold: deltas are summed and running totals contribute their
maximum.

A cost names where its price came from: ``billed`` when the runtime
reported the charge, ``list-reconstructed`` when eawf multiplied tokens by a
published rate table. Only a billed price may enforce anything, so the
enforcement fold refuses a reconstructed one rather than letting a list
price trip -- or fail to trip -- a budget cap.

A budget event reports that a ceiling was approached or reached, on one
axis, against one compiled contract and one policy revision. Two things
it can never say are refused at the boundary: that a prediction was
exhausted, and that a reading of unavailable quality crossed anything.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Annotated, Final, Literal, Self

from pydantic import Field, StrictBool, StrictFloat, StrictInt, model_validator

from eawf.kernel.runtime.provider import Digest, RuntimeRecord
from eawf.kernel.state.enums import QualityLadder
from eawf.kernel.state.types import UtcDatetime

#: Where a usage reading came from.
UsageSource = Literal["provider_receipt", "provider_transcript", "counter_sidecar", "eawf_derived"]

#: How much a reading can be trusted, from a direct measurement down to none.
UsageQuality = QualityLadder

#: Where a reading's cost came from. An unpriced reading carries no cost
#: and so no source.
UsagePriceSource = Literal["billed", "list-reconstructed"]

#: A resource dimension a Run's compiled ceiling bounds.
BudgetAxis = Literal[
    "wall_seconds", "input_tokens", "output_bytes", "tool_calls", "child_runs", "cost_microusd"
]

#: The unit each axis is counted in; an axis reported in another unit is
#: two numbers that cannot be compared.
AXIS_UNITS: Final[Mapping[str, str]] = {
    "wall_seconds": "seconds",
    "input_tokens": "tokens",
    "output_bytes": "bytes",
    "tool_calls": "calls",
    "child_runs": "runs",
    "cost_microusd": "microusd",
}

#: The token counters a usage reading may carry.
_TOKEN_CLASSES: Final = ("input_tokens", "output_tokens", "cache_tokens")
_COUNTERS: Final = (*_TOKEN_CLASSES, "cost_microusd")

#: How far a stated fraction may sit from the one its values derive.
_FRACTION_TOLERANCE: Final = 1e-9

_Count = Annotated[StrictInt, Field(ge=0)]


class UsagePayload(RuntimeRecord):
    """One usage reading, stating its source and whether it is a running total.

    Attributes:
        payload_kind: The payload discriminator.
        input_tokens: Input tokens, or ``None`` when the source reported none.
        output_tokens: Output tokens, or ``None`` when unreported.
        cache_tokens: Prompt-cache tokens, or ``None`` when unreported.
        cost_microusd: Cost, or ``None`` when unpriced -- never zero for
            unpriced.
        price_source: Where the cost came from; present exactly when a
            cost is.
        usage_source: Where the reading came from.
        is_cumulative: ``True`` when the counters are running session
            totals, ``False`` when they are the delta since the previous
            reading. Set by the producer from what the source reports.
        measurement_quality: How far the reading can be trusted.
        coverage_fraction: How much of its subject an estimated reading
            covers.
        budget_remaining: What the source says is left, when it says.
    """

    payload_kind: Literal["usage"] = "usage"
    input_tokens: _Count | None = None
    output_tokens: _Count | None = None
    cache_tokens: _Count | None = None
    cost_microusd: _Count | None = None
    price_source: UsagePriceSource | None = None
    usage_source: UsageSource
    is_cumulative: StrictBool
    measurement_quality: UsageQuality
    coverage_fraction: Annotated[StrictFloat, Field(ge=0.0, le=1.0)] | None = None
    budget_remaining: _Count | None = None

    @model_validator(mode="after")
    def _quality_matches_what_was_measured(self) -> Self:
        """Bind the counters and the coverage to the stated quality.

        Raises:
            ValueError: A reading of available quality carries no counter,
                an unavailable one carries some, coverage is stated
                exactly where the quality is not ``estimated``, or a cost
                and its price source are not stated together.
        """
        if (self.cost_microusd is None) != (self.price_source is None):
            raise ValueError("a cost names its price_source, and only a cost does")
        present = [name for name in _COUNTERS if getattr(self, name) is not None]
        unavailable = self.measurement_quality == "unavailable"
        if unavailable and present:
            raise ValueError(f"an unavailable reading carries no counters, not {present}")
        if not unavailable and not present:
            raise ValueError(f"a {self.measurement_quality} reading carries at least one counter")
        estimated = self.measurement_quality == "estimated"
        if estimated != (self.coverage_fraction is not None):
            raise ValueError("coverage_fraction is stated exactly on an estimated reading")
        return self


class UsageTotals(RuntimeRecord):
    """What a sequence of usage readings adds up to, per counter.

    A counter no reading reported stays ``None``: absence is not zero.
    """

    input_tokens: _Count | None = None
    output_tokens: _Count | None = None
    cache_tokens: _Count | None = None
    cost_microusd: _Count | None = None

    @property
    def tokens(self) -> int | None:
        """The token classes summed, or ``None`` when none was reported."""
        counted = [getattr(self, name) for name in _TOKEN_CLASSES]
        reported = [value for value in counted if value is not None]
        return sum(reported) if reported else None


def aggregate_usage(
    payloads: Iterable[UsagePayload], *, for_enforcement: bool = False
) -> UsageTotals:
    """Fold usage readings into totals without double counting a running total.

    Per counter, the deltas are summed and the running totals contribute
    their maximum, because each running total already contains every
    reading before it.

    Args:
        payloads: The readings, in any order.
        for_enforcement: Whether the totals feed a cap or a ceiling. Such a
            fold refuses every ``list-reconstructed`` cost, so the cost it
            reports is billed spend only, or ``None`` when none was billed.

    Returns:
        The totals, with ``None`` for a counter no reading reported.
    """
    readings = tuple(payloads)
    totals: dict[str, int | None] = {}
    for name in _COUNTERS:
        counted = [
            reading
            for reading in readings
            if not (
                for_enforcement
                and name == "cost_microusd"
                and reading.price_source == "list-reconstructed"
            )
        ]
        deltas = [
            value
            for reading in counted
            if not reading.is_cumulative and (value := getattr(reading, name)) is not None
        ]
        running = [
            value
            for reading in counted
            if reading.is_cumulative and (value := getattr(reading, name)) is not None
        ]
        reported = bool(deltas) or bool(running)
        totals[name] = sum(deltas) + max(running, default=0) if reported else None
    return UsageTotals.model_validate(totals)


class BudgetPayload(RuntimeRecord):
    """One crossing of a Run's compiled ceiling on one axis.

    Attributes:
        payload_kind: The payload discriminator.
        phase: ``warning`` or ``exhausted``, pinned by the event kind.
        axis: The resource dimension that crossed.
        basis: ``estimate`` for a prediction, ``hard_limit`` for a
            compiled ceiling.
        band: The threshold band reached.
        contract_digest: The compiled contract the ceiling came from.
        policy_digest: The budget policy the threshold came from.
        policy_revision: That policy's revision.
        ceiling_value: The ceiling crossed.
        observed_value: The consumption that crossed it.
        unit: The unit both values are counted in, fixed by the axis.
        fraction: ``observed_value / ceiling_value``.
        measurement_quality: How far ``observed_value`` can be trusted.
        last_progress_at: The Run's last observed progress, if any.
        last_heartbeat_at: The Run's last heartbeat, if any.
        notice_key: The stable key of the one notice the crossing upserts.
    """

    payload_kind: Literal["budget"] = "budget"
    phase: Literal["warning", "exhausted"]
    axis: BudgetAxis
    basis: Literal["estimate", "hard_limit"]
    band: Literal["approaching", "limit_reached"]
    contract_digest: Digest
    policy_digest: Digest
    policy_revision: Annotated[StrictInt, Field(ge=1)]
    ceiling_value: Annotated[StrictInt, Field(ge=1)]
    observed_value: _Count
    unit: Literal["seconds", "tokens", "bytes", "calls", "runs", "microusd"]
    fraction: Annotated[StrictFloat, Field(ge=0.0)]
    measurement_quality: UsageQuality
    last_progress_at: UtcDatetime | None = None
    last_heartbeat_at: UtcDatetime | None = None
    notice_key: Digest

    @model_validator(mode="after")
    def _crossing_is_one_that_can_happen(self) -> Self:
        """Refuse a crossing the values, the basis or the quality rule out.

        Raises:
            ValueError: A prediction is reported exhausted, an unavailable
                reading crosses a threshold, the unit is not the axis's,
                the fraction is not the one the values derive, or the band
                disagrees with the phase or the values.
        """
        if self.phase == "exhausted" and self.basis == "estimate":
            raise ValueError("an estimate cannot be exhausted; only a hard limit can")
        if self.measurement_quality == "unavailable":
            raise ValueError("a reading of unavailable quality cannot cross a threshold")
        if self.unit != AXIS_UNITS[self.axis]:
            raise ValueError(f"axis {self.axis} is counted in {AXIS_UNITS[self.axis]}")
        derived = self.observed_value / self.ceiling_value
        if abs(self.fraction - derived) > _FRACTION_TOLERANCE:
            raise ValueError(f"fraction {self.fraction} is not observed/ceiling = {derived}")
        reached = self.observed_value >= self.ceiling_value
        if (self.phase == "exhausted") != (self.band == "limit_reached"):
            raise ValueError(f"a {self.phase} event is not in the {self.band} band")
        if self.phase == "exhausted" and not reached:
            raise ValueError("an exhausted event reports a value at or over its ceiling")
        return self


__all__ = [
    "AXIS_UNITS",
    "BudgetAxis",
    "BudgetPayload",
    "UsagePayload",
    "UsagePriceSource",
    "UsageQuality",
    "UsageSource",
    "UsageTotals",
    "aggregate_usage",
]
