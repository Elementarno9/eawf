"""Wave-level workflow metrics for the ``eawf metrics`` CLI.

This module is **pure** — it consumes a typed :class:`~eawf.kernel.state.models.State`
and returns deterministic :class:`MetricsSummary` records without touching
disk, locks, or events. The CLI handler in :mod:`eawf.surfaces.cli.commands.metrics`
runs this once, then hands the result to the shared renderer in
:mod:`eawf.surfaces.render.metrics_view` (which feeds the CLI table, the future TUI
overlay, and release-notes inserts).

The four metrics, in order:

1. **EU variance** — for every CLOSED wave that has both an
   :class:`~eawf.kernel.state.models.EstimateSummary` and an
   :class:`~eawf.kernel.state.models.ActualSummary`, compute
   ``elapsed_eu - expected_eu`` and roll up count / mean / stdev / inside-
   pessimistic share. The "inside pessimistic" share is the fraction of
   samples whose actual EU did not exceed the pessimistic estimate (the
   standard reference-class calibration metric).
2. **Audit pass rate** — fraction of audits whose
   :class:`~eawf.kernel.state.enums.AuditVerdict` is :data:`AuditVerdict.PASS`
   across audits with a verdict set. Audits with no verdict yet (e.g.
   PENDING/RUNNING with ``verdict=None``) are excluded from the
   denominator.
3. **Wave elapsed** — for every CLOSED wave with both ``opened_at`` and
   ``closed_at`` populated, compute the wall-clock elapsed minutes and
   roll up count / mean / median / max.
4. **Planned vs reactive split** — classifies each wave by *why its iter
   was opened* (the typed ``Iter.trigger``): a ``reactive`` iter (repair
   cycle or mid-flight scope add) feeds the reactive numerator, a
   ``proactive`` iter (planned-scope delivery, incl. a deliberate scope
   expansion) feeds the planned count, and a ``none`` iter (pure
   bookkeeping) drops out of the denominator entirely. The earlier
   id-suffix heuristic (``I01`` planned, ``I02+`` reactive) over-counted
   reactive work — a planned scope expansion also opens an ``I02+`` iter —
   producing the inflated prior reactive-share figure; consulting
   ``trigger`` corrects that definitional artifact. When a wave's iter row
   is absent from the snapshot the split falls back to the id-suffix
   heuristic so a state that predates the ``trigger`` field still reports.

The output type is a Pydantic ``BaseModel`` so the CLI's JSON envelope
(``schema_version=1``) is type-checked at the boundary and re-validates
when round-tripped through orjson.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta
from heapq import heappop, heappush
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import AuditVerdict, IterTrigger, WaveStatus
from eawf.kernel.state.ids import natural_key
from eawf.kernel.state.models import Iter, State, Wave
from eawf.workflow.estimation.buckets import (
    critical_path_eu,
    resolve_wave_actual,
    sum_wave_eu,
    wave_estimate_eu,
)

# Schema version for the JSON envelope. Bump only when fields change in a
# wire-breaking way.
METRICS_SCHEMA_VERSION: Literal[1] = 1

# Wave-id grammar (mirrors :data:`eawf.kernel.state.ids.RE_WAVE`). Used to extract
# the iter suffix (``I01`` vs ``I02+``) for the planned/reactive split
# without dragging the ``ids`` module into a pure compute path that
# already has the full wave-id available. ``\d{2,}`` matches the widened
# grammar per AGENTS symbol-conventions so 3-digit ids (P100 / I100 /
# W100) bin correctly once the queue grows past 99.
_WAVE_ITER_RE = re.compile(r"^P\d{2,}-(I\d{2,})-W\d{2,}$")


class EuVarianceMetric(BaseModel):
    """EU variance roll-up across CLOSED waves with estimate + actual.

    ``sample_count`` is the number of waves in the denominator (CLOSED
    AND both estimate AND actual present). When ``sample_count == 0``
    every aggregate is ``0.0`` and ``inside_pessimistic_share`` is
    ``0.0`` — callers should branch on ``sample_count > 0`` before
    displaying the means.
    """

    model_config = ConfigDict(extra="forbid")

    sample_count: int = Field(ge=0)
    mean_delta_eu: float
    stdev_delta_eu: float
    inside_pessimistic_share: float = Field(ge=0.0, le=1.0)


class AuditPassRateMetric(BaseModel):
    """Audit pass-rate across audits with a verdict set.

    Denominator excludes audits whose ``verdict`` is still ``None`` (i.e.
    PENDING or RUNNING audits). When ``decided_count == 0`` the
    ``pass_share`` is ``0.0``.
    """

    model_config = ConfigDict(extra="forbid")

    decided_count: int = Field(ge=0)
    pass_count: int = Field(ge=0)
    minor_count: int = Field(ge=0)
    major_count: int = Field(ge=0)
    pass_share: float = Field(ge=0.0, le=1.0)


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


class PlannedVsReactiveMetric(BaseModel):
    """Split of waves by their iter's :class:`~eawf.kernel.state.enums.IterTrigger`.

    A wave under a ``reactive`` iter (repair / scope-add) counts toward
    :attr:`reactive_count`; a wave under a ``proactive`` iter (planned
    delivery) counts toward :attr:`planned_count`; a wave under a ``none``
    iter is excluded from both — it drops out of the denominator so pure
    bookkeeping iters do not skew the ratio. Waves whose iter row is absent
    fall back to the id-suffix heuristic (``I01`` planned, ``I02+``
    reactive); waves whose id does not match the canonical grammar are
    excluded from both counts. ``reactive_share`` is
    ``reactive_count / (planned_count + reactive_count)`` and is ``0.0``
    when that denominator is empty.
    """

    model_config = ConfigDict(extra="forbid")

    planned_count: int = Field(ge=0)
    reactive_count: int = Field(ge=0)
    reactive_share: float = Field(ge=0.0, le=1.0)


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
    """

    model_config = ConfigDict(extra="forbid")

    sample_count: int = Field(ge=0)
    planned_eu: float = Field(ge=0.0)
    actual_eu: float = Field(ge=0.0)
    variance_pct: float | None


class WeeklyBurnMetric(BaseModel):
    """Rolling-7-day EU consumption rollup against ``Project.weekly_eu_target``.

    ``consumed_eu`` sums :class:`~eawf.kernel.state.models.ActualSummary.elapsed_eu`
    across actuals whose ``updated_at`` falls inside the trailing
    :data:`WEEKLY_BURN_WINDOW`. ``target_eu`` mirrors
    ``state.project.weekly_eu_target`` (``None`` when the field is unset, in
    which case the TUI footer renders no burn line at all).

    The metric is *not* part of :class:`MetricsSummary` — the CLI ``eawf
    metrics`` envelope is wire-frozen at schema_version=1, and adding a
    field would bump that version. The TUI consumes the rollup directly via
    :func:`compute_weekly_burn`.
    """

    model_config = ConfigDict(extra="forbid")

    consumed_eu: float = Field(ge=0.0)
    target_eu: float | None
    window_days: int = Field(gt=0)


class RealisticWallClockMetric(BaseModel):
    """Phase/iter wall-clock projection from DAG, queue, and calibration.

    ``work_sum_eu`` is total serial work. ``critical_path_eu`` is the
    dependency lower bound. ``queue_wall_clock_eu`` is deterministic
    list-scheduling over the DAG with ``max_parallel_waves`` workers.
    ``realistic_wall_clock_eu`` applies a calibration multiplier derived
    from ``inside_pessimistic_share``: a lower share means recent work more
    often exceeded pessimistic estimates, so the wall-clock projection
    expands accordingly.
    """

    model_config = ConfigDict(extra="forbid")

    work_sum_eu: float = Field(ge=0.0)
    critical_path_eu: float = Field(ge=0.0)
    queue_wall_clock_eu: float = Field(ge=0.0)
    inside_pessimistic_share: float | None = Field(default=None, ge=0.0, le=1.0)
    pessimism_multiplier: float = Field(ge=1.0)
    realistic_wall_clock_eu: float = Field(ge=0.0)
    realistic_wall_clock_hours: float = Field(ge=0.0)
    max_parallel_waves: int = Field(ge=1)
    eu_minutes: float = Field(gt=0.0)


class MetricsSummary(BaseModel):
    """Top-level metrics payload for ``eawf metrics``.

    Carries the four wave-level metrics plus the schema-version pin so
    consumers can validate the envelope they receive.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = METRICS_SCHEMA_VERSION
    eu_variance: EuVarianceMetric
    audit_pass_rate: AuditPassRateMetric
    wave_elapsed: WaveElapsedMetric
    planned_vs_reactive: PlannedVsReactiveMetric


def _iter_id_of(wave_id: str) -> str | None:
    """Return the ``I##`` segment of *wave_id* or ``None`` for non-wave ids.

    Helper for the planned/reactive split; pure str slicing keeps the
    metric computable in a single state snapshot.
    """
    match = _WAVE_ITER_RE.match(wave_id)
    if match is None:
        return None
    return match.group(1)


def _classify_wave_by_id(wave_id: str) -> Literal["planned", "reactive", "excluded"]:
    """Fallback split by id suffix when the wave's iter row is absent.

    ``I01`` -> ``"planned"``, ``I02+`` -> ``"reactive"``, an unparseable id
    -> ``"excluded"``. Used only when the snapshot carries no
    :class:`~eawf.kernel.state.models.Iter` row for the wave, so a state
    written before the ``trigger`` field still produces a split.
    """
    iter_segment = _iter_id_of(wave_id)
    if iter_segment is None:
        return "excluded"
    return "planned" if iter_segment == "I01" else "reactive"


def _classify_wave(
    wave: Wave, iters: dict[str, Iter]
) -> Literal["planned", "reactive", "excluded"]:
    """Bucket *wave* for the planned-vs-reactive split via its iter trigger.

    Resolves the wave's iter row (``iters[wave.iter_id]``) and reads its
    :class:`~eawf.kernel.state.enums.IterTrigger`: ``reactive`` ->
    ``"reactive"``, ``proactive`` -> ``"planned"``, ``none`` ->
    ``"excluded"`` (dropped from the denominator). When no iter row exists
    for the wave the classification falls back to the id-suffix heuristic
    (:func:`_classify_wave_by_id`) so a pre-``trigger`` snapshot still
    reports.
    """
    iter_row = iters.get(wave.iter_id)
    if iter_row is None:
        return _classify_wave_by_id(wave.id)
    if iter_row.trigger is IterTrigger.NONE:
        return "excluded"
    if iter_row.trigger is IterTrigger.REACTIVE:
        return "reactive"
    return "planned"


def _stdev_population(values: list[float], mean: float) -> float:
    """Return the population standard deviation of *values* around *mean*.

    Returns ``0.0`` when ``len(values) <= 1`` — a single sample has no
    spread to report and the stdev is conventionally undefined. Using
    population stdev (divide by N) rather than sample stdev (divide by
    N-1) keeps the metric monotone in sample size for the small samples
    a fresh project will have.
    """
    if len(values) <= 1:
        return 0.0
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(var)


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


def _ready_wave_ids(
    *,
    waiting: set[str],
    parents: dict[str, set[str]],
    completed: set[str],
) -> list[str]:
    """Return dependency-ready wave ids in deterministic display order."""
    ready = [wave_id for wave_id in waiting if parents[wave_id] <= completed]
    return sorted(ready, key=natural_key)


def _queue_wall_clock_eu(waves: list[Wave], *, max_parallel_waves: int) -> float:
    """Return DAG queue wall-clock in EU using deterministic list scheduling."""
    if not waves:
        return 0.0
    if max_parallel_waves < 1:
        raise ValueError(f"max_parallel_waves must be >= 1: {max_parallel_waves!r}")

    by_id = {wave.id: wave for wave in waves}
    parents: dict[str, set[str]] = {wave.id: set() for wave in waves}
    for wave in waves:
        for dep in wave.deps:
            if dep not in by_id:
                continue
            parents[wave.id].add(dep)

    waiting = set(by_id)
    completed: set[str] = set()
    running: list[tuple[float, tuple[object, ...], str]] = []
    now_eu = 0.0
    while waiting or running:
        ready = _ready_wave_ids(waiting=waiting, parents=parents, completed=completed)
        while ready and len(running) < max_parallel_waves:
            wave_id = ready.pop(0)
            waiting.remove(wave_id)
            finish_eu = now_eu + wave_estimate_eu(by_id[wave_id])
            heappush(running, (finish_eu, natural_key(wave_id), wave_id))

        if not running:
            # Cyclic graph: critical_path_eu has the cycle guard, so use it
            # as the least surprising fallback instead of hanging.
            return critical_path_eu(waves)

        now_eu = running[0][0]
        while running and running[0][0] == now_eu:
            _finish_eu, _sort_key, wave_id = heappop(running)
            completed.add(wave_id)

    return round(now_eu, 2)


def compute_realistic_wall_clock(
    waves: list[Wave],
    *,
    max_parallel_waves: int = 4,
    inside_pessimistic_share: float | None = None,
    eu_minutes: float = 30.0,
) -> RealisticWallClockMetric:
    """Compute realistic wall-clock for a DAG of waves.

    The projection combines three independent signals:

    1. Work sum: total serial EU across all waves.
    2. Critical path + queue: dependency lower bound plus finite worker
       scheduling via ``max_parallel_waves``.
    3. Calibration: ``inside_pessimistic_share`` inflates the queued
       projection when recent waves often exceeded pessimistic estimates.

    Args:
        waves: Waves to schedule as one iter or phase DAG.
        max_parallel_waves: Worker count for queue simulation.
        inside_pessimistic_share: Fraction of calibrated samples inside
            pessimistic estimate bounds. ``None`` means no calibration data.
        eu_minutes: Conversion factor for the rendered hours column.

    Raises:
        ValueError: ``max_parallel_waves < 1``, ``eu_minutes <= 0``, or
            ``inside_pessimistic_share`` outside ``[0.0, 1.0]``.
    """
    if max_parallel_waves < 1:
        raise ValueError(f"max_parallel_waves must be >= 1: {max_parallel_waves!r}")
    if eu_minutes <= 0:
        raise ValueError(f"eu_minutes must be > 0: {eu_minutes!r}")
    if inside_pessimistic_share is not None and not 0.0 <= inside_pessimistic_share <= 1.0:
        raise ValueError(
            f"inside_pessimistic_share must be between 0 and 1: {inside_pessimistic_share!r}"
        )

    work_sum = sum_wave_eu(waves)
    critical = critical_path_eu(waves)
    queued = max(critical, _queue_wall_clock_eu(waves, max_parallel_waves=max_parallel_waves))
    multiplier = 1.0
    if inside_pessimistic_share is not None:
        multiplier += 1.0 - inside_pessimistic_share
    realistic_eu = round(queued * multiplier, 2)
    return RealisticWallClockMetric(
        work_sum_eu=work_sum,
        critical_path_eu=critical,
        queue_wall_clock_eu=queued,
        inside_pessimistic_share=inside_pessimistic_share,
        pessimism_multiplier=round(multiplier, 4),
        realistic_wall_clock_eu=realistic_eu,
        realistic_wall_clock_hours=round(realistic_eu * eu_minutes / 60.0, 2),
        max_parallel_waves=max_parallel_waves,
        eu_minutes=eu_minutes,
    )


def compute_eu_variance(state: State) -> EuVarianceMetric:
    """Compute the EU-variance roll-up across CLOSED waves.

    A wave contributes when:

    1. ``wave.status == WaveStatus.CLOSED``.
    2. ``state.estimates`` contains an entry for ``wave.id``.
    3. ``state.actuals`` contains an entry for ``wave.id`` with non-None
       ``elapsed_eu`` that is not flagged ``calibration_excluded``.

    The delta is ``actual.elapsed_eu - estimate.expected_eu``. Negative
    deltas mean the wave finished faster than expected; positive deltas
    mean it ran over. An excluded actual is dropped because the metric
    scores the ESTIMATE against a measured pace, and a re-originated or
    shared-session figure is a measurement artefact rather than a pace.
    """
    estimates = state.estimates or {}
    deltas: list[float] = []
    inside_pessimistic = 0
    for wave in state.waves.values():
        if wave.status != WaveStatus.CLOSED:
            continue
        est = estimates.get(wave.id)
        act = resolve_wave_actual(state, wave.id)
        if est is None or act is None or act.calibration_excluded:
            continue
        delta = act.elapsed_eu - est.expected_eu
        deltas.append(delta)
        if act.elapsed_eu <= est.pessimistic_eu:
            inside_pessimistic += 1
    if not deltas:
        return EuVarianceMetric(
            sample_count=0,
            mean_delta_eu=0.0,
            stdev_delta_eu=0.0,
            inside_pessimistic_share=0.0,
        )
    mean = sum(deltas) / len(deltas)
    stdev = _stdev_population(deltas, mean)
    return EuVarianceMetric(
        sample_count=len(deltas),
        mean_delta_eu=mean,
        stdev_delta_eu=stdev,
        inside_pessimistic_share=inside_pessimistic / len(deltas),
    )


def compute_estimate_actual_variance(state: State) -> EstimateActualVarianceMetric:
    """Compute the M26 estimate-actual variance percentage.

    The C09 §5.9.6 M26 gauge feeding the C06 VarianceTile and the ship-gate
    Variance section. A wave contributes when:

    1. ``wave.status == WaveStatus.CLOSED``.
    2. ``state.estimates`` carries an entry for ``wave.id``.
    3. ``state.actuals`` carries an entry for ``wave.id`` that is not
       flagged ``calibration_excluded``.

    The variance is the aggregate ``(sum actual_eu - sum planned_eu) / sum
    planned_eu * 100`` over the contributing waves. The aggregate form (sum
    of actuals vs sum of estimates) — rather than a mean of per-wave ratios
    — keeps a single large over-run from being diluted by many small waves,
    matching the ship-gate's "did the phase as a whole run over?" question.
    An excluded actual is dropped for the same reason the re-fit drops it:
    the figure is a floor or a split, so folding it into the numerator
    reports a measurement artefact as an over- or under-run.

    A positive percentage means the work ran over the estimate; a negative
    percentage means it finished under. ``variance_pct`` is ``None`` when no
    wave contributes or when the planned-EU denominator is zero.

    Args:
        state: Loaded typed :class:`State` snapshot (read-only).

    Returns:
        The M26 roll-up — sample count, summed planned / actual EU, and the
        variance percentage (``None`` when not computable).
    """
    estimates = state.estimates or {}
    planned_eu = 0.0
    actual_eu = 0.0
    sample_count = 0
    for wave in state.waves.values():
        if wave.status != WaveStatus.CLOSED:
            continue
        est = estimates.get(wave.id)
        act = resolve_wave_actual(state, wave.id)
        if est is None or act is None or act.calibration_excluded:
            continue
        planned_eu += est.expected_eu
        actual_eu += act.elapsed_eu
        sample_count += 1
    variance_pct = (actual_eu - planned_eu) / planned_eu * 100.0 if planned_eu > 0 else None
    return EstimateActualVarianceMetric(
        sample_count=sample_count,
        planned_eu=planned_eu,
        actual_eu=actual_eu,
        variance_pct=variance_pct,
    )


def compute_audit_pass_rate(state: State) -> AuditPassRateMetric:
    """Compute the audit pass-rate across audits with a verdict set.

    Denominator: audits with ``verdict in {PASS, MINOR, MAJOR}``. Audits
    whose verdict is still ``None`` (PENDING / RUNNING) are excluded so
    in-flight work does not skew the pass share.
    """
    audits = state.audits or {}
    pass_count = 0
    minor_count = 0
    major_count = 0
    for audit in audits.values():
        if audit.verdict is None:
            continue
        if audit.verdict == AuditVerdict.PASS:
            pass_count += 1
        elif audit.verdict == AuditVerdict.MINOR:
            minor_count += 1
        elif audit.verdict == AuditVerdict.MAJOR:
            major_count += 1
    decided = pass_count + minor_count + major_count
    share = (pass_count / decided) if decided else 0.0
    return AuditPassRateMetric(
        decided_count=decided,
        pass_count=pass_count,
        minor_count=minor_count,
        major_count=major_count,
        pass_share=share,
    )


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


def compute_planned_vs_reactive(state: State) -> PlannedVsReactiveMetric:
    """Split waves by their iter's :class:`~eawf.kernel.state.enums.IterTrigger`.

    Each wave is classified via :func:`_classify_wave`: a ``proactive``
    iter contributes to ``planned_count``, a ``reactive`` iter to
    ``reactive_count``, and a ``none`` iter is excluded from both so it
    drops out of the denominator. Waves whose iter row is absent fall back
    to the id-suffix heuristic, and waves whose id does not parse are
    excluded from both counts.
    """
    planned = 0
    reactive = 0
    for wave in state.waves.values():
        bucket = _classify_wave(wave, state.iters)
        if bucket == "planned":
            planned += 1
        elif bucket == "reactive":
            reactive += 1
    total = planned + reactive
    share = (reactive / total) if total else 0.0
    return PlannedVsReactiveMetric(
        planned_count=planned,
        reactive_count=reactive,
        reactive_share=share,
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


def compute_metrics(state: State) -> MetricsSummary:
    """Aggregate the four wave-level metrics into a :class:`MetricsSummary`.

    This is the single entry point for the CLI handler and downstream
    consumers (TUI overlay, release-notes renderer). The function is
    pure — it does not read disk, append events, or mutate state.
    """
    return MetricsSummary(
        schema_version=METRICS_SCHEMA_VERSION,
        eu_variance=compute_eu_variance(state),
        audit_pass_rate=compute_audit_pass_rate(state),
        wave_elapsed=compute_wave_elapsed(state),
        planned_vs_reactive=compute_planned_vs_reactive(state),
    )


__all__ = [
    "METRICS_SCHEMA_VERSION",
    "WEEKLY_BURN_WINDOW",
    "AuditPassRateMetric",
    "EstimateActualVarianceMetric",
    "EuVarianceMetric",
    "MetricsSummary",
    "PlannedVsReactiveMetric",
    "RealisticWallClockMetric",
    "WaveElapsedMetric",
    "WeeklyBurnMetric",
    "compute_audit_pass_rate",
    "compute_estimate_actual_variance",
    "compute_eu_variance",
    "compute_metrics",
    "compute_planned_vs_reactive",
    "compute_realistic_wall_clock",
    "compute_wave_elapsed",
    "compute_weekly_burn",
]
