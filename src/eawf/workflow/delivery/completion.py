"""What a Task must prove on the Batch base before it is finished.

A report is a claim about a workspace, and a workspace is not the Batch.
Completion therefore reads two durable facts instead of the claim: the
seal, which says a tree was accepted for integration, and the selected
generation whose work names this Task, which says the head everyone is
delivering actually carries it. Without either of them the Task does not
complete, whatever the executor reported.

The proof half is a reuse decision rather than a rerun of everything.
Between the generation that delivered a Task and the current head, other
Tasks land, and each of those integrations names the criteria it
invalidated. A criterion any of them names is required again at the
current head; a criterion none of them names stays proved where it was
proved, at the generation that delivered this Task. Cost therefore scales
with what moved rather than with how many Tasks the Batch holds.

The naming is not taken on trust. A generation's affected set digests to
the criterion component of that generation's own revision binding, so a
ledger line whose affected set was widened or narrowed after the fact no
longer agrees with the key it was written under, and is refused rather
than read. The line is checked the same way: a receipt bound to an
ordinal at a head the ledger does not record there was taken on a
generation that lost, and what it proved is about a tree nobody is
delivering.

Nothing here runs a gate, moves a Task or writes a record. The decision
names the legs to rerun and the legs that carry; performing the move is
the lifecycle machine's job, and it is denied outright while any leg is
still unproven.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from eawf.kernel.delivery.integration import (
    IntegrationGeneration,
    IntegrationGenerationLedger,
)
from eawf.kernel.delivery.receipts import (
    ProofReceipt,
    ReceiptReusePlan,
    RevisionBinding,
    RevisionRefKind,
    canonical_digest,
)
from eawf.kernel.runtime.candidate import DELIVERABLE_VERDICTS, CandidateBundle
from eawf.kernel.state.enums import AgentReportVerdict
from eawf.kernel.state.epoch2.base import StrictPositiveInt
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.store.kinds.gate_receipt import GateIdentityStr
from eawf.runtime.integration.apply import IntegrationRefusal
from eawf.runtime.verification.receipts import (
    ProofRuntimeFacts,
    VerificationLeg,
    build_verification_leg,
    decide_receipt_reuse,
)
from eawf.workflow.delivery.criteria import ExecutionContractSet

logger = logging.getLogger(__name__)


class CompletionRefusal(StrEnum):
    """The stable codes a Task completion is refused with.

    ``GENERATION_SUPERSEDED`` is bound to the integration vocabulary
    rather than re-spelled: a proof taken on a generation that lost and a
    delivery prepared on one are the same fact seen from two sides, and a
    caller that routes on the integration code must keep routing when the
    refusal arrives from here.
    """

    REPORT_UNSUCCESSFUL = "completion_report_unsuccessful"
    BUNDLE_UNSEALED = "completion_bundle_unsealed"
    GENERATION_UNSELECTED = "completion_generation_unselected"
    TASK_UNDELIVERED = "completion_task_undelivered"
    CRITERIA_UNCOVERED = "completion_criteria_uncovered"
    AFFECTED_SET_UNBOUND = "completion_affected_set_unbound"
    GENERATION_SUPERSEDED = IntegrationRefusal.GENERATION_SUPERSEDED


class CompletionRefusedError(ValueError):
    """One Task completion refused, with the code that says which rule.

    Attributes:
        code: The stable refusal code a caller routes on.
    """

    def __init__(self, code: CompletionRefusal, detail: str) -> None:
        """Keep the code beside the sentence an operator reads.

        Args:
            code: The stable refusal code.
            detail: One sentence naming what was refused and why.
        """
        self.code = code
        super().__init__(f"{code.value}: {detail}")


def affected_criterion_ids(generation: IntegrationGeneration) -> frozenset[str]:
    """Return the criteria *generation* invalidated, as its own key commits to.

    Args:
        generation: One selected generation of the Batch.

    Returns:
        The criterion ids the integration named affected.

    Raises:
        CompletionRefusedError: The named set does not digest to the
            criterion component of the generation's own revision binding,
            so the line claims an invalidation its freshness key never
            carried.
    """
    declared = tuple(generation.affected_criterion_ids)
    if canonical_digest(list(declared)) != generation.integrated_revision.criteria_digest:
        raise CompletionRefusedError(
            CompletionRefusal.AFFECTED_SET_UNBOUND,
            f"generation {generation.id} names {len(declared)} affected criteria that do not "
            "digest to the criterion component of its own revision binding",
        )
    return frozenset(declared)


def selected_line(
    ledger: IntegrationGenerationLedger, *, base: RevisionBinding
) -> Mapping[int, RevisionBinding]:
    """Return the revision the Batch records at each ordinal it has reached.

    Args:
        ledger: The Batch's generation history.
        base: The Batch base binding, which is the ordinal no integration
            produced.

    Returns:
        Ordinal to bound revision, base included.
    """
    line = {base.integration_generation: base}
    for item in ledger.generations:
        line[item.generation] = item.integrated_revision
    return line


def _refuse_superseded(
    receipts: Sequence[ProofReceipt],
    *,
    line: Mapping[int, RevisionBinding],
    batch_ref: str,
) -> None:
    """Refuse a proof anchored to a generation the Batch line does not record.

    Args:
        receipts: The receipts held for the Task.
        line: What the Batch records at each ordinal.
        batch_ref: The Batch being completed on.

    Raises:
        CompletionRefusedError: A receipt binds an ordinal of this Batch
            at a head the line does not carry there, so it proves
            something about a tree that is not being delivered.
    """
    for receipt in receipts:
        bound = receipt.freshness.revision_binding
        if bound.ref_kind is not RevisionRefKind.INTEGRATION or str(bound.batch_ref) != batch_ref:
            continue
        recorded = line.get(bound.integration_generation)
        if recorded is not None and recorded.head_sha == bound.head_sha:
            continue
        raise CompletionRefusedError(
            CompletionRefusal.GENERATION_SUPERSEDED,
            f"receipt {receipt.id} is bound to generation {bound.integration_generation} of "
            f"batch {bound.batch_ref.entity_key}, which is not the generation the Batch's "
            "selected line records at that ordinal",
        )


def _delivering_generation(
    ledger: IntegrationGenerationLedger, *, task_ref: str
) -> IntegrationGeneration:
    """Return the newest selected generation whose work names this Task.

    Args:
        ledger: The Batch's generation history.
        task_ref: The Task's canonical URN.

    Returns:
        The generation that carried the Task's work onto the Batch.

    Raises:
        CompletionRefusedError: No generation on the selected line names
            the Task, so its candidate was never integrated.
    """
    delivered = [
        item
        for item in ledger.generations
        if any(str(ref) == task_ref for ref in item.affected_task_refs)
    ]
    if not delivered:
        raise CompletionRefusedError(
            CompletionRefusal.TASK_UNDELIVERED,
            f"no selected generation of batch {ledger.batch_ref.entity_key} carries the work of "
            "this Task, so nothing integrated it",
        )
    return delivered[-1]


def _require_seal(
    task: Task, *, report_verdict: AgentReportVerdict, bundle: CandidateBundle | None
) -> CandidateBundle:
    """Return the sealed bundle a reported success must stand on.

    Args:
        task: The Task being completed.
        report_verdict: The verdict the Run's terminal report carried.
        bundle: The Task's sealed candidate, when one exists.

    Returns:
        The sealed bundle.

    Raises:
        CompletionRefusedError: The report does not propose the work for
            delivery, nothing sealed it, or the seal offered is about
            another Task.
    """
    if report_verdict not in DELIVERABLE_VERDICTS:
        raise CompletionRefusedError(
            CompletionRefusal.REPORT_UNSUCCESSFUL,
            f"the Run reported {report_verdict.value}, which does not propose its work for "
            "delivery",
        )
    if bundle is None:
        raise CompletionRefusedError(
            CompletionRefusal.BUNDLE_UNSEALED,
            f"the Run reported {report_verdict.value} but no candidate of task "
            f"{task.urn.entity_key} is sealed, so the report is a claim about a workspace",
        )
    if str(bundle.task_ref) != str(task.urn):
        raise CompletionRefusedError(
            CompletionRefusal.BUNDLE_UNSEALED,
            f"the sealed bundle offered for task {task.urn.entity_key} was sealed for task "
            f"{bundle.task_ref.entity_key}",
        )
    return bundle


def _require_coverage(task: Task, contracts: ExecutionContractSet) -> None:
    """Require one compiled contract family covering exactly the Task's criteria.

    Raises:
        CompletionRefusedError: A criterion the Task promised is covered
            by no gate, or the contracts were compiled for criteria the
            Task does not carry. Either way the reuse decision would be
            taken over the wrong set and an unproved promise would pass.
    """
    promised = {criterion.id for criterion in task.criteria}
    compiled = {criterion.id for criterion in contracts.criteria}
    gated = {criterion_id for item in contracts.contracts for criterion_id in item.criterion_ids}
    if promised != compiled:
        raise CompletionRefusedError(
            CompletionRefusal.CRITERIA_UNCOVERED,
            f"the compiled contracts carry {len(compiled)} criteria, not the {len(promised)} "
            f"task {task.urn.entity_key} promised",
        )
    ungated = sorted(promised - gated)
    if ungated:
        raise CompletionRefusedError(
            CompletionRefusal.CRITERIA_UNCOVERED,
            f"criteria {', '.join(ungated)} of task {task.urn.entity_key} are covered by no gate, "
            "so completion would reuse nothing and prove nothing for them",
        )


class TaskCompletionDecision(BaseModel):
    """Whether one Task's promises are proved on the Batch head, and where.

    Attributes:
        task_ref: The Task the decision is about.
        batch_ref: The Batch it is being completed on.
        head_generation: The ordinal that is the Batch head.
        delivering_generation: The ordinal whose work carried this Task.
        affected_criterion_ids: The criteria the integrations since that
            generation invalidated, sorted.
        plan: One reuse decision per required leg, each naming its reason.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_ref: str
    batch_ref: str
    head_generation: StrictPositiveInt
    delivering_generation: StrictPositiveInt
    affected_criterion_ids: tuple[GateIdentityStr, ...] = ()
    plan: ReceiptReusePlan

    @property
    def completable(self) -> bool:
        """Return whether every required leg is already proved at its binding."""
        return not self.plan.reruns and not self.plan.unavailable

    @property
    def rerun_gate_ids(self) -> tuple[str, ...]:
        """Return the gates that must run again before the Task may complete."""
        return tuple(item.gate_id for item in self.plan.reruns)

    @property
    def reused_gate_ids(self) -> tuple[str, ...]:
        """Return the gates a fresh receipt already answers."""
        return tuple(item.gate_id for item in self.plan.reused)

    @property
    def unavailable_gate_ids(self) -> tuple[str, ...]:
        """Return the judgment gates this deterministic path cannot settle."""
        return tuple(item.gate_id for item in self.plan.unavailable)

    @model_validator(mode="after")
    def _delivery_precedes_head(self) -> Self:
        """Require the delivering generation to sit at or below the head.

        Raises:
            ValueError: The Task's delivery is newer than the Batch head,
                which would mean reading a generation off a line the head
                is not on.
        """
        if self.delivering_generation > self.head_generation:
            raise ValueError(
                f"generation {self.delivering_generation} delivered the Task but the Batch head "
                f"is {self.head_generation}"
            )
        return self


def decide_task_completion(
    task: Task,
    *,
    report_verdict: AgentReportVerdict,
    bundle: CandidateBundle | None,
    ledger: IntegrationGenerationLedger,
    base: RevisionBinding,
    contracts: ExecutionContractSet,
    receipts: Sequence[ProofReceipt],
    facts: ProofRuntimeFacts,
    now: datetime | None = None,
    max_age: timedelta | None = None,
) -> TaskCompletionDecision:
    """Decide whether *task* may complete, and what it still has to prove.

    Args:
        task: The Task being completed.
        report_verdict: The verdict the Run's terminal report carried.
        bundle: The Task's sealed candidate, when one exists.
        ledger: The Batch's generation history.
        base: The Batch base binding, at the ordinal no integration made.
        contracts: The Task's criteria, compiled against their gates.
        receipts: Every proof receipt held for the Task, in any order.
        facts: The runtime half of every freshness key.
        now: The UTC decision time; required together with *max_age*.
        max_age: The oldest receipt age that may be reused.

    Returns:
        The decision: which gates carry, which must run again at the
        current head, and whether the Task is completable at all.

    Raises:
        CompletionRefusedError: The report proposes nothing, nothing
            sealed it, the Batch has selected no generation, no
            generation carries this Task's work, the contracts are not
            this Task's, a generation's affected set is unbound from its
            key, or a held proof is anchored to a generation the Batch
            line does not record.
        ValueError: The Task is unplaced, the ledger is another Batch's,
            or exactly one of *now* and *max_age* was given.
    """
    sealed = _require_seal(task, report_verdict=report_verdict, bundle=bundle)
    if task.batch_ref is None:
        raise ValueError(f"task {task.urn.entity_key!r} is unplaced and completes on no Batch")
    batch_ref = str(task.batch_ref)
    if str(ledger.batch_ref) != batch_ref:
        raise ValueError(
            f"the generation history is batch {ledger.batch_ref.entity_key!r}, not the Task's "
            f"{task.batch_ref.entity_key!r}"
        )
    _require_coverage(task, contracts)
    head = ledger.head
    if head is None:
        raise CompletionRefusedError(
            CompletionRefusal.GENERATION_UNSELECTED,
            f"batch {task.batch_ref.entity_key} has selected no integration generation, so the "
            f"tree that carries candidate {sealed.candidate_ref} is not the delivered one",
        )
    _refuse_superseded(receipts, line=selected_line(ledger, base=base), batch_ref=batch_ref)
    delivering = _delivering_generation(ledger, task_ref=str(task.urn))
    affected: frozenset[str] = frozenset()
    for item in ledger.generations:
        if item.generation > delivering.generation:
            affected |= affected_criterion_ids(item)
    legs = [
        build_verification_leg(
            contract,
            revision_binding=(
                head.integrated_revision
                if affected.intersection(contract.criterion_ids)
                else delivering.integrated_revision
            ),
            facts=facts,
        )
        for contract in contracts.contracts
    ]
    return _decision(
        task,
        legs=legs,
        receipts=receipts,
        head=head,
        delivering=delivering,
        affected=affected,
        now=now,
        max_age=max_age,
    )


def _decision(
    task: Task,
    *,
    legs: Sequence[VerificationLeg],
    receipts: Sequence[ProofReceipt],
    head: IntegrationGeneration,
    delivering: IntegrationGeneration,
    affected: frozenset[str],
    now: datetime | None,
    max_age: timedelta | None,
) -> TaskCompletionDecision:
    """Settle every required leg and wrap the outcome for the caller."""
    plan = decide_receipt_reuse(legs, receipts, now=now, max_age=max_age)
    decision = TaskCompletionDecision(
        task_ref=str(task.urn),
        batch_ref=str(head.batch_ref),
        head_generation=head.generation,
        delivering_generation=delivering.generation,
        affected_criterion_ids=tuple(sorted(affected)),
        plan=plan,
    )
    logger.info(
        f"decide_task_completion task={task.urn.entity_key} head={head.generation} "
        f"delivered_at={delivering.generation} affected={len(affected)} "
        f"reruns={len(plan.reruns)} reused={len(plan.reused)} "
        f"completable={decision.completable}"
    )
    return decision


__all__ = [
    "CompletionRefusal",
    "CompletionRefusedError",
    "TaskCompletionDecision",
    "affected_criterion_ids",
    "decide_task_completion",
    "selected_line",
]
