"""The observed outcome of each verification-site verdict, and the cohort it scores.

A Batch audit verdict predicts whether one criterion of one Batch holds. What
happened to that subject afterwards is read off the Batch ledger, where every
verification pass is filed as one cycle line:

- **refuted by repair**: a pass at or after the verdict opened a repair Task
  bounded to the verdict's criterion, so the subject did not hold;
- **refuted by head invalidation**: a later pass on a newer head no longer
  holds the verdict, because the head move invalidated its criterion;
- **held**: neither happened and the Batch merged, so the subject held.

Anything else is unsettled and carries no outcome, never a guessed one.

The cohort the jury is scored against joins each settled verdict to its ground
truth: the observed outcome (the silver tier), or the principal's gold label
for the subject, which overrides the outcome of the subject's newest verdict and
makes it scoreable even before it settles. Each verdict is one juror's ballot on
its own subject. An unverified verdict judged nothing, and a verdict no producer
answers for as ``(agent_role, runtime)`` cannot be scored against its producer,
so neither enters the cohort.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from eawf.kernel.delivery.batch_proof import (
    AuditVerdict,
    BatchAudit,
    BatchVerificationCycle,
    BatchVerificationStage,
)
from eawf.kernel.delivery.gold_label import AuditGoldLabel, AuditSubject
from eawf.kernel.delivery.integration import ConflictExitKind
from eawf.kernel.state.enums import AgentReportVerdict, AgentSessionRole
from eawf.observability.eval.jury import JurorBallot
from eawf.observability.eval.jury_validation import (
    LabeledVerdict,
    LabelSource,
    ValidationCohort,
)
from eawf.observability.eval.reputation import VerdictOutcome

logger = logging.getLogger(__name__)

#: The confidence a binary audit verdict states: it is a verdict, not a graded forecast.
BINARY_CONFIDENCE: Final = 1.0


class ObservedOutcome(StrEnum):
    """What happened to a verdict's subject after the verdict was reached."""

    HELD = "held"
    REFUTED = "refuted"


class OutcomeSource(StrEnum):
    """The ledger fact that settled an outcome."""

    MERGED = "merged"
    REPAIR = "repair"
    HEAD_INVALIDATION = "head_invalidation"


@dataclass(frozen=True, slots=True, kw_only=True)
class ObservedVerdict:
    """One verification-site verdict and what its subject went on to do.

    Attributes:
        audit: The verdict as the cycle that first held it filed it.
        outcome: What happened to the subject; ``None`` while unsettled.
        source: The fact that settled it; ``None`` while unsettled.
    """

    audit: BatchAudit
    outcome: ObservedOutcome | None
    source: OutcomeSource | None

    @property
    def subject(self) -> AuditSubject:
        """Return the subject the verdict judged: its Batch and criterion."""
        return (self.audit.batch_ref.entity_key, self.audit.criterion_id)

    @property
    def key(self) -> str:
        """Return the verdict's own key, the one its Trust row is listed under."""
        return f"{self.audit.batch_ref.entity_key}-{self.audit.id}"


def _repaired(line: BatchVerificationCycle, criterion_id: str) -> bool:
    """Return whether *line* opened a repair Task bounded to *criterion_id*."""
    return (
        line.stage is BatchVerificationStage.REPAIR
        and line.exit is not None
        and line.exit.kind is ConflictExitKind.REPAIR_TASK
        and criterion_id in line.blocking_criterion_ids
    )


def _settle(
    audit: BatchAudit, later: Sequence[BatchVerificationCycle], *, merged: bool
) -> tuple[ObservedOutcome | None, OutcomeSource | None]:
    """Return the outcome of *audit* given its Batch's cycle lines from its own on."""
    for line in later:
        if _repaired(line, audit.criterion_id):
            return ObservedOutcome.REFUTED, OutcomeSource.REPAIR
        moved = line.generation > audit.generation
        if moved and all(item.id != audit.id for item in line.audits):
            return ObservedOutcome.REFUTED, OutcomeSource.HEAD_INVALIDATION
    if merged:
        return ObservedOutcome.HELD, OutcomeSource.MERGED
    return None, None


def observe_verdict_outcomes(
    lines: Sequence[BatchVerificationCycle], *, merged_batches: frozenset[str]
) -> tuple[ObservedVerdict, ...]:
    """Return every verdict any cycle line held, each with its observed outcome.

    Args:
        lines: Every cycle line of the Batch ledger, in the order it was appended.
        merged_batches: The keys of the Batches that merged.

    Returns:
        One row per distinct verdict, Batch by Batch, each Batch's in the order its
        verdicts were first filed.
    """
    by_batch: dict[str, list[BatchVerificationCycle]] = {}
    for line in lines:
        by_batch.setdefault(line.batch_ref.entity_key, []).append(line)
    observed: list[ObservedVerdict] = []
    for batch_key, history in by_batch.items():
        seen: set[str] = set()
        for index, line in enumerate(history):
            for audit in line.audits:
                if audit.id in seen:
                    continue
                seen.add(audit.id)
                outcome, source = _settle(
                    audit, history[index:], merged=batch_key in merged_batches
                )
                observed.append(ObservedVerdict(audit=audit, outcome=outcome, source=source))
    logger.debug(
        f"observe_verdict_outcomes verdicts={len(observed)} "
        f"settled={sum(1 for item in observed if item.outcome is not None)}"
    )
    return tuple(observed)


def newest_by_subject(
    observed: Sequence[ObservedVerdict],
) -> Mapping[AuditSubject, ObservedVerdict]:
    """Return each subject's newest verdict, the one a gold label judges.

    Args:
        observed: The verdicts, each Batch's in the order they were first filed.

    Returns:
        The last verdict filed on each subject.
    """
    newest: dict[AuditSubject, ObservedVerdict] = {}
    for item in observed:
        newest[item.subject] = item
    return newest


def scoreable(item: ObservedVerdict, producers: Mapping[str, tuple[AgentSessionRole, str]]) -> bool:
    """Return whether the cohort can score *item*: it judged, and a producer answers for it.

    Args:
        item: The verdict.
        producers: The ``(agent_role, runtime)`` each reviewer Run answers for, by Run key.

    Returns:
        ``False`` for an unverified verdict or one no producer answers for.
    """
    return (
        item.audit.verdict is not AuditVerdict.UNVERIFIED
        and item.audit.review.run_ref.entity_key in producers
    )


def native_cohort(
    observed: Sequence[ObservedVerdict],
    *,
    producers: Mapping[str, tuple[AgentSessionRole, str]],
    labels: Mapping[AuditSubject, AuditGoldLabel],
) -> tuple[ValidationCohort, dict[str, tuple[JurorBallot, ...]]]:
    """Return the labelled cohort and the one ballot each of its verdicts cast.

    Args:
        observed: The verdicts with their observed outcomes.
        producers: The ``(agent_role, runtime)`` each reviewer Run answers for, by Run key.
        labels: The gold label in force for each labelled subject.

    Returns:
        The cohort, silver rows first-filed order and gold rows likewise, and the
        ballots keyed by verdict key, which each row's ``base_id`` names.
    """
    newest = newest_by_subject(observed)
    silver: list[LabeledVerdict] = []
    gold: list[LabeledVerdict] = []
    ballots: dict[str, tuple[JurorBallot, ...]] = {}
    for item in observed:
        audit = item.audit
        if not scoreable(item, producers):
            continue
        label = labels.get(item.subject) if newest[item.subject].key == item.key else None
        if label is None and item.outcome is None:
            continue
        role, runtime = producers[audit.review.run_ref.entity_key]
        verdict = (
            AgentReportVerdict.PASS
            if audit.verdict is AuditVerdict.VERIFIED_TRUE
            else AgentReportVerdict.FAIL
        )
        outcome = VerdictOutcome(
            base_id=item.key,
            agent_role=role,
            runtime=runtime,
            verdict=verdict,
            confidence=BINARY_CONFIDENCE,
            held=None if item.outcome is None else item.outcome is ObservedOutcome.HELD,
            outcome_source=None if item.source is None else item.source.value,
        )
        ballots[item.key] = (
            JurorBallot(
                juror_id=f"{role.value}-{runtime}"[:72],
                acceptance_style="binary",
                verdict=verdict,
                agent_role=role,
                runtime=runtime,
            ),
        )
        if label is not None:
            gold.append(
                LabeledVerdict(
                    outcome=outcome, ground_truth=label.ground_truth, label_source=LabelSource.GOLD
                )
            )
        else:
            silver.append(
                LabeledVerdict(
                    outcome=outcome,
                    ground_truth=item.outcome is ObservedOutcome.HELD,
                    label_source=LabelSource.SILVER,
                )
            )
    logger.debug(f"native_cohort silver={len(silver)} gold={len(gold)}")
    return ValidationCohort(silver=silver, gold=gold), ballots


__all__ = [
    "BINARY_CONFIDENCE",
    "ObservedOutcome",
    "ObservedVerdict",
    "OutcomeSource",
    "native_cohort",
    "newest_by_subject",
    "observe_verdict_outcomes",
    "scoreable",
]
