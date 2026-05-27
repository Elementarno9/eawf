"""Strict typed models for layered configuration sections."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.state.enums import EffortBucket


class BucketEstimateOverride(BaseModel):
    """Operator-provided estimate centroid for one effort bucket."""

    model_config = ConfigDict(extra="forbid")

    expected_eu: float = Field(gt=0.0)
    pessimistic_eu: float | None = Field(default=None, gt=0.0)


class BucketFitConfig(BaseModel):
    """Config knobs for using fitted effort-bucket centroids."""

    model_config = ConfigDict(extra="forbid")

    overrides: dict[EffortBucket, BucketEstimateOverride] = Field(default_factory=dict)
    n_min: int = Field(default=5, ge=1)
    high_confidence_n: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def _validate_threshold_order(self) -> BucketFitConfig:
        if self.high_confidence_n < self.n_min:
            raise ValueError(
                f"bucket high_confidence_n must be >= n_min: "
                f"{self.high_confidence_n} < {self.n_min}"
            )
        return self


class EstimationDisplayConfig(BaseModel):
    """Display preferences under the ``estimation`` config section."""

    model_config = ConfigDict(extra="forbid")

    show_category: bool = False
    show_raw_eu: bool = True
    show_expected_time: bool = True
    show_pessimistic_time: bool = True
    eu_quantum: float = Field(default=0.25, gt=0.0)
    time_quantum_under_2h_minutes: int = Field(default=15, gt=0)
    time_quantum_over_2h_minutes: int = Field(default=30, gt=0)


class EstimationConfig(BaseModel):
    """Strict typed model for the ``estimation`` config section."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    eu_minutes: float = Field(default=30.0, gt=0.0)
    realtime_recalibration: bool = False
    calibration_profile: str = "eawf_v0_lockbox_2026_05"
    idle_policy: str = "D30_non_agent_gap"
    display: EstimationDisplayConfig = Field(default_factory=EstimationDisplayConfig)
    buckets: BucketFitConfig = Field(default_factory=BucketFitConfig)


__all__ = [
    "BucketEstimateOverride",
    "BucketFitConfig",
    "EstimationConfig",
    "EstimationDisplayConfig",
]
