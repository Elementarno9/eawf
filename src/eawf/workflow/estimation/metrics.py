"""Estimation metrics for the ``eawf metrics`` CLI and the telemetry projection.

The CLI's bare view and its ``variance`` sub-verb read the selected
generation's estimate and actual ledgers through
:func:`compute_ledger_metrics`: how many estimates and actuals the
generation holds, and the M26 estimate-actual variance over the subjects
that carry both. The functions are **pure**: they take ledger records and
return typed models without touching disk, locks or events.

The wave roll-ups (:func:`compute_wave_elapsed`, :func:`compute_weekly_burn`)
are kept for the telemetry projection, which hands them its own scoped
snapshot.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.migration.v1_state import State
from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import ActualSummary, EstimateSummary
from eawf.kernel.store.ledger import LedgerRecord

#: Schema version of the ``eawf metrics`` JSON envelope. Version 2 is the
#: ledger roll-up; version 1 was the retired wave table.
METRICS_SCHEMA_VERSION: Literal[2] = 2

#: The estimate-ledger statuses a re-fit files a mapping revision under.
_MAPPING_STATUSES: Final = frozenset({"applied", "proposed"})


class WaveElapsedMetric(BaseModel):
    """Wall-clock elapsed-minutes roll-up across CLOSED waves.

    Only CLOSED waves with both ``opened_at`` and ``closed_at`` set
    contribute. The roll-up reports minutes (not EU) because this
    metric measures lifecycle latency, not effort. When
    ``sample_count == 0`` every aggregate is ``0.0``.
    """

    model_config = ConfigDict(extra="forbid")

    sample_count: int = Field(ge=0)
    mean_minutes: float = Field(ge=0.0)
    median_minutes: float = Field(ge=0.0)
    max_minutes: float = Field(ge=0.0)


#: Rolling window width for :func:`compute_weekly_burn`. Seven days lines up
#: with the operator-set ``Project.weekly_eu_target`` cadence; bumping the
#: window would mean re-cadencing the target value too, so keep it pinned.
WEEKLY_BURN_WINDOW: timedelta = timedelta(days=7)


class EstimateActualVarianceMetric(BaseModel):
    """M26 ``eawf_estimate_actual_variance_pct`` roll-up.

    The C09 §5.9.6 M26 gauge: ``(actual EU - planned EU) / planned EU *
    100`` aggregated over CLOSED waves that carry both an estimate and an
    actual. ``planned_eu`` sums :class:`EstimateSummary.expected_eu`,
    ``actual_eu`` sums :class:`ActualSummary.elapsed_eu` over the same
    contributing waves.

    ``variance_pct`` is ``None`` when ``sample_count == 0`` or when the
    planned-EU denominator is zero — the VarianceTile and the ship-gate
    Variance section surface "no data" rather than a fabricated ``0%``
    or a divide-by-zero.

    ``mapping_revisions`` labels the effort-unit mapping revisions the
    contributing estimates were made under, ``"unrecorded"`` for an estimate
    that recorded none, so an aggregate spanning revisions says so.
    """

    model_config = ConfigDict(extra="forbid")

    sample_count: int = Field(ge=0)
    planned_eu: float = Field(ge=0.0)
    actual_eu: float = Field(ge=0.0)
    variance_pct: float | None
    mapping_revisions: list[str] = Field(default_factory=list)


class WeeklyBurnMetric(BaseModel):
    """Rolling-7-day EU consumption rollup against ``Project.weekly_eu_target``.

    ``consumed_eu`` sums :class:`~eawf.kernel.state.models.ActualSummary.elapsed_eu`
    across actuals whose ``updated_at`` falls inside the trailing
    :data:`WEEKLY_BURN_WINDOW`. ``target_eu`` mirrors
    ``state.project.weekly_eu_target`` (``None`` when the field is unset, in
    which case the TUI footer renders no burn line at all).

    The TUI consumes the rollup directly via :func:`compute_weekly_burn`.
    """

    model_config = ConfigDict(extra="forbid")

    consumed_eu: float = Field(ge=0.0)
    target_eu: float | None
    window_days: int = Field(gt=0)


class LedgerMetrics(BaseModel):
    """The ``eawf metrics`` payload: a generation's estimate and actual ledgers.

    Attributes:
        schema_version: The envelope version, :data:`METRICS_SCHEMA_VERSION`.
        estimate_count: The estimates the ledger holds, one per subject.
        actual_count: The actuals the ledger holds, one per subject.
        unreadable_count: Lines that did not read as their kind.
        variance: The M26 variance over subjects carrying both.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = METRICS_SCHEMA_VERSION
    estimate_count: int = Field(ge=0)
    actual_count: int = Field(ge=0)
    unreadable_count: int = Field(ge=0)
    variance: EstimateActualVarianceMetric


def _median(values: list[float]) -> float:
    """Return the median of *values* (sorted-copy O(n log n))."""
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def compute_wave_elapsed(state: State) -> WaveElapsedMetric:
    """Compute the wall-clock elapsed-minutes roll-up across CLOSED waves.

    A wave contributes when it has ``status == CLOSED`` and both
    ``opened_at`` and ``closed_at`` populated. Waves whose ``closed_at``
    is earlier than ``opened_at`` (clock-skew anomaly) are skipped.
    """
    minutes: list[float] = []
    for wave in state.waves.values():
        if wave.status != WaveStatus.CLOSED:
            continue
        if wave.opened_at is None or wave.closed_at is None:
            continue
        delta = wave.closed_at - wave.opened_at
        seconds = delta.total_seconds()
        if seconds < 0:
            continue
        minutes.append(seconds / 60.0)
    if not minutes:
        return WaveElapsedMetric(
            sample_count=0,
            mean_minutes=0.0,
            median_minutes=0.0,
            max_minutes=0.0,
        )
    mean = sum(minutes) / len(minutes)
    return WaveElapsedMetric(
        sample_count=len(minutes),
        mean_minutes=mean,
        median_minutes=_median(minutes),
        max_minutes=max(minutes),
    )


def compute_weekly_burn(state: State, *, now: datetime | None = None) -> WeeklyBurnMetric:
    """Sum trailing-7-day actual-EU consumption versus the project target.

    The rollup denominator is the rolling :data:`WEEKLY_BURN_WINDOW`; the
    numerator is the sum of every :class:`ActualSummary.elapsed_eu` whose
    ``updated_at`` falls inside ``[now - window, now]``. When
    ``state.project`` is ``None`` or the operator has not set
    ``weekly_eu_target``, ``target_eu`` is ``None`` and the TUI footer
    short-circuits to "no burn line".

    A ``calibration_excluded`` actual still counts here, unlike in the
    bucket re-fit and the estimate-quality metrics. Burn measures SPEND,
    and an excluded row is real spend — the flag disqualifies the figure
    as a reference class for estimating future work, not as a record of
    work already done.

    Args:
        state: Loaded typed :class:`State` snapshot.
        now: Optional clock injection for deterministic tests. Defaults to
            :func:`datetime.now` (UTC).
    """
    anchor = now if now is not None else datetime.now(UTC)
    window_start = anchor - WEEKLY_BURN_WINDOW
    actuals = state.actuals or {}
    consumed = 0.0
    for actual in actuals.values():
        if window_start <= actual.updated_at <= anchor:
            consumed += actual.elapsed_eu
    target: float | None = None
    if state.project is not None:
        target = state.project.weekly_eu_target
    return WeeklyBurnMetric(
        consumed_eu=consumed,
        target_eu=target,
        window_days=WEEKLY_BURN_WINDOW.days,
    )


def compute_ledger_metrics(
    estimates: Sequence[LedgerRecord], actuals: Sequence[LedgerRecord]
) -> LedgerMetrics:
    """Roll up a generation's estimate and actual ledgers.

    A line carries its estimate or actual under ``payload`` when the cutover
    imported it, and is the statement itself otherwise. A mapping revision
    filed on the estimate ledger is no estimate, and a line that does not
    read as its kind is counted as unreadable rather than guessed at.

    A subject contributes to the variance when it carries both an estimate
    and an actual that is not ``calibration_excluded``: an excluded figure
    is a floor or a split, so folding it in would report a measurement
    artefact as an over- or under-run. The variance is the aggregate
    ``(sum actual EU - sum planned EU) / sum planned EU * 100``, so one large
    over-run is not diluted by many small subjects.

    Args:
        estimates: The estimate ledger's lines, in file order.
        actuals: The actual ledger's lines, in file order.

    Returns:
        The counts and the variance; ``variance_pct`` is ``None`` when no
        subject contributes or the planned EU sums to zero.
    """
    planned: dict[str, EstimateSummary] = {}
    unreadable = 0
    for record in estimates:
        if record.status in _MAPPING_STATUSES:
            continue
        try:
            planned[record.record_key] = EstimateSummary.model_validate(
                record.payload.get("payload", record.payload)
            )
        except ValidationError:
            unreadable += 1
    measured: dict[str, ActualSummary] = {}
    for record in actuals:
        try:
            measured[record.record_key] = ActualSummary.model_validate(
                record.payload.get("payload", record.payload)
            )
        except ValidationError:
            unreadable += 1
    planned_eu = 0.0
    actual_eu = 0.0
    sample_count = 0
    revisions: set[str] = set()
    for subject, estimate in planned.items():
        actual = measured.get(subject)
        if actual is None or actual.calibration_excluded:
            continue
        planned_eu += estimate.expected_eu
        actual_eu += actual.elapsed_eu
        sample_count += 1
        revision = estimate.mapping_revision
        revisions.add("unrecorded" if revision is None else str(revision))
    variance_pct = (actual_eu - planned_eu) / planned_eu * 100.0 if planned_eu > 0 else None
    return LedgerMetrics(
        estimate_count=len(planned),
        actual_count=len(measured),
        unreadable_count=unreadable,
        variance=EstimateActualVarianceMetric(
            sample_count=sample_count,
            planned_eu=planned_eu,
            actual_eu=actual_eu,
            variance_pct=variance_pct,
            mapping_revisions=sorted(revisions),
        ),
    )


__all__ = [
    "METRICS_SCHEMA_VERSION",
    "WEEKLY_BURN_WINDOW",
    "EstimateActualVarianceMetric",
    "LedgerMetrics",
    "WaveElapsedMetric",
    "WeeklyBurnMetric",
    "compute_ledger_metrics",
    "compute_wave_elapsed",
    "compute_weekly_burn",
]
