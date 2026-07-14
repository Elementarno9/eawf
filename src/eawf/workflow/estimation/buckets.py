"""Wave effort-bucket roll-up + calibration helpers.

Bucket-drift premise (B069 reframe): the EU effort buckets are re-fit
against recorded actual elapsed EU. The recalibration input is the
measured agent runtime captured on every :class:`ActualSummary` at wave
close (derived from the telemetry-rollup session ``duration_ms``), so the
drift re-fit now reads a live signal rather than waiting on the operator
to run ``eawf actual start/stop`` by hand. ``elapsed_eu`` is the measured
session runtime, NOT the open->close wall-clock span (which counts idle /
overnight / cross-session time and would inflate the re-fit), so the
calibration compares like with like against the :data:`BUCKET_EU`
centroids.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.config.schema import EstimationConfig
from eawf.kernel.state.enums import Confidence, EffortBucket, WaveStatus
from eawf.kernel.state.models import ActualSummary, EstimateSummary, State, Wave

#: Code-canonical effort-bucket EU calibration — the single source of the
#: bucket -> expected-EU mapping. One EU is ~30 minutes of agent-driven
#: session time (:data:`EU_MINUTES`), so XS is a ~7.5-minute task and XL a
#: ~105-minute one. Operator config (``estimation.buckets.overrides``) may
#: tighten a bucket per repo, but this table is the fallback every estimate
#: and the drift calibration compare against. These centroids are the
#: starting estimate the close-time measured ``elapsed_eu`` is re-fit
#: against (the B069 recalibration input); :func:`calibrate_buckets`
#: surfaces the drift but never mutates this table, so the values stay
#: pinned by
#: ``tests/workflow/estimation/test_buckets.py::test_bucket_eu_is_canonical_table``
#: and a silent edit reds a test rather than shifting every projection.
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

FIT_N_MIN: int = 5
HIGH_CONFIDENCE_SAMPLE_COUNT: int = 30


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


def resolve_wave_actual(state: State, wave_id: str) -> ActualSummary | None:
    """Resolve a wave's :class:`ActualSummary` — the single resolution path.

    This is the **one** place that maps a wave id onto its actual row, so
    every realized-EU reader (the :func:`actual_eu_for_wave` /
    :func:`actual_eu_for_iter` / :func:`actual_eu_for_phase` accessors, plus
    any caller that needs the full row for the pessimistic bound or existence
    check) shares one resolution semantics instead of re-deriving it.

    Resolution handles both keying conventions that appear in ``state.actuals``
    on disk: an actual stored under its wave-id dict key, **and** an actual
    whose dict key differs but whose ``scope_id`` is the wave id. The dict-key
    match wins when both are present (it is the canonical key); the
    ``scope_id`` scan is the fallback. Returns ``None`` when neither resolves.

    Args:
        state: Loaded typed :class:`State` snapshot (read-only).
        wave_id: The wave whose actual is wanted.

    Returns:
        The resolved :class:`ActualSummary`, or ``None`` when *wave_id* has no
        actual under either keying convention.
    """
    actuals = state.actuals or {}
    direct = actuals.get(wave_id)
    if direct is not None:
        return direct
    for actual in actuals.values():
        if actual.scope_id == wave_id:
            return actual
    return None


def actual_eu_for_wave(state: State, wave_id: str) -> float:
    """Return realized EU for one wave — the single realized-EU accessor.

    Reads ``elapsed_eu`` off the actual resolved by :func:`resolve_wave_actual`
    (the realized-EU counterpart to :func:`wave_estimate_eu` on the planned
    side). A wave with no matching actual contributes ``0.0`` — there is no
    realized effort to report yet.

    Args:
        state: Loaded typed :class:`State` snapshot (read-only).
        wave_id: The wave whose realized EU is wanted.

    Returns:
        The wave's ``ActualSummary.elapsed_eu``, or ``0.0`` when no actual
        resolves to *wave_id*.
    """
    actual = resolve_wave_actual(state, wave_id)
    return actual.elapsed_eu if actual is not None else 0.0


def actual_eu_for_iter(state: State, iter_id: str) -> float:
    """Return realized EU summed across an iter's waves.

    Sums :func:`actual_eu_for_wave` over every wave whose ``iter_id`` matches
    *iter_id*. An iter with no waves, or whose waves carry no actuals, returns
    ``0.0``.

    Args:
        state: Loaded typed :class:`State` snapshot (read-only).
        iter_id: The iter whose realized EU is wanted.

    Returns:
        The summed realized EU across the iter's waves, rounded to 2 dp.
    """
    total = sum(
        actual_eu_for_wave(state, wave.id)
        for wave in state.waves.values()
        if wave.iter_id == iter_id
    )
    return round(total, 2)


def actual_eu_for_phase(state: State, phase_id: str) -> float:
    """Return realized EU summed across a phase's waves.

    Resolves the phase's iters (``Iter.phase_id == phase_id``) and sums
    :func:`actual_eu_for_wave` over every wave under those iters. A phase with
    no waves, or whose waves carry no actuals, returns ``0.0``.

    This is the accessor a phase-scoped EU / status pane reads for its
    consumed-EU numerator; the return shape (a plain ``float`` keyed by phase
    id) is a drop-in for an inline ``sum(elapsed_eu ...)`` so the consumer
    needs no reshape.

    Args:
        state: Loaded typed :class:`State` snapshot (read-only).
        phase_id: The phase whose realized EU is wanted.

    Returns:
        The summed realized EU across the phase's waves, rounded to 2 dp.
    """
    phase_iter_ids = {
        iter_row.id for iter_row in state.iters.values() if iter_row.phase_id == phase_id
    }
    total = sum(
        actual_eu_for_wave(state, wave.id)
        for wave in state.waves.values()
        if wave.iter_id in phase_iter_ids
    )
    return round(total, 2)


class BucketCalibration(BaseModel):
    """Re-fit verdict for one effort bucket against 90-day actuals.

    Attributes:
        bucket: The effort bucket this row calibrates.
        configured_eu: The currently-configured centroid (:data:`BUCKET_EU`).
        fitted_eu: The mean actual EU of CLOSED, in-window waves tagged with
            *bucket*. ``None`` when the bucket has no in-window samples.
        fitted_pessimistic_eu: The nearest-rank p90 actual EU of the same
            samples. ``None`` when the bucket has no in-window samples.
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
    fitted_pessimistic_eu: float | None
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
    actual records positive elapsed EU. The positive-elapsed filter keeps
    a wave with no captured runtime (auto-actual elapsed EU of ``0.0``)
    out of the re-fit; the close path now records measured runtime EU on
    the auto-actual, so a wave with telemetry contributes its real pace as
    the B069 recalibration input.

    An actual flagged :attr:`ActualSummary.calibration_excluded` is skipped
    even when it clears every other filter. The flag marks a figure that was
    measured but is not a reference class — a counter reset re-originated the
    wave (so the figure is a floor, not a measure) or several concurrent waves
    shared one session (so the figure is a split, and the split is an
    approximation whenever the concurrency moved mid-span). Re-fitting a
    bucket against either one moves the estimate by the measurement artefact
    rather than by the pace.

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
        if act is None or act.elapsed_eu <= 0 or act.calibration_excluded:
            continue
        if not (window_start <= act.updated_at <= now):
            continue
        grouped[wave.effort_bucket].append(act.elapsed_eu)
    return grouped


def _fitted_pessimistic_eu(samples: list[float]) -> float | None:
    """Return the nearest-rank p90 from fitted actual samples."""
    if not samples:
        return None
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * 0.9) - 1))
    return ordered[index]


def _estimation_config(config: EstimationConfig | Mapping[str, Any] | None) -> EstimationConfig:
    """Coerce optional layered config input to strict estimation config."""
    if config is None:
        return EstimationConfig()
    if isinstance(config, EstimationConfig):
        return config
    raw: Any = config
    if isinstance(raw, Mapping) and isinstance(raw.get("estimation"), Mapping):
        raw = raw["estimation"]
    return EstimationConfig.model_validate(raw)


def _configured_bucket_eu(bucket: EffortBucket, config: EstimationConfig) -> float:
    """Return configured expected EU for *bucket*, falling back to ``BUCKET_EU``."""
    override = config.buckets.overrides.get(bucket)
    if override is not None:
        return override.expected_eu
    return BUCKET_EU[bucket]


def calibrate_buckets(
    state: State,
    *,
    now: datetime | None = None,
    config: EstimationConfig | Mapping[str, Any] | None = None,
) -> CalibrationReport:
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
        config: Optional typed ``estimation`` config, or a merged config
            mapping containing an ``estimation`` section. Configured bucket
            overrides replace :data:`BUCKET_EU` for drift comparison.

    Returns:
        The per-bucket calibration verdict over the 90-day window.
    """
    anchor = now if now is not None else datetime.now(UTC)
    settings = _estimation_config(config)
    grouped = _bucket_actuals(state, now=anchor)
    rows: list[BucketCalibration] = []
    for bucket in BUCKET_EU:
        configured = _configured_bucket_eu(bucket, settings)
        samples = grouped[bucket]
        if not samples:
            rows.append(
                BucketCalibration(
                    bucket=bucket,
                    configured_eu=configured,
                    fitted_eu=None,
                    fitted_pessimistic_eu=None,
                    sample_count=0,
                    drift_pct=None,
                    nudge=False,
                )
            )
            continue
        fitted = sum(samples) / len(samples)
        fitted_pessimistic = _fitted_pessimistic_eu(samples)
        drift = abs(fitted - configured) / configured
        rows.append(
            BucketCalibration(
                bucket=bucket,
                configured_eu=configured,
                fitted_eu=fitted,
                fitted_pessimistic_eu=fitted_pessimistic,
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


class _EstimateBasis(BaseModel):
    """Resolved source values for a bucket-derived default estimate."""

    model_config = ConfigDict(extra="forbid")

    expected_eu: float = Field(gt=0.0)
    pessimistic_eu: float = Field(gt=0.0)
    confidence: Confidence
    source: str


def _confidence_for_sample_count(sample_count: int, config: EstimationConfig) -> Confidence:
    """Return calibrated-fit confidence for *sample_count*."""
    if sample_count >= config.buckets.high_confidence_n:
        return Confidence.HIGH
    return Confidence.MEDIUM


def _basis_from_config(bucket: EffortBucket, config: EstimationConfig) -> _EstimateBasis | None:
    """Return explicit config override basis for *bucket*, if configured."""
    override = config.buckets.overrides.get(bucket)
    if override is None:
        return None
    pessimistic = override.pessimistic_eu
    if pessimistic is None:
        pessimistic = override.expected_eu * _DEFAULT_PESSIMISTIC_RATIO
    return _EstimateBasis(
        expected_eu=override.expected_eu,
        pessimistic_eu=pessimistic,
        confidence=Confidence.HIGH,
        source="config",
    )


def _basis_from_fit(
    bucket: EffortBucket,
    *,
    state: State | None,
    now: datetime,
    config: EstimationConfig,
) -> _EstimateBasis | None:
    """Return calibrated actuals basis when the bucket has enough samples."""
    if state is None:
        return None
    report = calibrate_buckets(state, now=now, config=config)
    row = next(row for row in report.buckets if row.bucket == bucket)
    if row.fitted_eu is None or row.sample_count < config.buckets.n_min:
        return None
    pessimistic = row.fitted_pessimistic_eu
    if pessimistic is None:
        pessimistic = row.fitted_eu * _DEFAULT_PESSIMISTIC_RATIO
    return _EstimateBasis(
        expected_eu=row.fitted_eu,
        pessimistic_eu=pessimistic,
        confidence=_confidence_for_sample_count(row.sample_count, config),
        source="fitted",
    )


def _basis_from_bucket(bucket: EffortBucket) -> _EstimateBasis:
    """Return static built-in bucket basis."""
    expected = BUCKET_EU[bucket]
    return _EstimateBasis(
        expected_eu=expected,
        pessimistic_eu=expected * _DEFAULT_PESSIMISTIC_RATIO,
        confidence=Confidence.LOW,
        source="bucket",
    )


def default_estimate_summary(
    wave: Wave,
    *,
    now: datetime,
    state: State | None = None,
    config: EstimationConfig | Mapping[str, Any] | None = None,
) -> EstimateSummary | None:
    """Derive a default :class:`EstimateSummary` from ``wave.effort_bucket``.

    Estimate selection is ordered by trust: explicit config override first,
    then a fitted bucket centroid once the bucket has at least
    ``estimation.buckets.n_min`` in-window samples, then the static
    :data:`BUCKET_EU` fallback. Returns ``None`` when the wave carries no
    ``effort_bucket`` — there is no centroid to derive from, so no estimate
    is written (the variance metric simply skips the wave).

    The ``current_store_record_id`` is a synthetic, store-free id: lifecycle
    transitions are pure in-memory state mutators and never append a JSONL
    audit envelope, so the field records provenance ("bucket default at claim
    time") rather than pointing at a store record.

    Args:
        wave: The wave being claimed.
        now: Claim timestamp (UTC) used for ``updated_at`` and the id stamp.
        state: Optional state snapshot used for fitted-bucket calibration.
        config: Optional typed ``estimation`` config, or a merged config
            mapping containing an ``estimation`` section.

    Returns:
        The bucket-derived estimate, or ``None`` when ``effort_bucket`` is
        unset.
    """
    if wave.effort_bucket is None:
        return None
    settings = _estimation_config(config)
    bucket = wave.effort_bucket
    basis = (
        _basis_from_config(bucket, settings)
        or _basis_from_fit(bucket, state=state, now=now, config=settings)
        or _basis_from_bucket(bucket)
    )
    expected_eu = round(basis.expected_eu, 2)
    pessimistic_eu = round(basis.pessimistic_eu, 2)
    eu_minutes = settings.eu_minutes
    expected_minutes = round(expected_eu * eu_minutes, 2)
    pessimistic_minutes = round(pessimistic_eu * eu_minutes, 2)
    estimate_id = f"EST-{wave.id}"
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    display = f"{expected_eu} EU · exp ~{expected_minutes:.0f}m · pess ~{pessimistic_minutes:.0f}m"
    record_source = "bucket" if basis.source == "bucket" else f"bucket-{basis.source}"
    return EstimateSummary(
        id=estimate_id,
        scope_id=wave.id,
        expected_eu=expected_eu,
        pessimistic_eu=pessimistic_eu,
        expected_minutes=expected_minutes,
        pessimistic_minutes=pessimistic_minutes,
        display=display,
        reference_class=f"bucket:{wave.effort_bucket.value}",
        confidence=basis.confidence,
        current_store_record_id=f"{estimate_id}-{record_source}-{stamp}",
        updated_at=now,
    )
