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


class EuBasis(StrEnum):
    """Captured quantity used to convert runtime counters into elapsed EU."""

    API_DURATION = "api_duration"
    TOKENS = "tokens"
    WALL_CLOCK = "wall_clock"


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
    eu_basis: EuBasis = EuBasis.API_DURATION
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


class ProseLevel(StrEnum):
    """Strictness ladder for the doc-clarity prose stack, loosest to strictest.

    The doc-clarity prose lints run at one of three escalating
    strictness levels. The order is load-bearing: it is what the
    authority guard compares so a local repo layer may only *tighten*
    (move toward :attr:`STRICT`), never *loosen* below the baseline the
    CI profile sets.

    - :attr:`LOOSE` — the managed-repo default. Prose lints run
      advisory-only; nothing blocks on a clarity finding.
    - :attr:`STANDARD` — the neutral middle. The deterministic prose
      lints block; the heavier checks (Vale, the LLM clarity judge)
      stay advisory.
    - :attr:`STRICT` — the agent-driven default. Every prose lint
      blocks and the LLM clarity-judge gate is on.
    """

    LOOSE = "loose"
    STANDARD = "standard"
    STRICT = "strict"


#: Strictness rank per :class:`ProseLevel` (higher == stricter). The
#: authority guard compares ranks so "tighten" / "loosen" is a numeric
#: ``>=`` rather than a brittle string comparison. Kept beside the enum so
#: a new level forces a matching rank entry (a missing key raises ``KeyError``
#: in :func:`prose_level_rank`).
_PROSE_LEVEL_RANK: dict[ProseLevel, int] = {
    ProseLevel.LOOSE: 0,
    ProseLevel.STANDARD: 1,
    ProseLevel.STRICT: 2,
}


def prose_level_rank(level: ProseLevel) -> int:
    """Return the strictness rank of *level* (higher is stricter).

    Args:
        level: The prose strictness level to rank.

    Returns:
        The integer rank — ``0`` for :attr:`ProseLevel.LOOSE` up to ``2``
        for :attr:`ProseLevel.STRICT`.

    Raises:
        KeyError: when *level* has no rank registered in
            :data:`_PROSE_LEVEL_RANK` (a programming error introduced by
            adding an enum member without a matching rank row).
    """
    return _PROSE_LEVEL_RANK[level]


class ProseConfig(BaseModel):
    """Strict typed model for the ``prose`` config section (doc-clarity).

    Mounts the operator-tunable knobs for the doc-clarity prose-lint
    stack. The single load-bearing field is :attr:`level`; the boolean
    overrides let a layer toggle an individual gate *within* the floor
    its :attr:`level` already implies, but they can never drop below it
    (the authority guard at :func:`assert_prose_not_weaker_than` owns the
    cross-layer "tighten-only" invariant).

    The default is :attr:`ProseLevel.STANDARD` so a repo that declares no
    ``prose`` block still gets the deterministic lints blocking; the
    agent-driven profile raises the floor to :attr:`ProseLevel.STRICT`
    and the managed profile relaxes it to :attr:`ProseLevel.LOOSE`.

    Attributes:
        level: The strictness floor for the whole prose stack. The
            authority guard rejects a local layer that sets this below
            the baseline level (typically the profile / CI layer's value).
        clarity_judge: Whether the Layer-3 LLM clarity judge runs as a
            gate. ``None`` (default) defers to the level (on at
            :attr:`ProseLevel.STRICT`, off otherwise); an explicit bool
            opts the gate on or off within the level's floor.
        block_on_lint: Whether the deterministic prose lints block
            (vs advisory). ``None`` (default) defers to the level (block
            at :attr:`ProseLevel.STANDARD` and above).
    """

    model_config = ConfigDict(extra="forbid")

    level: ProseLevel = ProseLevel.STANDARD
    clarity_judge: bool | None = None
    block_on_lint: bool | None = None

    @property
    def rank(self) -> int:
        """Strictness rank of this config's :attr:`level` (higher is stricter)."""
        return prose_level_rank(self.level)

    def tightens_or_equals(self, baseline: ProseConfig) -> bool:
        """Return whether this config is at least as strict as *baseline*.

        The cross-layer authority invariant in one boolean: a local layer
        is allowed iff its :attr:`level` is not below the baseline's.

        Args:
            baseline: The baseline config a local layer may only tighten
                (typically the profile / CI layer's resolved value).

        Returns:
            ``True`` when this config's level rank is ``>=`` the
            baseline's, i.e. it tightens or matches the baseline.
        """
        return self.rank >= baseline.rank


def assert_prose_not_weaker_than(baseline: ProseConfig, candidate: ProseConfig) -> ProseConfig:
    """Reject a *candidate* prose config that loosens below *baseline*.

    The doc-clarity authority guard: a local repo / user layer may only
    *tighten* the prose baseline the CI-side profile sets (agent-driven =
    strict, managed = loose), never loosen it. Tightening (raising the
    level toward :attr:`ProseLevel.STRICT`) and matching the baseline are
    both accepted; loosening (dropping the level rank) is rejected.

    Args:
        baseline: The baseline a local layer may not drop below — the
            value resolved from the profile / CI layer.
        candidate: The local-layer value to validate against the baseline.

    Returns:
        *candidate* unchanged when it tightens or matches *baseline* (so
        the call site can use the return value inline).

    Raises:
        ValueError: when *candidate*'s level is strictly looser than
            *baseline*'s — the message names both levels so the operator
            sees the floor they tripped.
    """
    if not candidate.tightens_or_equals(baseline):
        raise ValueError(
            f"local prose level {candidate.level.value!r} loosens below the "
            f"baseline {baseline.level.value!r}; local config may only tighten "
            f"the prose baseline, never loosen it"
        )
    return candidate


__all__ = [
    "AgentDrivenReleasePolicy",
    "AutoChoose",
    "BucketEstimateOverride",
    "BucketFitConfig",
    "CommitSubjectStyle",
    "EstimationConfig",
    "EstimationDisplayConfig",
    "EuBasis",
    "PreferencesConfig",
    "ProseConfig",
    "ProseLevel",
    "ReleaseCadence",
    "SolutionBias",
    "VcsConventionsConfig",
    "VcsReleaseConventionsConfig",
    "assert_prose_not_weaker_than",
    "prose_level_rank",
]
