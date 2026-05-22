"""Wave effort-bucket roll-up + calibration helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from eawf.state.enums import ActualStatus, Confidence, EffortBucket, WaveStatus
from eawf.state.models import ActualSummary, EstimateSummary, State, Wave

BUCKET_EU: dict[EffortBucket, float] = {
    EffortBucket.XS: 0.25,
    EffortBucket.S: 0.5,
    EffortBucket.M: 1.0,
    EffortBucket.L: 2.0,
    EffortBucket.XL: 3.5,
}

EU_MINUTES = 30.0

#: Pessimistic-over-expected ratio for a bucket-derived default estimate.
#: Mirrors the ``eawf estimate set`` defaults (pessimistic 1.8 / central 0.5),
#: so a bucket default and an operator-set estimate land on the same spread
#: and the ``inside_pessimistic`` calibration metric stays comparable.
_DEFAULT_PESSIMISTIC_RATIO = 3.6

#: Rolling window for :func:`calibrate_buckets` — actuals older than 90 days
#: do not inform the re-fit so a long-stale corpus cannot freeze drift.
CALIBRATION_WINDOW: timedelta = timedelta(days=90)

#: Relative-drift threshold for a calibration nudge. A bucket whose fitted
#: centroid differs from its configured :data:`BUCKET_EU` value by more than
#: this fraction (``> 25 %``) emits a nudge; at-or-below the threshold is
#: treated as in-tolerance and stays quiet.
DRIFT_THRESHOLD: float = 0.25


def wave_estimate_eu(wave: Wave) -> float:
    """Return default EU for ``wave.effort_bucket`` or ``0`` when unset."""
    if wave.effort_bucket is None:
        return 0.0
    return BUCKET_EU[wave.effort_bucket]


def sum_wave_eu(waves: list[Wave]) -> float:
    """Return capacity budget across all waves."""
    return round(sum(wave_estimate_eu(wave) for wave in waves), 2)


def critical_path_eu(waves: list[Wave]) -> float:
    """Return longest dependency-path effort in EU."""
    by_id = {wave.id: wave for wave in waves}
    memo: dict[str, float] = {}

    def _cost(wave: Wave, seen: set[str]) -> float:
        if wave.id in memo:
            return memo[wave.id]
        if wave.id in seen:
            return wave_estimate_eu(wave)
        dep_costs = [_cost(by_id[dep], seen | {wave.id}) for dep in wave.deps if dep in by_id]
        total = wave_estimate_eu(wave) + (max(dep_costs) if dep_costs else 0.0)
        memo[wave.id] = total
        return total

    if not waves:
        return 0.0
    return round(max(_cost(wave, set()) for wave in waves), 2)


class BucketCalibration(BaseModel):
    """Re-fit verdict for one effort bucket against 90-day actuals.

    Attributes:
        bucket: The effort bucket this row calibrates.
        configured_eu: The currently-configured centroid (:data:`BUCKET_EU`).
        fitted_eu: The mean actual EU of CLOSED, in-window waves tagged with
            *bucket*. ``None`` when the bucket has no in-window samples.
        sample_count: Number of contributing waves.
        drift_pct: Relative drift ``|fitted - configured| / configured *
            100``, or ``None`` when ``fitted_eu`` is ``None``.
        nudge: ``True`` when ``drift_pct`` exceeds the :data:`DRIFT_THRESHOLD`
            (``> 25 %``) — the operator should re-cadence the bucket.
    """

    model_config = ConfigDict(extra="forbid")

    bucket: EffortBucket
    configured_eu: float = Field(gt=0.0)
    fitted_eu: float | None
    sample_count: int = Field(ge=0)
    drift_pct: float | None
    nudge: bool


class CalibrationReport(BaseModel):
    """The full XS..XL calibration verdict over a 90-day actuals window."""

    model_config = ConfigDict(extra="forbid")

    window_days: int = Field(gt=0)
    drift_threshold_pct: float = Field(gt=0.0)
    buckets: list[BucketCalibration]

    @property
    def nudged_buckets(self) -> list[EffortBucket]:
        """Return the buckets whose fitted drift fired a nudge."""
        return [row.bucket for row in self.buckets if row.nudge]


def _bucket_actuals(state: State, *, now: datetime) -> dict[EffortBucket, list[float]]:
    """Group in-window actual EU by the wave's configured effort bucket.

    A wave contributes its :class:`ActualSummary.elapsed_eu` when it is
    CLOSED, carries a non-``None`` ``effort_bucket``, has an actual whose
    ``updated_at`` falls inside the :data:`CALIBRATION_WINDOW`, and that
    actual records positive elapsed EU.

    Args:
        state: Loaded typed :class:`State` snapshot (read-only).
        now: Window anchor (UTC) — actuals before ``now - window`` are
            excluded.

    Returns:
        A mapping of effort bucket to the list of contributing actual EU.
    """
    actuals = state.actuals or {}
    window_start = now - CALIBRATION_WINDOW
    grouped: dict[EffortBucket, list[float]] = {bucket: [] for bucket in BUCKET_EU}
    for wave in state.waves.values():
        if wave.status != WaveStatus.CLOSED or wave.effort_bucket is None:
            continue
        act = actuals.get(wave.id)
        if act is None or act.elapsed_eu <= 0:
            continue
        if not (window_start <= act.updated_at <= now):
            continue
        grouped[wave.effort_bucket].append(act.elapsed_eu)
    return grouped


def calibrate_buckets(state: State, *, now: datetime | None = None) -> CalibrationReport:
    """Re-fit the XS..XL effort-bucket centroids from 90-day actuals.

    For every bucket the re-fit takes the mean elapsed EU of CLOSED, in-
    window waves tagged with that bucket and compares it to the configured
    :data:`BUCKET_EU` centroid. When the relative drift exceeds
    :data:`DRIFT_THRESHOLD` (``> 25 %``) the bucket's row carries
    ``nudge=True`` so the operator can re-cadence it. Buckets with no in-
    window samples report ``fitted_eu=None`` / ``drift_pct=None`` and never
    nudge (no evidence to act on).

    This is a **pure** read-only computation — it does not mutate the
    configured centroids, write state, or append events. Applying a nudge is
    an explicit operator action surfaced by the CLI, not a side effect.

    Args:
        state: Loaded typed :class:`State` snapshot (read-only).
        now: Optional clock injection for deterministic tests. Defaults to
            :func:`datetime.now` (UTC).

    Returns:
        The per-bucket calibration verdict over the 90-day window.
    """
    anchor = now if now is not None else datetime.now(UTC)
    grouped = _bucket_actuals(state, now=anchor)
    rows: list[BucketCalibration] = []
    for bucket, configured in BUCKET_EU.items():
        samples = grouped[bucket]
        if not samples:
            rows.append(
                BucketCalibration(
                    bucket=bucket,
                    configured_eu=configured,
                    fitted_eu=None,
                    sample_count=0,
                    drift_pct=None,
                    nudge=False,
                )
            )
            continue
        fitted = sum(samples) / len(samples)
        drift = abs(fitted - configured) / configured
        rows.append(
            BucketCalibration(
                bucket=bucket,
                configured_eu=configured,
                fitted_eu=fitted,
                sample_count=len(samples),
                drift_pct=drift * 100.0,
                nudge=drift > DRIFT_THRESHOLD,
            )
        )
    return CalibrationReport(
        window_days=CALIBRATION_WINDOW.days,
        drift_threshold_pct=DRIFT_THRESHOLD * 100.0,
        buckets=rows,
    )


def timestamp_actual_eu(waves: list[Wave]) -> float:
    """Estimate actual EU from closed wave timestamps."""
    total_minutes = 0.0
    for wave in waves:
        if wave.opened_at is None or wave.closed_at is None:
            continue
        opened = wave.opened_at
        closed = wave.closed_at
        if not isinstance(opened, datetime) or not isinstance(closed, datetime):
            continue
        delta = closed - opened
        if delta.total_seconds() <= 0:
            continue
        total_minutes += delta.total_seconds() / 60.0
    return round(total_minutes / EU_MINUTES, 2)


def default_estimate_summary(wave: Wave, *, now: datetime) -> EstimateSummary | None:
    """Derive a default :class:`EstimateSummary` from ``wave.effort_bucket``.

    The bucket centroid (:data:`BUCKET_EU`) is the expected EU; the
    pessimistic figure applies :data:`_DEFAULT_PESSIMISTIC_RATIO` so the
    spread matches an operator-set estimate. Returns ``None`` when the wave
    carries no ``effort_bucket`` — there is no centroid to derive from, so
    no estimate is written (the variance metric simply skips the wave).

    The ``current_store_record_id`` is a synthetic, store-free id: lifecycle
    transitions are pure in-memory state mutators and never append a JSONL
    audit envelope, so the field records provenance ("bucket default at claim
    time") rather than pointing at a store record.

    Args:
        wave: The wave being claimed.
        now: Claim timestamp (UTC) used for ``updated_at`` and the id stamp.

    Returns:
        The bucket-derived estimate, or ``None`` when ``effort_bucket`` is
        unset.
    """
    if wave.effort_bucket is None:
        return None
    expected_eu = BUCKET_EU[wave.effort_bucket]
    pessimistic_eu = round(expected_eu * _DEFAULT_PESSIMISTIC_RATIO, 2)
    expected_minutes = round(expected_eu * EU_MINUTES, 2)
    pessimistic_minutes = round(pessimistic_eu * EU_MINUTES, 2)
    estimate_id = f"EST-{wave.id}"
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    display = f"{expected_eu} EU · exp ~{expected_minutes:.0f}m · pess ~{pessimistic_minutes:.0f}m"
    return EstimateSummary(
        id=estimate_id,
        scope_id=wave.id,
        expected_eu=expected_eu,
        pessimistic_eu=pessimistic_eu,
        expected_minutes=expected_minutes,
        pessimistic_minutes=pessimistic_minutes,
        display=display,
        reference_class=f"bucket:{wave.effort_bucket.value}",
        confidence=Confidence.LOW,
        current_store_record_id=f"{estimate_id}-bucket-{stamp}",
        updated_at=now,
    )


def actual_summary_from_timestamps(wave: Wave, *, now: datetime) -> ActualSummary | None:
    """Derive an :class:`ActualSummary` from ``opened_at``/``closed_at``.

    Reuses :func:`timestamp_actual_eu` (single-wave list) for the elapsed-EU
    derivation. Returns ``None`` — derives nothing — when either timestamp is
    missing or the elapsed span is non-positive, so a close on a wave with no
    usable timestamps never raises. This is the close-path crash-safety
    contract: a missing actual is a skipped sample, not a fault.

    The ``current_store_record_id`` is synthetic for the same reason as
    :func:`default_estimate_summary`: lifecycle mutators do not append store
    records.

    Args:
        wave: The wave being closed (``opened_at``/``closed_at`` must be set
            for an actual to be derived).
        now: Close timestamp (UTC) used for ``updated_at`` and the id stamp.

    Returns:
        The timestamp-derived actual, or ``None`` when no usable elapsed span
        exists.
    """
    if wave.opened_at is None or wave.closed_at is None:
        return None
    elapsed_eu = timestamp_actual_eu([wave])
    if elapsed_eu <= 0:
        return None
    actual_id = f"ACT-{wave.id}"
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return ActualSummary(
        id=actual_id,
        scope_id=wave.id,
        status=ActualStatus.DONE,
        elapsed_eu=elapsed_eu,
        attention_eu=None,
        agent_runtime_eu=None,
        current_store_record_id=f"{actual_id}-close-{stamp}",
        updated_at=now,
    )
