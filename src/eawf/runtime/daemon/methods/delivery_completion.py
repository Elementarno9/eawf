"""Judging whether one Task is finished on the Batch head it sits under.

``runtime.delivery.assess_completion``: whether one Task is finished.

The verb reads the durable half from the tree -- the Task, whether any of
its candidates sealed, and the Batch's generation history -- and is handed
the half no native record holds: the gates its criteria reference, the
proof receipts taken for them, and the runner and environment those
proofs ran under. A presented receipt buys nothing unless its freshness
key is the one the decision computes from the ledger, which the caller
does not get to choose.

It answers and does not move the Task. Performing the move belongs to
``domain.task.complete``, which consults the same judgment through
:func:`completion_binding` rather than being a second way to write one.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.delivery.adoption import ADOPTION_KEY_PREFIX, LandedAdoption
from eawf.kernel.delivery.integration import IntegrationGeneration
from eawf.kernel.delivery.receipts import ProofReceipt, RevisionBinding, canonical_digest
from eawf.kernel.runtime.candidate import CandidateBundle
from eawf.kernel.spec.common import GateSpec
from eawf.kernel.state.enums import AgentReportVerdict
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.state.epoch2.urns import BatchUrn, TaskUrn
from eawf.kernel.state.epoch2.values import ExactRevisionBinding
from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.delivery import (
    IdempotencyKey,
    read_generation_ledger,
    stored_tasks,
)
from eawf.runtime.daemon.native_dispatch import run_ledger
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_mutator
from eawf.runtime.verification.receipts import ProofRuntimeFacts
from eawf.workflow.delivery.completion import (
    CompletionRefusal,
    CompletionRefusedError,
    TaskCompletionDecision,
    decide_task_completion,
)
from eawf.workflow.delivery.criteria import (
    CriteriaAuthoring,
    CriteriaAuthoringError,
    ExecutionContractSet,
    compile_execution_contracts,
)

logger = logging.getLogger(__name__)


#: The verb that judges whether one Task is finished on the Batch head.
DELIVERY_ASSESS_COMPLETION_METHOD: Final = "runtime.delivery.assess_completion"

#: The refusal code of a completion naming a commit the Batch head is not.
COMPLETION_COMMIT_MISMATCH: Final = "completion_commit_mismatch"

#: The refusal code of a completion reusing a receipt no proof run filed.
COMPLETION_RECEIPT_UNFILED: Final = "completion_receipt_unfiled"

#: The receipt-ledger discriminator of one filed proof.
PROOF_PAYLOAD_KIND: Final = "proof_receipt"

#: The receipt-ledger key prefix a filed proof is kept under.
PROOF_KEY_PREFIX: Final = "PRF-"


def _refused(code: CompletionRefusal, detail: str) -> DaemonValidationError:
    """Return the wire form of one completion refusal."""
    return DaemonValidationError(f"validation_failed: {code.value}: {detail}")


class FiledProof(BaseModel):
    """One proof receipt the daemon took itself, as the receipt ledger holds it.

    Attributes:
        payload_kind: The discriminator separating a filed proof from the
            other lines of the receipt ledger.
        task_ref: The Task the proof was taken for.
        gate: The gate that ran, so a later assessment names the same gate
            without the caller restating it.
        receipt: The receipt, keyed on the exact leg it proved.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    payload_kind: Literal["proof_receipt"] = PROOF_PAYLOAD_KIND
    task_ref: TaskUrn
    gate: GateSpec
    receipt: ProofReceipt


def filed_proofs(session: RootSession, task_ref: TaskUrn) -> tuple[FiledProof, ...]:
    """Return every proof filed for *task_ref*, oldest first.

    Raises:
        ValidationError: A line claims to be a filed proof and does not
            validate as one, which means the ledger is corrupt.
    """
    wanted = str(task_ref)
    lines = read_ledger_records(session.ledger_path(Epoch2Collection.RECEIPT))
    proofs = (
        FiledProof.model_validate(item.payload)
        for item in lines
        if item.payload.get("payload_kind") == PROOF_PAYLOAD_KIND
    )
    return tuple(item for item in proofs if str(item.task_ref) == wanted)


def adoption_of(
    session: RootSession, *, batch_ref: BatchUrn, task_ref: str
) -> LandedAdoption | None:
    """Return the newest adoption of *batch_ref* that carries *task_ref*, if any.

    Raises:
        ValidationError: A line claims to be an adoption and does not
            validate as one, which means the ledger is corrupt.
    """
    wanted = str(batch_ref)
    adopted = [
        adoption
        for item in read_ledger_records(session.ledger_path(Epoch2Collection.BATCH))
        if item.record_key.startswith(ADOPTION_KEY_PREFIX)
        and item.payload.get("batch_ref") == wanted
        and (adoption := LandedAdoption.model_validate(item.payload)).carries(task_ref)
    ]
    return adopted[-1] if adopted else None


class TaskCompletionInputs(BaseModel):
    """The half of a completion judgment no native record holds.

    Attributes:
        base: The revision the Batch starts from, which is the ordinal no
            integration produced.
        report_verdict: The verdict the Run's terminal report carried.
        gates: The gates the Task's criteria reference. Named by the
            caller because a Task record carries its criteria and not the
            gates they are proved by.
        receipts: The proof receipts held for the Task. Presented rather
            than read because no native record holds one yet; a receipt
            counts only where its freshness key is the one the decision
            derives from the ledger.
        proof_facts: The runner, environment, selector and policy those
            proofs ran under.
    """

    model_config = ConfigDict(extra="forbid")

    base: RevisionBinding
    report_verdict: AgentReportVerdict
    gates: tuple[GateSpec, ...] = Field(min_length=1)
    receipts: tuple[ProofReceipt, ...] = ()
    proof_facts: ProofRuntimeFacts


class TaskCompletionParams(TaskCompletionInputs):
    """Params of :data:`DELIVERY_ASSESS_COMPLETION_METHOD`.

    Attributes:
        urn: The Task being judged.
        actor: Who asked.
        idempotency_key: The client's name for this request.
    """

    urn: TaskUrn
    actor: PrincipalKey
    idempotency_key: IdempotencyKey


class TaskCompletionAnswer(BaseModel):
    """What one completion assessment answers with.

    Attributes:
        task_ref: The Task that was judged.
        batch_ref: The Batch it is being completed on.
        head_generation: The ordinal that is the Batch head.
        delivering_generation: The ordinal whose work carried this Task.
        affected_criterion_ids: The criteria the integrations since that
            generation invalidated.
        rerun_gate_ids: The gates that must run again at the head.
        reused_gate_ids: The gates a fresh receipt already answers.
        unavailable_gate_ids: The judgment gates this path cannot settle.
        completable: Whether every required leg is already proved.
        reason: One sentence an operator reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_ref: str
    batch_ref: str
    head_generation: int
    delivering_generation: int
    affected_criterion_ids: tuple[str, ...] = ()
    rerun_gate_ids: tuple[str, ...] = ()
    reused_gate_ids: tuple[str, ...] = ()
    unavailable_gate_ids: tuple[str, ...] = ()
    completable: bool
    reason: str


def _completion_params(params: dict[str, Any]) -> TaskCompletionParams:
    """Validate request params, dropping the key the fence already used.

    Raises:
        DaemonValidationError: The request does not parse. The pydantic
            detail is reduced to field paths so the refusal never repeats
            a submitted value into a log.
    """
    try:
        return TaskCompletionParams.model_validate(
            {key: value for key, value in params.items() if key != REPO_ROOT_PARAM}
        )
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        raise DaemonValidationError(
            f"validation_failed: schema_validation_failed: check {', '.join(fields)}"
        ) from error


def task_of(session: RootSession, urn: TaskUrn) -> Task:
    """Return the Task *urn* names, from whichever tier holds it.

    Raises:
        DaemonValidationError: No Task of that key lives in this tree.
    """
    wanted = str(urn)
    for task in stored_tasks(session):
        if str(task.urn) == wanted:
            return task
    raise DaemonValidationError(
        f"validation_failed: task_absent: no task {urn.entity_key} lives in this tree"
    )


def sealed_bundle_of(
    records: Sequence[LedgerRecord], *, task_ref: TaskUrn
) -> CandidateBundle | None:
    """Return the sealed candidate of *task_ref*, or ``None`` when none sealed.

    A candidate's identity is derived from its Task and the tree it
    produced, so a Task that resubmitted the same tree has one bundle
    however many Runs produced it. Where two trees did seal, the newest
    line is the one the Batch integrated.

    Raises:
        ValidationError: A line claims to be a bundle and does not
            validate as one, which means the ledger is corrupt.
    """
    wanted = str(task_ref)
    sealed = [
        bundle
        for item in records
        if item.payload.get("payload_kind") == "candidate_bundle"
        and str((bundle := CandidateBundle.model_validate(item.payload)).task_ref) == wanted
    ]
    return sealed[-1] if sealed else None


def compiled_contracts(task: Task, gates: Sequence[GateSpec]) -> ExecutionContractSet:
    """Compile the Task's own criteria against the gates the request names.

    Raises:
        DaemonValidationError: The criteria and gates do not clear the
            authoring floor, so nothing could be proved about them.
    """
    try:
        return compile_execution_contracts(
            CriteriaAuthoring(
                scope_id=task.urn.entity_key, criteria=task.criteria, gates=tuple(gates)
            )
        )
    except CriteriaAuthoringError as error:
        raise _refused(
            CompletionRefusal.CRITERIA_UNCOVERED,
            f"the criteria of task {task.urn.entity_key} and the gates named for them do not "
            f"compile: {error}",
        ) from error


@dataclass(frozen=True, slots=True)
class _CompletionJudgment:
    """Everything one completion assessment read and decided.

    Attributes:
        task: The Task that was judged.
        decision: Which legs carry and which must run again.
        head: The generation the Batch head stands on.
        contracts: The Task's criteria compiled against the named gates.
        filed: The freshness keys of every proof the daemon filed for it.
    """

    task: Task
    decision: TaskCompletionDecision
    head: IntegrationGeneration
    contracts: ExecutionContractSet
    filed: frozenset[str]


def _judge_completion(
    context: Epoch2RootContext, args: TaskCompletionParams
) -> _CompletionJudgment:
    """Read the tree and decide whether one Task is finished on its Batch head.

    Raises:
        DaemonValidationError: The Task is absent or unplaced, its
            criteria and gates do not compile, its reported success is
            unsealed, its Batch has selected no generation, no generation
            carries its work, or a presented proof is anchored to a
            generation the Batch line does not record.
    """
    with context.session([str(args.urn)]) as session:
        task = task_of(session, args.urn)
        if task.batch_ref is None:
            raise _refused(
                CompletionRefusal.TASK_UNDELIVERED,
                f"task {task.urn.entity_key} is unplaced, so no Batch carries its work",
            )
        bundle: CandidateBundle | LandedAdoption | None = sealed_bundle_of(
            read_ledger_records(run_ledger(session)), task_ref=task.urn
        ) or adoption_of(session, batch_ref=task.batch_ref, task_ref=str(task.urn))
        filed = {item.receipt.freshness_key for item in filed_proofs(session, task.urn)}
        ledger = read_generation_ledger(session.ledger_path(Epoch2Collection.BATCH), task.batch_ref)
    contracts = compiled_contracts(task, args.gates)
    try:
        decision = decide_task_completion(
            task,
            report_verdict=args.report_verdict,
            bundle=bundle,
            ledger=ledger,
            base=args.base,
            contracts=contracts,
            receipts=args.receipts,
            facts=args.proof_facts,
        )
    except CompletionRefusedError as error:
        raise DaemonValidationError(f"validation_failed: {error}") from error
    # decide_task_completion refuses a ledger with no head, so one exists here.
    head = ledger.head
    assert head is not None, "a decided completion always stands on a selected generation"
    return _CompletionJudgment(
        task=task, decision=decision, head=head, contracts=contracts, filed=frozenset(filed)
    )


def _completion_answer(judgment: _CompletionJudgment) -> TaskCompletionAnswer:
    """Return the answer one completion judgment stands for."""
    task, decision = judgment.task, judgment.decision
    reason = (
        f"task {task.urn.entity_key} is proved on generation {decision.head_generation}"
        if decision.completable
        else (
            f"task {task.urn.entity_key} needs {len(decision.rerun_gate_ids)} gate(s) proved on "
            f"generation {decision.head_generation} before it completes"
        )
    )
    logger.info(
        f"assess_task_completion task={task.urn.entity_key} head={decision.head_generation} "
        f"completable={decision.completable}"
    )
    return TaskCompletionAnswer(
        task_ref=decision.task_ref,
        batch_ref=decision.batch_ref,
        head_generation=decision.head_generation,
        delivering_generation=decision.delivering_generation,
        affected_criterion_ids=decision.affected_criterion_ids,
        rerun_gate_ids=decision.rerun_gate_ids,
        reused_gate_ids=decision.reused_gate_ids,
        unavailable_gate_ids=decision.unavailable_gate_ids,
        completable=decision.completable,
        reason=reason,
    )


def assess_task_completion(
    context: Epoch2RootContext, args: TaskCompletionParams
) -> TaskCompletionAnswer:
    """Judge whether one Task is finished on the Batch head it sits under.

    Args:
        context: The native context of the tree the Task lives in.
        args: The validated request.

    Returns:
        Which gates carry, which must run again at the head, and whether
        the Task may complete at all.

    Raises:
        DaemonValidationError: The Task is absent or unplaced, its
            criteria and gates do not compile, its reported success is
            unsealed, its Batch has selected no generation, no generation
            carries its work, or a presented proof is anchored to a
            generation the Batch line does not record.
    """
    return _completion_answer(_judge_completion(context, args))


def completion_binding(
    context: Epoch2RootContext, args: TaskCompletionParams, *, integrated_commit: str
) -> tuple[TaskCompletionAnswer, ExactRevisionBinding | None]:
    """Judge one Task and derive the exact binding its completion records.

    Only a receipt the daemon filed from its own proof run counts toward
    the binding, so a caller cannot complete a Task on a receipt it wrote.
    The binding is derived rather than presented: the head and tree are
    the Batch head generation's own, the contract digest covers the
    compiled contracts the proofs were judged against, and the evidence
    digest covers exactly the receipts the decision reused. A caller
    therefore names only the commit it believes landed, and that name is
    checked against the head rather than trusted.

    Args:
        context: The native context of the tree the Task lives in.
        args: The validated assessment request.
        integrated_commit: The commit the caller says carries the Task.

    Returns:
        The assessment answer beside the binding, or beside ``None`` when
        a required leg is still unproven at its binding.

    Raises:
        DaemonValidationError: The assessment refused, or the named commit
            is not the head the Batch's selected generation delivers.
    """
    judgment = _judge_completion(context, args)
    answer = _completion_answer(judgment)
    revision = judgment.head.integrated_revision
    if revision.head_sha != integrated_commit:
        raise DaemonValidationError(
            f"validation_failed: {COMPLETION_COMMIT_MISMATCH}: batch "
            f"{judgment.head.batch_ref.entity_key} stands on generation "
            f"{judgment.head.generation} at {revision.head_sha}, not on {integrated_commit}"
        )
    if not answer.completable:
        return answer, None
    unfiled = sorted(
        item.gate_id
        for item in judgment.decision.plan.reused
        if item.expected_freshness_key not in judgment.filed
    )
    if unfiled:
        raise DaemonValidationError(
            f"validation_failed: {COMPLETION_RECEIPT_UNFILED}: gate(s) {', '.join(unfiled)} of "
            f"task {judgment.task.urn.entity_key} are answered by receipts no proof run filed"
        )
    reused = sorted(
        (
            {
                "gate_id": item.gate_id,
                "receipt_id": item.receipt_id,
                "freshness_key": item.expected_freshness_key,
            }
            for item in judgment.decision.plan.reused
        ),
        key=lambda row: str(row["gate_id"]),
    )
    binding = ExactRevisionBinding(
        head_sha=revision.head_sha,
        tree_sha=revision.tree_sha,
        contract_digest=canonical_digest(judgment.contracts.model_dump(mode="json")),
        policy_revision=judgment.task.contract_revision,
        evidence_digest=canonical_digest(reused),
    )
    return answer, binding


@native_mutator(DELIVERY_ASSESS_COMPLETION_METHOD)
async def _assess_task_completion(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Judge whether one Task may move to COMPLETED on its Batch head."""
    args = _completion_params(params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(assess_task_completion, context, args)
    return answer.model_dump(mode="json")


__all__ = [
    "COMPLETION_COMMIT_MISMATCH",
    "COMPLETION_RECEIPT_UNFILED",
    "DELIVERY_ASSESS_COMPLETION_METHOD",
    "PROOF_KEY_PREFIX",
    "PROOF_PAYLOAD_KIND",
    "FiledProof",
    "TaskCompletionAnswer",
    "TaskCompletionInputs",
    "TaskCompletionParams",
    "adoption_of",
    "assess_task_completion",
    "completion_binding",
    "filed_proofs",
]
