"""Wave effort-bucket roll-up helpers."""

from __future__ import annotations

from datetime import datetime

from eawf.state.enums import EffortBucket
from eawf.state.models import Wave

BUCKET_EU: dict[EffortBucket, float] = {
    EffortBucket.XS: 0.25,
    EffortBucket.S: 0.5,
    EffortBucket.M: 1.0,
    EffortBucket.L: 2.0,
    EffortBucket.XL: 3.5,
}

EU_MINUTES = 30.0


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
