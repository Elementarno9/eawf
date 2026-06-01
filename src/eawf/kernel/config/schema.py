"""Strict typed models for layered configuration sections."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.state.enums import EffortBucket

CommitSubjectStyle = Literal["bracket", "trailer"]
ReleaseCadence = Literal["manual", "per-phase"]
AgentDrivenReleasePolicy = Literal["manual", "per-phase"]


class SolutionBias(StrEnum):
    """Planner bias toward solution complexity under ``preferences``.

    The planner consults this preference when sizing a wave DAG: a
    ``SIMPLE`` bias favours fewer, smaller waves (lean toward YAGNI),
    ``THOROUGH`` favours broader coverage, and ``BALANCED`` is the
    neutral default.
    """

    SIMPLE = "simple"
    BALANCED = "balanced"
    THOROUGH = "thorough"


class AutoChoose(StrEnum):
    """Whether an ``AskUserQuestion`` auto-picks its recommended option.

    Mirrors the closed ``ask | auto | never``-style ladders used by the
    other operator-gate preferences (e.g. ``vcs.auto_commit``):

    - :attr:`OFF` — never auto-pick; always surface the question (default).
    - :attr:`RECOMMENDED` — auto-pick only when the surface marks one
      option as recommended; otherwise surface the question.
    - :attr:`ALWAYS` — auto-pick the recommended option whenever one
      exists, surfacing nothing.
    """

    OFF = "off"
    RECOMMENDED = "recommended"
    ALWAYS = "always"


class VcsReleaseConventionsConfig(BaseModel):
    """Release cadence conventions under ``vcs.conventions``."""

    model_config = ConfigDict(extra="forbid")

    cadence: ReleaseCadence = "manual"
    agent_driven: AgentDrivenReleasePolicy = "per-phase"


class VcsConventionsConfig(BaseModel):
    """VCS commit-message convention preferences."""

    model_config = ConfigDict(extra="forbid")

    subject_style: CommitSubjectStyle = "bracket"
    wave_trailer: str = Field(default="Eawf-Wave", min_length=1)
    release: VcsReleaseConventionsConfig = Field(default_factory=VcsReleaseConventionsConfig)


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


class PreferencesConfig(BaseModel):
    """Strict typed model for the ``preferences`` config section.

    Operator-tunable planner + AskUserQuestion defaults. Every field is a
    closed enum so an unknown value fails validation at the loader
    boundary. These keys ADD the validated preference surface; the
    planner / AUQ consumers read them in a later wave.
    """

    model_config = ConfigDict(extra="forbid")

    solution_bias: SolutionBias = SolutionBias.BALANCED
    scope_size: EffortBucket = EffortBucket.M
    auto_choose: AutoChoose = AutoChoose.OFF


__all__ = [
    "AgentDrivenReleasePolicy",
    "AutoChoose",
    "BucketEstimateOverride",
    "BucketFitConfig",
    "CommitSubjectStyle",
    "EstimationConfig",
    "EstimationDisplayConfig",
    "PreferencesConfig",
    "ReleaseCadence",
    "SolutionBias",
    "VcsConventionsConfig",
    "VcsReleaseConventionsConfig",
]
