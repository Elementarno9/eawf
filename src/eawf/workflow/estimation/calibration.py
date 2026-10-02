"""Calibration: re-fit the effort-unit mapping against recorded actuals.

A re-fit reads the recorded actuals and nothing else. It never touches an
estimate: re-fitting changes the mapping future estimates cite, and every
estimate already made keeps the revision it recorded.

The fit runs only when it is due. The mapping declares a cadence in days
and a minimum sample of eligible actuals; short of either the outcome is
``not_due`` and names which precondition failed, because a fit over a thin
sample stamps a new revision on noise.

Every row the fit leaves out is counted under the reason it was left out,
so no fit is reported over an unstated sample.

A fit that moves the effort constant by no more than the mapping's
threshold applies as the next revision with a notice. A larger move is
not applied: the outcome carries the question the operator decides, with
the two answers it offers.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Mapping
from datetime import date, timedelta
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from eawf.kernel.state.enums import ActualStatus
from eawf.kernel.state.epoch2.pending_action import (
    OptionEffect,
    PendingActionOption,
    TermExpansion,
)
from eawf.kernel.state.models import ActualSummary
from eawf.workflow.estimation.mapping import (
    EffortDispersion,
    EffortMapping,
    ExclusionCount,
    ExclusionReason,
    MappingFit,
    MappingStatus,
)


class RefitDisposition(StrEnum):
    """What a re-fit concluded."""

    NOT_DUE = "not_due"
    APPLY = "apply"
    NEEDS_DECISION = "needs_decision"


class NotDueReason(StrEnum):
    """Which precondition kept a re-fit from running."""

    CADENCE_NOT_ELAPSED = "cadence_not_elapsed"
    SAMPLE_BELOW_MINIMUM = "sample_below_minimum"


class RefitOutcome(BaseModel):
    """The result of one re-fit attempt.

    Attributes:
        disposition: What the re-fit concluded.
        current_revision: The revision the re-fit started from.
        current_digest: That revision's digest.
        eligible_count: How many actuals the fit could use.
        excluded: The actuals left out, counted per reason.
        not_due: The preconditions that failed; empty unless ``not_due``.
        proposed: The fitted next revision; ``None`` when not due.
        relative_change: How far the fit moves the effort constant,
            relative to the current one; ``None`` when not due.
        notice: The notice an applied revision is announced with.
        question: The decision an over-threshold fit asks the operator.
        options: The answers that question offers.
        recommended_option_id: The answer recommended.
        recommendation_rationale: Its one-sentence reason.
        terms: What each abbreviation the question shows stands for.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: RefitDisposition
    current_revision: int
    current_digest: str
    eligible_count: int
    excluded: tuple[ExclusionCount, ...]
    not_due: tuple[NotDueReason, ...] = ()
    proposed: EffortMapping | None = None
    relative_change: float | None = None
    notice: str | None = None
    question: str | None = None
    options: tuple[PendingActionOption, ...] = ()
    recommended_option_id: str | None = None
    recommendation_rationale: str | None = None
    terms: tuple[TermExpansion, ...] = ()


#: What the effort unit stands for, shown beside a question that names it.
_EU_TERM: Final = TermExpansion(
    term="EU", expansion="an effort unit, a fixed span of agent session minutes"
)

#: Why adopting a fit is recommended: it rests on the minimum sample or more.
_ADOPT_RATIONALE: Final = (
    "The fit rests on at least the declared minimum of measured waves, "
    "so it is better evidence than the current constant."
)


def _exclusion(actual: ActualSummary) -> ExclusionReason | None:
    """Return why *actual* cannot calibrate, or ``None`` when it can."""
    if actual.calibration_excluded:
        return ExclusionReason.FLAGGED_EXCLUDED
    if actual.status is not ActualStatus.DONE:
        return ExclusionReason.NOT_DONE
    if actual.elapsed_eu <= 0.0:
        return ExclusionReason.NOT_POSITIVE
    return None


def _fitted(
    mapping: EffortMapping,
    sample: list[tuple[str, float]],
    excluded: tuple[ExclusionCount, ...],
    today: date,
) -> EffortMapping:
    """Return the next revision fitted to *sample*, a list of (subject, elapsed EU)."""
    elapsed = [eu for _, eu in sample]
    effort_eu = statistics.median(elapsed)
    deciles = statistics.quantiles(
        [eu * mapping.eu_minutes for eu in elapsed], n=10, method="inclusive"
    )
    return mapping.model_copy(
        update={
            "revision": mapping.revision + 1,
            "status": MappingStatus.FITTED,
            "effective_on": today,
            "effort_eu": effort_eu,
            "dispersion": EffortDispersion(p10=deciles[0], p50=deciles[4], p90=deciles[8]),
            "fit": MappingFit(
                fitted_on=today,
                input_sample=tuple(subject for subject, _ in sample),
                residuals_eu=tuple(eu - effort_eu for eu in elapsed),
                excluded=excluded,
            ),
        }
    )


def _decision_options(
    current: EffortMapping, proposed: EffortMapping
) -> tuple[PendingActionOption, ...]:
    """Return the two answers an over-threshold re-fit offers."""
    return (
        PendingActionOption(
            option_id="adopt_refit",
            label=f"Adopt revision {proposed.revision}",
            effect=OptionEffect.APPROVE,
            consequence=(
                f"Future estimates cite revision {proposed.revision} at "
                f"{proposed.effort_eu:.3f} EU per wave; existing estimates keep theirs."
            ),
            cost=f"Revision {current.revision} stops being the constant new estimates use.",
            preview=(
                f"revision {current.revision} ({current.effort_eu:.3f} EU) -> "
                f"revision {proposed.revision} ({proposed.effort_eu:.3f} EU)"
            ),
        ),
        PendingActionOption(
            option_id="keep_current",
            label=f"Keep revision {current.revision}",
            effect=OptionEffect.DECLINE,
            consequence=(
                f"Estimates keep citing revision {current.revision} at "
                f"{current.effort_eu:.3f} EU per wave."
            ),
            cost="The measured drift stays unapplied until the next re-fit.",
            preview=f"revision {current.revision} ({current.effort_eu:.3f} EU) stays in force",
        ),
    )


def refit_mapping(
    mapping: EffortMapping,
    actuals: Mapping[str, ActualSummary],
    *,
    today: date,
    unreadable: int = 0,
) -> RefitOutcome:
    """Re-fit *mapping* against *actuals* and say what should happen.

    The fitted constant is the median elapsed EU of the eligible actuals,
    and its dispersion their deciles in minutes. Nothing is written: the
    caller applies the proposed revision or files the question.

    Args:
        mapping: The revision in force.
        actuals: The recorded actuals, keyed by subject.
        today: The date the re-fit runs on.
        unreadable: Recorded actual rows that did not read back as an
            actual, counted among the exclusions.

    Returns:
        ``not_due`` when the cadence has not elapsed or the eligible sample
        is below the mapping's minimum; ``apply`` when the fit moves the
        constant by at most the threshold; ``needs_decision`` otherwise.
    """
    sample: list[tuple[str, float]] = []
    reasons: Counter[ExclusionReason] = Counter({ExclusionReason.UNREADABLE: unreadable})
    for subject, actual in sorted(actuals.items()):
        reason = _exclusion(actual)
        if reason is None:
            sample.append((subject, actual.elapsed_eu))
        else:
            reasons[reason] += 1
    excluded = tuple(
        ExclusionCount(reason=reason, count=reasons[reason])
        for reason in ExclusionReason
        if reasons[reason]
    )
    base: dict[str, Any] = {
        "current_revision": mapping.revision,
        "current_digest": mapping.digest,
        "eligible_count": len(sample),
        "excluded": excluded,
    }
    not_due: list[NotDueReason] = []
    if today < mapping.effective_on + timedelta(days=mapping.refit_cadence_days):
        not_due.append(NotDueReason.CADENCE_NOT_ELAPSED)
    if len(sample) < mapping.refit_minimum_sample:
        not_due.append(NotDueReason.SAMPLE_BELOW_MINIMUM)
    if not_due:
        return RefitOutcome(disposition=RefitDisposition.NOT_DUE, not_due=tuple(not_due), **base)
    proposed = _fitted(mapping, sample, excluded, today)
    # rounded so a move of exactly the threshold is not pushed past it by float error
    change = round(abs(proposed.effort_eu - mapping.effort_eu) / mapping.effort_eu, 9)
    summary = (
        f"re-fit over {len(sample)} actuals moves the effort constant from "
        f"{mapping.effort_eu:.3f} to {proposed.effort_eu:.3f} EU ({change:.1%})"
    )
    if change <= mapping.refit_threshold:
        return RefitOutcome(
            disposition=RefitDisposition.APPLY,
            proposed=proposed,
            relative_change=change,
            notice=f"effort mapping revision {proposed.revision} applied: {summary}",
            **base,
        )
    return RefitOutcome(
        disposition=RefitDisposition.NEEDS_DECISION,
        proposed=proposed,
        relative_change=change,
        question=(
            f"A {summary}, past the {mapping.refit_threshold:.0%} threshold. "
            f"Adopt revision {proposed.revision}?"
        ),
        options=_decision_options(mapping, proposed),
        recommended_option_id="adopt_refit",
        recommendation_rationale=_ADOPT_RATIONALE,
        terms=(_EU_TERM,),
        **base,
    )


__all__ = [
    "NotDueReason",
    "RefitDisposition",
    "RefitOutcome",
    "refit_mapping",
]
