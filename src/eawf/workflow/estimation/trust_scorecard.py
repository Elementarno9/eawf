"""Trust scorecard metrics for estimation calibration."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.models import State
from eawf.workflow.estimation.buckets import calibrate_buckets

SCORECARD_SCHEMA_VERSION: Literal[1] = 1


class EuCalibrationMetric(BaseModel):
    """EU calibration row for the trust scorecard."""

    model_config = ConfigDict(extra="forbid")

    sample_count: int = Field(ge=0)
    nudged_bucket_count: int = Field(ge=0)
    max_drift_pct: float | None
    bucket_drift: bool
    drift_badge: Literal["ok", "bucket-drift", "no-data"]


class TrustScorecard(BaseModel):
    """Top-level trust scorecard payload."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = SCORECARD_SCHEMA_VERSION
    eu_calibration: EuCalibrationMetric


def compute_eu_calibration_metric(
    state: State,
    *,
    now: datetime | None = None,
) -> EuCalibrationMetric:
    """Return the bucket-drift verdict from ``calibrate_buckets``."""
    report = calibrate_buckets(state, now=now)
    populated = [row for row in report.buckets if row.sample_count > 0]
    nudged = [row for row in populated if row.nudge]
    max_drift = max((row.drift_pct or 0.0 for row in populated), default=None)
    bucket_drift = bool(nudged)
    if bucket_drift:
        badge: Literal["ok", "bucket-drift", "no-data"] = "bucket-drift"
    elif populated:
        badge = "ok"
    else:
        badge = "no-data"
    return EuCalibrationMetric(
        sample_count=sum(row.sample_count for row in populated),
        nudged_bucket_count=len(nudged),
        max_drift_pct=max_drift,
        bucket_drift=bucket_drift,
        drift_badge=badge,
    )


def compute_trust_scorecard(
    state: State,
    *,
    now: datetime | None = None,
) -> TrustScorecard:
    """Compute the estimation trust scorecard from a typed state snapshot."""
    return TrustScorecard(
        schema_version=SCORECARD_SCHEMA_VERSION,
        eu_calibration=compute_eu_calibration_metric(state, now=now),
    )


__all__ = [
    "SCORECARD_SCHEMA_VERSION",
    "EuCalibrationMetric",
    "TrustScorecard",
    "compute_eu_calibration_metric",
    "compute_trust_scorecard",
]
