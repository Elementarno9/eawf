"""Join telemetry session rows back to wave session attempts."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.models import Wave
from eawf.observability.telemetry.models import TelemetrySession

DEFAULT_EU_MINUTES = 30.0


class WaveAttemptRollup(BaseModel):
    """Telemetry rollup for one :class:`SessionAttempt` on a wave."""

    model_config = ConfigDict(extra="forbid")

    attempt: int = Field(ge=1)
    runtime: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    telemetry_session_id: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    attention_eu: float | None = Field(default=None, ge=0.0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    cost_usd: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))


class WaveSessionRollup(BaseModel):
    """Per-wave telemetry rollup joined from session-attempt rows."""

    model_config = ConfigDict(extra="forbid")

    wave_id: str = Field(min_length=1)
    attempts: list[WaveAttemptRollup] = Field(default_factory=list)
    duration_ms: int | None = Field(default=None, ge=0)
    attention_eu: float | None = Field(default=None, ge=0.0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    cost_usd: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))


def rollup_wave_sessions(
    wave: Wave,
    telemetry_sessions: list[TelemetrySession],
    *,
    eu_minutes: float = DEFAULT_EU_MINUTES,
) -> WaveSessionRollup:
    """Return telemetry joined to ``wave.sessions`` by runtime session id.

    Args:
        wave: State wave carrying daemon ``SessionAttempt`` rows.
        telemetry_sessions: Projected telemetry session rows.
        eu_minutes: Minutes represented by one effort unit.

    Returns:
        Per-attempt and aggregate rollup for telemetry rows whose
        ``session_id`` matches a wave attempt.

    Raises:
        ValueError: When *eu_minutes* is not positive.
    """
    if eu_minutes <= 0:
        raise ValueError(f"eu_minutes must be positive: {eu_minutes!r}")
    by_session_id = {row.session_id: row for row in telemetry_sessions}
    attempts: list[WaveAttemptRollup] = []
    for attempt_no, attempt in sorted(wave.sessions.items()):
        telemetry = by_session_id.get(attempt.session_id)
        if telemetry is None:
            continue
        attempts.append(
            _attempt_rollup(attempt_no, attempt.runtime, attempt.session_id, telemetry, eu_minutes)
        )
    return _wave_rollup(wave.id, attempts)


def _attempt_rollup(
    attempt_no: int,
    runtime: str,
    session_id: str,
    telemetry: TelemetrySession,
    eu_minutes: float,
) -> WaveAttemptRollup:
    """Build one attempt rollup from a matched telemetry session."""
    attention_eu = _duration_ms_to_eu(telemetry.duration_ms, eu_minutes=eu_minutes)
    return WaveAttemptRollup(
        attempt=attempt_no,
        runtime=runtime,
        session_id=session_id,
        telemetry_session_id=telemetry.session_id,
        duration_ms=telemetry.duration_ms,
        attention_eu=attention_eu,
        input_tokens=telemetry.total_input_tokens,
        output_tokens=telemetry.total_output_tokens,
        cache_read_tokens=telemetry.total_cache_read,
        cache_write_tokens=telemetry.total_cache_write,
        cost_usd=telemetry.total_cost_usd,
    )


def _wave_rollup(wave_id: str, attempts: list[WaveAttemptRollup]) -> WaveSessionRollup:
    """Fold attempt rollups into one wave rollup."""
    durations = [row.duration_ms for row in attempts if row.duration_ms is not None]
    attention = [row.attention_eu for row in attempts if row.attention_eu is not None]
    return WaveSessionRollup(
        wave_id=wave_id,
        attempts=attempts,
        duration_ms=sum(durations) if durations else None,
        attention_eu=sum(attention) if attention else None,
        input_tokens=sum(row.input_tokens for row in attempts),
        output_tokens=sum(row.output_tokens for row in attempts),
        cache_read_tokens=sum(row.cache_read_tokens for row in attempts),
        cache_write_tokens=sum(row.cache_write_tokens for row in attempts),
        cost_usd=sum((row.cost_usd for row in attempts), Decimal("0")),
    )


def _duration_ms_to_eu(duration_ms: int | None, *, eu_minutes: float) -> float | None:
    """Convert telemetry milliseconds to effort units."""
    if duration_ms is None:
        return None
    return duration_ms / (eu_minutes * 60_000.0)


__all__ = [
    "DEFAULT_EU_MINUTES",
    "WaveAttemptRollup",
    "WaveSessionRollup",
    "rollup_wave_sessions",
]
