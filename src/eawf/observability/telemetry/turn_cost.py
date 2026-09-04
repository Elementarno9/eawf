"""Turn-cost record plus the completed-unit producer that builds it.

The unit of measurement is a **completed unit of work**: one closed wave
joined to every run attributed to it. It is deliberately not a run and not
a command. A run is an implementation detail of how the work was dispatched
(one wave may be retried, split across runtimes, or resumed), so a per-run
percentile measures the dispatcher rather than the work. Splitting a wave
into more runs must therefore not move the number at all, which is the
invariant :func:`build_turn_cost_record` is written to hold: the same wave
expressed as one run or as three summing runs produces a byte-identical
record.

Three exclusion rules apply to the runs inside a unit, in this order:

1. A run whose role attributes to the ``UNATTRIBUTED`` member of
   :class:`~eawf.observability.telemetry.cost_class.CostClass` is counted and
   dropped from both cost sums. A run with no role at all raises instead
   of being charged to execution.
2. A run on a runtime whose reasoning-token accounting is unsettled is
   counted and dropped. The runtime folds reasoning tokens into its
   reported output total, so its rows cannot be compared against runtimes
   that report the two separately until a vendor rollout settles it.
3. A run with no ``price_source`` is counted as unpriced and dropped. It is
   never summed as zero, because a zero-cost row and an unpriced row are
   different facts and averaging them together understates real spend.

Wall clock is summed over *every* run in the unit regardless of those
rules: elapsed time is neither priced nor affected by token accounting, so
excluding a run from the cost sums must not shorten the unit's clock.

Percentiles use the nearest-rank method, which returns an observed value
rather than an interpolated one. That keeps ``Decimal`` costs exact (no
float midpoint drift) and keeps the record reproducible across platforms.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import AgentSessionRole, WaveStatus
from eawf.kernel.state.models import Wave
from eawf.observability.telemetry.cost_class import CostClass, classify_cost_class
from eawf.observability.telemetry.models import RuntimeName

__all__ = [
    "REASONING_UNSETTLED_RUNTIMES",
    "CompletedUnitRun",
    "PriceSource",
    "TurnCostRecord",
    "build_turn_cost_record",
]


REASONING_UNSETTLED_RUNTIMES: frozenset[str] = frozenset({"codex"})
"""Runtimes whose reported output tokens already absorb reasoning tokens.

Rows from these runtimes are flagged and excluded from the cost and token
sums; the counts still surface on the record so the exclusion is visible
rather than silent.
"""


class PriceSource(BaseModel):
    """Provenance of the price applied to one run's cost.

    A cost is summable only when it carries a source. The model is a row
    rather than a bare string so a future source can add its own fields
    without re-typing every consumer.

    Attributes:
        kind: Where the rate came from (a vendor-reported charge or a
            local pricing snapshot).
        pricing_version: Version stamp of the rate table that priced the
            run, or ``None`` when the vendor reported the charge directly.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1)
    pricing_version: str | None = None


class CompletedUnitRun(BaseModel):
    """One run joined to a completed unit of work.

    Attributes:
        run_id: Stable id of the run.
        wave_id: Wave the run is attributed to.
        role: Agent role the run was dispatched under. ``None`` is an
            unlabelled run and makes the producer raise.
        runtime: Runtime that executed the run.
        model: Model id the run billed against.
        wall_clock_ms: Elapsed wall clock of the run.
        input_tokens: Non-cached input tokens billed.
        output_tokens: Output tokens billed.
        cache_read_tokens: Tokens served from the prompt cache.
        cache_write_tokens: Tokens written to the prompt cache.
        reasoning_tokens: Reasoning tokens reported separately. Never a
            summand of the token total.
        cost_usd: Priced cost of the run.
        price_source: Price provenance, or ``None`` for an unpriced run.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    wave_id: str = Field(min_length=1)
    role: AgentSessionRole | None = None
    runtime: RuntimeName
    model: str = Field(min_length=1)
    wall_clock_ms: int = Field(ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    cost_usd: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    price_source: PriceSource | None = None


class TurnCostRecord(BaseModel):
    """Wall clock and cost per completed unit of work for one fixture.

    Verification and execution cost are separate required fields and there
    is deliberately no combined total: ``extra="forbid"`` makes an attempt
    to carry one raise, so a consumer that wants the sum has to add the two
    explicitly and can never mistake a partial figure for the whole.

    Attributes:
        fixture_id: Id of the fixture the record was produced from.
        harness_revision: Revision of the harness that produced it.
        runtime: Runtime the measurement is declared against.
        model: Model id the measurement is declared against.
        unit_count: Number of completed units in the record.
        p50_wall_clock_ms: Median per-unit wall clock (nearest rank).
        p90_wall_clock_ms: 90th-percentile per-unit wall clock.
        p50_cost_usd: Median per-unit summable cost.
        p90_cost_usd: 90th-percentile per-unit summable cost.
        verification_cost_usd: Total cost of verification-class runs.
        execution_cost_usd: Total cost of execution-class runs.
        token_total: Sum of input, output, cache-read and cache-write
            tokens over summed runs. Reasoning tokens are never included.
        unattributed_run_count: Runs excluded as unattributed.
        unpriced_run_count: Runs excluded for carrying no price source.
        reasoning_summand_unsettled: Whether any run was excluded for
            unsettled reasoning-token accounting.
        reasoning_summand_unsettled_run_count: How many runs were.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_id: str = Field(min_length=1)
    harness_revision: str = Field(min_length=1)
    runtime: RuntimeName
    model: str = Field(min_length=1)
    unit_count: int = Field(ge=1)
    p50_wall_clock_ms: int = Field(ge=0)
    p90_wall_clock_ms: int = Field(ge=0)
    p50_cost_usd: Decimal = Field(ge=Decimal("0"))
    p90_cost_usd: Decimal = Field(ge=Decimal("0"))
    verification_cost_usd: Decimal = Field(ge=Decimal("0"))
    execution_cost_usd: Decimal = Field(ge=Decimal("0"))
    token_total: int = Field(ge=0)
    unattributed_run_count: int = Field(ge=0)
    unpriced_run_count: int = Field(ge=0)
    reasoning_summand_unsettled: bool
    reasoning_summand_unsettled_run_count: int = Field(ge=0)


@dataclass(slots=True)
class _CostFold:
    """Mutable accumulator threaded through the per-unit fold."""

    verification_cost_usd: Decimal = Decimal("0")
    execution_cost_usd: Decimal = Decimal("0")
    token_total: int = 0
    unattributed_run_count: int = 0
    unpriced_run_count: int = 0
    unsettled_run_count: int = 0
    unit_wall_clock_ms: list[int] = field(default_factory=list)
    unit_cost_usd: list[Decimal] = field(default_factory=list)


def build_turn_cost_record(
    *,
    waves: Sequence[Wave],
    runs: Sequence[CompletedUnitRun],
    fixture_id: str,
    harness_revision: str,
    runtime: RuntimeName,
    model: str,
) -> TurnCostRecord:
    """Return the turn-cost record for the closed waves in *waves*.

    Args:
        waves: Candidate waves. Only ``CLOSED`` waves become units.
        runs: Runs to join onto those waves by ``wave_id``.
        fixture_id: Id of the fixture being measured.
        harness_revision: Revision of the producing harness.
        runtime: Runtime the measurement is declared against.
        model: Model id the measurement is declared against.

    Returns:
        A :class:`TurnCostRecord` whose percentiles are taken over the
        per-unit aggregates, not over individual runs.

    Raises:
        KeyError: When a run names a wave absent from *waves*.
        ValueError: When no closed wave carries at least one run, or when
            a joined run carries no role.
    """
    units = _join_completed_units(waves=waves, runs=runs)
    if not units:
        raise ValueError(
            f"no completed unit of work for fixture {fixture_id!r}: "
            "a unit is a closed wave with at least one attributed run"
        )
    fold = _CostFold()
    for unit_runs in units:
        _fold_unit(fold, unit_runs)
    return TurnCostRecord(
        fixture_id=fixture_id,
        harness_revision=harness_revision,
        runtime=runtime,
        model=model,
        unit_count=len(units),
        p50_wall_clock_ms=_nearest_rank(fold.unit_wall_clock_ms, numerator=1, denominator=2),
        p90_wall_clock_ms=_nearest_rank(fold.unit_wall_clock_ms, numerator=9, denominator=10),
        p50_cost_usd=_nearest_rank(fold.unit_cost_usd, numerator=1, denominator=2),
        p90_cost_usd=_nearest_rank(fold.unit_cost_usd, numerator=9, denominator=10),
        verification_cost_usd=fold.verification_cost_usd,
        execution_cost_usd=fold.execution_cost_usd,
        token_total=fold.token_total,
        unattributed_run_count=fold.unattributed_run_count,
        unpriced_run_count=fold.unpriced_run_count,
        reasoning_summand_unsettled=fold.unsettled_run_count > 0,
        reasoning_summand_unsettled_run_count=fold.unsettled_run_count,
    )


def _join_completed_units(
    *,
    waves: Sequence[Wave],
    runs: Sequence[CompletedUnitRun],
) -> list[list[CompletedUnitRun]]:
    """Group *runs* into one bucket per closed wave, ordered by wave id.

    Args:
        waves: Candidate waves keyed by id.
        runs: Runs to attribute.

    Returns:
        One run list per closed wave that has at least one run, ordered by
        wave id so the fold is deterministic.

    Raises:
        KeyError: When a run names a wave absent from *waves*.
    """
    by_id = {wave.id: wave for wave in waves}
    grouped: dict[str, list[CompletedUnitRun]] = {}
    for run in runs:
        wave = by_id.get(run.wave_id)
        if wave is None:
            raise KeyError(f"run {run.run_id!r} references unknown wave {run.wave_id!r}")
        if wave.status is not WaveStatus.CLOSED:
            continue
        grouped.setdefault(run.wave_id, []).append(run)
    return [grouped[wave_id] for wave_id in sorted(grouped)]


def _fold_unit(fold: _CostFold, unit_runs: Sequence[CompletedUnitRun]) -> None:
    """Fold one completed unit's runs into *fold*."""
    unit_wall_clock_ms = 0
    unit_cost_usd = Decimal("0")
    for run in unit_runs:
        unit_wall_clock_ms += run.wall_clock_ms
        unit_cost_usd += _fold_run(fold, run)
    fold.unit_wall_clock_ms.append(unit_wall_clock_ms)
    fold.unit_cost_usd.append(unit_cost_usd)


def _fold_run(fold: _CostFold, run: CompletedUnitRun) -> Decimal:
    """Return *run*'s summable cost, updating the exclusion counters.

    Args:
        fold: Accumulator to update in place.
        run: The run being folded.

    Returns:
        The run's cost when it is summable, else ``Decimal("0")``.

    Raises:
        ValueError: When the run carries no role.
    """
    cost_class = classify_cost_class(run.role)
    if cost_class is CostClass.UNATTRIBUTED:
        fold.unattributed_run_count += 1
        return Decimal("0")
    if run.runtime in REASONING_UNSETTLED_RUNTIMES:
        fold.unsettled_run_count += 1
        return Decimal("0")
    if run.price_source is None:
        fold.unpriced_run_count += 1
        return Decimal("0")
    fold.token_total += _four_class_tokens(run)
    if cost_class is CostClass.VERIFICATION:
        fold.verification_cost_usd += run.cost_usd
    else:
        fold.execution_cost_usd += run.cost_usd
    return run.cost_usd


def _four_class_tokens(run: CompletedUnitRun) -> int:
    """Return the four-class token total for *run*.

    Reasoning tokens are excluded by construction: on the runtimes that
    report them separately they are already inside ``output_tokens``, so
    adding them again would double-count the same work.
    """
    return run.input_tokens + run.output_tokens + run.cache_read_tokens + run.cache_write_tokens


def _nearest_rank[T: (int, Decimal)](
    values: Sequence[T],
    *,
    numerator: int,
    denominator: int,
) -> T:
    """Return the nearest-rank percentile of *values*.

    Args:
        values: Per-unit aggregates. Must be non-empty.
        numerator: Quantile numerator (``1`` of ``2`` for p50).
        denominator: Quantile denominator.

    Returns:
        The observed value at rank ``ceil(n * numerator / denominator)``.

    Raises:
        ValueError: When *values* is empty.
    """
    if not values:
        raise ValueError("nearest-rank percentile needs at least one value")
    ordered = sorted(values)
    rank = -(-len(ordered) * numerator // denominator)
    return ordered[max(rank, 1) - 1]
