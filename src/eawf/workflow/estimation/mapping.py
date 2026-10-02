"""The effort-unit mapping: one typed policy record that every estimate cites.

Effort is one constant per wave, but that constant is a policy, not a
literal. :class:`EffortMapping` carries it together with its revision,
its digest, its measured dispersion and the rules calibration runs under.
An estimate records the revision in force when it was made, so a later
re-fit changes what future estimates say and never what an earlier one
predicted.

The pessimistic value is the mapping's declared upper bound, the measured
p90. It is never derived by scaling the expected value, because a
multiple applied by convention says nothing about the observed tail.

The shipped mapping, :data:`CURRENT_EFFORT_MAPPING`, is a proposal default
pending re-fit: it was measured once, and calibration has not yet had the
sample it needs to confirm or replace it.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from eawf.kernel.state.models import EstimateSummary


class MappingStatus(StrEnum):
    """How much evidence a mapping revision stands on."""

    PROPOSAL_DEFAULT = "proposal_default"
    FITTED = "fitted"


class ExclusionReason(StrEnum):
    """Why calibration left a recorded actual out of its sample."""

    FLAGGED_EXCLUDED = "flagged_excluded"
    NOT_DONE = "not_done"
    NOT_POSITIVE = "not_positive_elapsed"
    UNREADABLE = "unreadable"


class _MappingRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EffortDispersion(_MappingRecord):
    """The measured spread of per-wave effort, in minutes.

    Attributes:
        p10: The tenth percentile.
        p50: The median.
        p90: The ninetieth percentile, which is the declared upper bound.
    """

    p10: Annotated[float, Field(gt=0.0)]
    p50: Annotated[float, Field(gt=0.0)]
    p90: Annotated[float, Field(gt=0.0)]

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        """Refuse percentiles out of order.

        Raises:
            ValueError: ``p10 <= p50 <= p90`` does not hold.
        """
        if not self.p10 <= self.p50 <= self.p90:
            raise ValueError(
                f"dispersion out of order: p10={self.p10} p50={self.p50} p90={self.p90}"
            )
        return self


class ExclusionCount(_MappingRecord):
    """How many rows one exclusion reason removed from a fit.

    Attributes:
        reason: Why the rows were left out.
        count: How many were.
    """

    reason: ExclusionReason
    count: Annotated[StrictInt, Field(ge=1)]


class MappingFit(_MappingRecord):
    """The amendment a re-fit records: what it fitted on and how well.

    Attributes:
        fitted_on: The date the fit ran.
        input_sample: The subjects whose actuals the fit used.
        residuals_eu: Each sampled actual minus the fitted constant, in
            the order of *input_sample*.
        excluded: The rows left out, counted per reason.
    """

    fitted_on: date
    input_sample: Annotated[tuple[str, ...], Field(min_length=1)]
    residuals_eu: tuple[float, ...]
    excluded: tuple[ExclusionCount, ...] = ()

    @model_validator(mode="after")
    def _one_residual_per_sample(self) -> Self:
        """Refuse a fit whose residuals do not match its sample.

        Raises:
            ValueError: The residual count differs from the sample count.
        """
        if len(self.residuals_eu) != len(self.input_sample):
            raise ValueError(
                f"{len(self.residuals_eu)} residuals for a sample of {len(self.input_sample)}"
            )
        return self


class EffortMapping(_MappingRecord):
    """One revision of the effort-unit mapping.

    Attributes:
        revision: Bumped on every change; a revision is a new mapping.
        status: Whether the revision is the shipped proposal or a fit.
        effective_on: The date the revision came into force.
        effort_eu: The effort of one wave, in EU.
        eu_minutes: The minutes of agent session time one EU stands for.
        dispersion: The measured spread of per-wave effort, in minutes.
        minimum_reference_sample: The fewest reference rows an estimate
            may rest on before it renders as unavailable.
        refit_cadence_days: The days that must pass after
            *effective_on* before a re-fit is due.
        refit_minimum_sample: The fewest eligible actuals a re-fit needs.
        refit_threshold: The largest relative change of *effort_eu* a
            re-fit applies without an operator decision.
        fit: The amendment a fitted revision was produced by.
    """

    revision: Annotated[StrictInt, Field(ge=1)]
    status: MappingStatus
    effective_on: date
    effort_eu: Annotated[float, Field(gt=0.0)]
    eu_minutes: Annotated[float, Field(gt=0.0)]
    dispersion: EffortDispersion
    minimum_reference_sample: Annotated[StrictInt, Field(ge=1)]
    refit_cadence_days: Annotated[StrictInt, Field(ge=1)]
    refit_minimum_sample: Annotated[StrictInt, Field(ge=1)]
    refit_threshold: Annotated[float, Field(gt=0.0)]
    fit: MappingFit | None = None

    @model_validator(mode="after")
    def _fit_matches_status(self) -> Self:
        """Require a fit on a fitted revision and none on a proposal.

        Raises:
            ValueError: A fitted revision names no fit, or a proposal
                default claims one.
        """
        if (self.status is MappingStatus.FITTED) != (self.fit is not None):
            raise ValueError(f"a {self.status.value} mapping revision cannot carry fit={self.fit}")
        return self

    @property
    def digest(self) -> str:
        """The ``sha256:`` digest of the revision, so a silent edit is detectable."""
        # The lifecycle modules import this one while the CLI tree is built,
        # and the compiled runtime models behind the digest are not cheap.
        from eawf.kernel.runtime.compiled import canonical_digest

        return canonical_digest(self.model_dump(mode="json"))

    @property
    def effort_minutes(self) -> float:
        """The expected minutes of one wave."""
        return self.effort_eu * self.eu_minutes

    @property
    def pessimistic_minutes(self) -> float:
        """The declared upper bound of one wave, in minutes: the measured p90."""
        return self.dispersion.p90

    @property
    def pessimistic_eu(self) -> float:
        """The declared upper bound of one wave, in EU."""
        return self.dispersion.p90 / self.eu_minutes


#: The shipped mapping: one constant of 0.8 EU, 24 minutes, with the
#: dispersion measured when the five-point ladder was retired. Revision 1
#: was that ladder.
CURRENT_EFFORT_MAPPING: Final[EffortMapping] = EffortMapping(
    revision=2,
    status=MappingStatus.PROPOSAL_DEFAULT,
    effective_on=date(2026, 9, 3),
    effort_eu=0.8,
    eu_minutes=30.0,
    dispersion=EffortDispersion(p10=8.8, p50=23.9, p90=123.1),
    minimum_reference_sample=20,
    refit_cadence_days=30,
    refit_minimum_sample=100,
    refit_threshold=0.25,
)


class UnavailableReason(StrEnum):
    """Why an estimate renders as unavailable rather than as a number."""

    NO_REFERENCE_CLASS = "no_reference_class"
    NO_SAMPLE_SIZE = "no_sample_size"
    SAMPLE_BELOW_MINIMUM = "sample_below_minimum"
    NO_MAPPING_REVISION = "no_mapping_revision"


def estimate_unavailable_reason(
    estimate: EstimateSummary, mapping: EffortMapping
) -> UnavailableReason | None:
    """Return why *estimate* is not a number a reader may use, if it is not.

    An estimate is usable only when it names the reference class it drew
    from, the sample size behind it and the mapping revision it was made
    under, and the sample reaches the mapping's declared minimum. Anything
    less is false precision.

    Args:
        estimate: The estimate to judge.
        mapping: The mapping whose minimum sample applies.

    Returns:
        The first reason the estimate fails, or ``None`` when it is usable.
    """
    if not estimate.reference_class:
        return UnavailableReason.NO_REFERENCE_CLASS
    if estimate.reference_sample_size is None:
        return UnavailableReason.NO_SAMPLE_SIZE
    if estimate.mapping_revision is None:
        return UnavailableReason.NO_MAPPING_REVISION
    if estimate.reference_sample_size < mapping.minimum_reference_sample:
        return UnavailableReason.SAMPLE_BELOW_MINIMUM
    return None


__all__ = [
    "CURRENT_EFFORT_MAPPING",
    "EffortDispersion",
    "EffortMapping",
    "ExclusionCount",
    "ExclusionReason",
    "MappingFit",
    "MappingStatus",
    "UnavailableReason",
    "estimate_unavailable_reason",
]
