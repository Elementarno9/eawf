"""Pure projection for the TUI ``/metrics`` six-tile dashboard."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import AgentSessionRole, EffortBucket, WaveStatus
from eawf.kernel.state.models import ActualSummary, EstimateSummary, State, Wave
from eawf.observability.telemetry.models import (
    RuntimeErrorClass,
    TelemetryRuntimeSwitch,
    TelemetrySession,
)
from eawf.observability.telemetry.store.base import AbstractMetricsStore
from eawf.workflow.estimation.buckets import CalibrationReport, calibrate_buckets
from eawf.workflow.estimation.metrics import (
    EstimateActualVarianceMetric,
    WaveElapsedMetric,
    WeeklyBurnMetric,
    compute_wave_elapsed,
    compute_weekly_burn,
)

logger = logging.getLogger(__name__)

METRICS_PROJECTION_SCHEMA_VERSION: Literal[1] = 1
MetricsWindow = Literal["7d", "30d", "90d", "all"]

_WINDOWS: dict[MetricsWindow, timedelta | None] = {
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
    "all": None,
}


class RuntimeTokensProjection(BaseModel):
    """Token totals for one runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    cache_create_tokens: int = Field(ge=0)

    @property
    def total_tokens(self) -> int:
        """Return all tracked tokens for the runtime."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_create_tokens
        )


class CacheHealthProjection(BaseModel):
    """Cache read/create totals and derived hit ratio for one runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime: str
    cache_read_tokens: int = Field(ge=0)
    cache_create_tokens: int = Field(ge=0)
    hit_ratio: float = Field(ge=0.0, le=1.0)


class SwitchoverFrequencyProjection(BaseModel):
    """Runtime switchover count for one cause."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cause: RuntimeErrorClass
    count: int = Field(ge=0)


class VarianceWaveProjection(BaseModel):
    """Per-wave estimate-vs-actual variance drill row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    wave_id: str
    title: str
    bucket: EffortBucket
    planned_eu: float = Field(ge=0.0)
    actual_eu: float = Field(ge=0.0)
    delta_eu: float
    variance_pct: float | None
    inside_pessimistic: bool


class VarianceBucketProjection(BaseModel):
    """Variance roll-up for one effort bucket plus its drill rows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bucket: EffortBucket
    sample_count: int = Field(ge=0)
    planned_eu: float = Field(ge=0.0)
    actual_eu: float = Field(ge=0.0)
    delta_eu: float
    variance_pct: float | None
    inside_pessimistic_share: float = Field(ge=0.0, le=1.0)
    waves: tuple[VarianceWaveProjection, ...] = Field(default_factory=tuple)


class RoleCalibrationProjection(BaseModel):
    """Per-agent-role bucket calibration extension row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_role: AgentSessionRole
    report: CalibrationReport


class MetricsProjection(BaseModel):
    """Six-tile metrics projection consumed by the TUI overlay."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = METRICS_PROJECTION_SCHEMA_VERSION
    scope: str
    window: MetricsWindow
    generated_at: datetime
    variance: EstimateActualVarianceMetric
    variance_by_bucket: tuple[VarianceBucketProjection, ...] = Field(default_factory=tuple)
    weekly_burn: WeeklyBurnMetric
    wave_elapsed: WaveElapsedMetric
    cache_health: tuple[CacheHealthProjection, ...] = Field(default_factory=tuple)
    switchover_frequency: tuple[SwitchoverFrequencyProjection, ...] = Field(default_factory=tuple)
    per_runtime_tokens: tuple[RuntimeTokensProjection, ...] = Field(default_factory=tuple)
    per_role_calibration: tuple[RoleCalibrationProjection, ...] = Field(default_factory=tuple)


def compute_metrics_projection(
    state: State,
    *,
    store: AbstractMetricsStore | None = None,
    scope: str | None = None,
    window: MetricsWindow = "7d",
    now: datetime | None = None,
) -> MetricsProjection:
    """Compute the dashboard projection from state plus optional telemetry rows.

    Args:
        state: Loaded state snapshot.
        store: Optional telemetry store. Missing store yields empty telemetry
            tiles while state-backed tiles still populate.
        scope: Optional scope filter. ``None`` means the state's current scope.
        window: Rolling window for telemetry rows.
        now: Clock injection for deterministic tests.

    Returns:
        A strict :class:`MetricsProjection` with one field per dashboard tile
        and variance drill-down rows grouped by effort bucket.
    """
    anchor = now if now is not None else datetime.now(UTC)
    effective_scope = scope or state.urn
    scoped_state = _state_for_scope(state, scope=scope)
    sessions, switches = _fetch_telemetry_rows(
        store,
        state=state,
        scope=effective_scope,
        window=window,
        now=anchor,
    )
    projection = MetricsProjection(
        scope=effective_scope,
        window=window,
        generated_at=anchor,
        variance=_compute_variance(scoped_state, scope=None),
        variance_by_bucket=_variance_by_bucket(scoped_state, scope=None),
        weekly_burn=compute_weekly_burn(scoped_state, now=anchor),
        wave_elapsed=compute_wave_elapsed(scoped_state),
        cache_health=_cache_health(sessions),
        switchover_frequency=_switchover_frequency(switches),
        per_runtime_tokens=_per_runtime_tokens(sessions),
        per_role_calibration=_per_role_calibration(scoped_state, scope=None, now=anchor),
    )
    logger.info(
        f"compute_metrics_projection scope={effective_scope!r} window={window!r} "
        f"sessions={len(sessions)} switches={len(switches)}"
    )
    return projection


def _fetch_telemetry_rows(
    store: AbstractMetricsStore | None,
    *,
    state: State,
    scope: str,
    window: MetricsWindow,
    now: datetime,
) -> tuple[list[TelemetrySession], list[TelemetryRuntimeSwitch]]:
    """Return filtered telemetry session and switchover rows."""
    if store is None:
        return [], []
    sessions = [
        row
        for row in store.fetch_all("telemetry_sessions", TelemetrySession)
        if isinstance(row, TelemetrySession)
    ]
    switches = [
        row
        for row in store.fetch_all("telemetry_runtime_switches", TelemetryRuntimeSwitch)
        if isinstance(row, TelemetryRuntimeSwitch)
    ]
    return (
        [
            row
            for row in sessions
            if _session_in_scope(row, scope) and _in_window(row.started_at, window, now)
        ],
        [
            row
            for row in switches
            if (
                _state_wave_id_in_scope(row.wave_id, state, scope)
                and _in_window(row.ts, window, now)
            )
        ],
    )


def _in_window(ts: datetime | None, window: MetricsWindow, now: datetime) -> bool:
    """Return whether *ts* is inside the requested rolling window."""
    delta = _WINDOWS[window]
    if delta is None:
        return True
    if ts is None:
        return False
    return now - delta <= ts <= now


def _session_in_scope(session: TelemetrySession, scope: str) -> bool:
    """Return whether *session* belongs to *scope*."""
    if scope in {"user", "workspace", "all"}:
        return True
    if session.project_id == scope:
        return True
    if scope.startswith("urn:"):
        return False
    if session.wave_id is None:
        return False
    return _wave_in_scope(session.wave_id, scope)


def _wave_in_scope(wave_id: str, scope: str) -> bool:
    """Return whether *wave_id* belongs to a wave, iter, or phase scope."""
    if scope in {"user", "workspace", "all"}:
        return True
    if scope.startswith("urn:"):
        return False
    if wave_id == scope:
        return True
    parts = wave_id.split("-")
    if len(parts) >= 2 and "-".join(parts[:2]) == scope:
        return True
    return bool(parts and parts[0] == scope)


def _state_wave_in_scope(wave: Wave, state: State, scope: str | None) -> bool:
    """Return whether *wave* belongs to the requested state scope."""
    if scope is None or scope in {"user", "workspace", "all", state.urn}:
        return True
    if state.project is not None and scope == state.project.repo_urn:
        return True
    return _wave_in_scope(wave.id, scope)


def _state_wave_id_in_scope(wave_id: str, state: State, scope: str) -> bool:
    """Return whether a telemetry wave id belongs to a state-backed scope."""
    wave = state.waves.get(wave_id)
    if wave is not None:
        return _state_wave_in_scope(wave, state, scope)
    if scope in {state.urn, "user", "workspace", "all"}:
        return scope != state.urn
    if state.project is not None and scope == state.project.repo_urn:
        return False
    return _wave_in_scope(wave_id, scope)


def _state_for_scope(state: State, *, scope: str | None) -> State:
    """Return a state copy with waves and wave-keyed actuals filtered by scope."""
    scoped_waves = {
        wave_id: wave
        for wave_id, wave in state.waves.items()
        if _state_wave_in_scope(wave, state, scope)
    }
    scoped_wave_ids = set(scoped_waves)
    scoped_actuals: dict[str, ActualSummary] = {}
    for key, actual in (state.actuals or {}).items():
        if key in scoped_wave_ids or actual.scope_id in scoped_wave_ids:
            scoped_actuals[key] = actual
    scoped_estimates: dict[str, EstimateSummary] = {}
    for key, estimate in (state.estimates or {}).items():
        if key in scoped_wave_ids or estimate.scope_id in scoped_wave_ids:
            scoped_estimates[key] = estimate
    return state.model_copy(
        update={
            "waves": scoped_waves,
            "actuals": scoped_actuals,
            "estimates": scoped_estimates,
        }
    )


def _variance_rows(state: State, *, scope: str | None) -> list[VarianceWaveProjection]:
    """Return all per-wave variance drill rows for the requested scope."""
    estimates = state.estimates or {}
    actuals = state.actuals or {}
    rows: list[VarianceWaveProjection] = []
    for wave in state.waves.values():
        if not _state_wave_in_scope(wave, state, scope):
            continue
        if wave.status != WaveStatus.CLOSED or wave.effort_bucket is None:
            continue
        est = _estimate_for_wave(estimates, wave.id)
        act = _actual_for_wave(actuals, wave.id)
        if est is None or act is None:
            continue
        delta = act.elapsed_eu - est.expected_eu
        variance_pct = delta / est.expected_eu * 100.0 if est.expected_eu > 0 else None
        rows.append(
            VarianceWaveProjection(
                wave_id=wave.id,
                title=wave.title,
                bucket=wave.effort_bucket,
                planned_eu=est.expected_eu,
                actual_eu=act.elapsed_eu,
                delta_eu=delta,
                variance_pct=variance_pct,
                inside_pessimistic=act.elapsed_eu <= est.pessimistic_eu,
            )
        )
    return sorted(rows, key=lambda row: row.wave_id)


def _estimate_for_wave(rows: dict[str, EstimateSummary], wave_id: str) -> EstimateSummary | None:
    """Return the estimate keyed by wave id or carrying wave id as scope."""
    direct = rows.get(wave_id)
    if direct is not None:
        return direct
    for row in rows.values():
        if row.scope_id == wave_id:
            return row
    return None


def _actual_for_wave(rows: dict[str, ActualSummary], wave_id: str) -> ActualSummary | None:
    """Return the actual keyed by wave id or carrying wave id as scope."""
    direct = rows.get(wave_id)
    if direct is not None:
        return direct
    for row in rows.values():
        if row.scope_id == wave_id:
            return row
    return None


def _compute_variance(state: State, *, scope: str | None) -> EstimateActualVarianceMetric:
    """Compute aggregate estimate-actual variance for the requested scope."""
    planned_eu = 0.0
    actual_eu = 0.0
    rows = _variance_rows(state, scope=scope)
    for row in rows:
        planned_eu += row.planned_eu
        actual_eu += row.actual_eu
    variance_pct = (actual_eu - planned_eu) / planned_eu * 100.0 if planned_eu > 0 else None
    return EstimateActualVarianceMetric(
        sample_count=len(rows),
        planned_eu=planned_eu,
        actual_eu=actual_eu,
        variance_pct=variance_pct,
    )


def _variance_by_bucket(
    state: State,
    *,
    scope: str | None,
) -> tuple[VarianceBucketProjection, ...]:
    """Group variance drill rows by effort bucket."""
    grouped: dict[EffortBucket, list[VarianceWaveProjection]] = defaultdict(list)
    for row in _variance_rows(state, scope=scope):
        grouped[row.bucket].append(row)
    buckets: list[VarianceBucketProjection] = []
    for bucket in EffortBucket:
        rows = grouped.get(bucket, [])
        if not rows:
            continue
        planned_eu = sum(row.planned_eu for row in rows)
        actual_eu = sum(row.actual_eu for row in rows)
        delta_eu = actual_eu - planned_eu
        variance_pct = delta_eu / planned_eu * 100.0 if planned_eu > 0 else None
        inside = sum(1 for row in rows if row.inside_pessimistic)
        buckets.append(
            VarianceBucketProjection(
                bucket=bucket,
                sample_count=len(rows),
                planned_eu=planned_eu,
                actual_eu=actual_eu,
                delta_eu=delta_eu,
                variance_pct=variance_pct,
                inside_pessimistic_share=inside / len(rows),
                waves=tuple(rows),
            )
        )
    return tuple(buckets)


def _per_role_calibration(
    state: State,
    *,
    scope: str | None,
    now: datetime,
) -> tuple[RoleCalibrationProjection, ...]:
    """Return CalibrationReport rows for each observed agent role."""
    actuals = state.actuals or {}
    scoped_waves = [
        wave
        for wave in state.waves.values()
        if wave.agent_role is not None and _state_wave_in_scope(wave, state, scope)
    ]
    rows: list[RoleCalibrationProjection] = []
    for role in AgentSessionRole:
        role_waves = {wave.id: wave for wave in scoped_waves if wave.agent_role == role}
        if not role_waves:
            continue
        role_actuals: dict[str, ActualSummary] = {}
        for wave_id in role_waves:
            actual = _actual_for_wave(actuals, wave_id)
            if actual is not None:
                role_actuals[wave_id] = actual
        role_state = state.model_copy(update={"waves": role_waves, "actuals": role_actuals})
        rows.append(
            RoleCalibrationProjection(
                agent_role=role,
                report=calibrate_buckets(role_state, now=now),
            )
        )
    return tuple(rows)


def _per_runtime_tokens(sessions: list[TelemetrySession]) -> tuple[RuntimeTokensProjection, ...]:
    """Aggregate token counts by runtime."""
    totals: dict[str, dict[str, int]] = defaultdict(
        lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
    )
    for session in sessions:
        bucket = totals[session.runtime]
        bucket["input"] += session.total_input_tokens
        bucket["output"] += session.total_output_tokens
        bucket["cache_read"] += session.total_cache_read
        bucket["cache_create"] += session.total_cache_write
    return tuple(
        RuntimeTokensProjection(
            runtime=runtime,
            input_tokens=counts["input"],
            output_tokens=counts["output"],
            cache_read_tokens=counts["cache_read"],
            cache_create_tokens=counts["cache_create"],
        )
        for runtime, counts in sorted(totals.items())
    )


def _cache_health(sessions: list[TelemetrySession]) -> tuple[CacheHealthProjection, ...]:
    """Aggregate cache health by runtime."""
    tokens = _per_runtime_tokens(sessions)
    rows: list[CacheHealthProjection] = []
    for row in tokens:
        denom = row.cache_read_tokens + row.cache_create_tokens
        hit_ratio = row.cache_read_tokens / denom if denom else 0.0
        rows.append(
            CacheHealthProjection(
                runtime=row.runtime,
                cache_read_tokens=row.cache_read_tokens,
                cache_create_tokens=row.cache_create_tokens,
                hit_ratio=hit_ratio,
            )
        )
    return tuple(rows)


def _switchover_frequency(
    switches: list[TelemetryRuntimeSwitch],
) -> tuple[SwitchoverFrequencyProjection, ...]:
    """Aggregate runtime switchovers by cause."""
    counts: dict[RuntimeErrorClass, int] = defaultdict(int)
    for switch in switches:
        counts[switch.cause] += 1
    return tuple(
        SwitchoverFrequencyProjection(cause=cause, count=count)
        for cause, count in sorted(counts.items(), key=lambda item: item[0].value)
    )


__all__ = [
    "METRICS_PROJECTION_SCHEMA_VERSION",
    "CacheHealthProjection",
    "MetricsProjection",
    "MetricsWindow",
    "RoleCalibrationProjection",
    "RuntimeTokensProjection",
    "SwitchoverFrequencyProjection",
    "VarianceBucketProjection",
    "VarianceWaveProjection",
    "compute_metrics_projection",
]
