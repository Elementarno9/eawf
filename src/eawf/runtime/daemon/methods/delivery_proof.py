"""Proving a Task at the binding it completes on, and assembling its assessment.

``runtime.delivery.prove_task``: run the Task's gates where they count.

A Task completes only on receipts keyed to the exact generation each leg
binds, so a receipt taken anywhere else -- the executor's workspace, an
earlier head, a wave close -- proves nothing the completion reads. This
verb computes those legs itself, checks the bound commit out into a
detached worktree of the tree's own repository, runs each deterministic
gate there through the same out-of-process runner a wave close uses, and
files one receipt per leg, passing or not. A leg already proved by a
passing filed receipt is not run again.

``runtime.delivery.task_assessment``: the document ``task complete`` needs.

The verb reads and writes nothing. It gathers what the completion
judgment is presented -- the base the Batch started from, the verdict of
the seal or adoption the Task stands on, the gates and receipts its proof
runs filed, and this daemon's own runtime facts -- runs the judgment, and
answers the verdict beside the document, so an operator never assembles
one by hand.

The runtime half of a freshness key is this daemon's own: the selector is
the gate set, the policy is the gates' policies, the runner is the
interpreter and the eawf version, and the environment is the platform. A
daemon upgraded between the proof and the assessment therefore asks for
the legs to be proved again, which is the point of keying a proof on its
runner.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from eawf._version import __version__
from eawf.kernel.delivery.adoption import LandedAdoption
from eawf.kernel.delivery.receipts import ProofReceipt, canonical_digest
from eawf.kernel.identity import EntityKind, IdentityError, format_entity_key
from eawf.kernel.spec.common import GateSpec
from eawf.kernel.state.enums import GateReceiptResult
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey
from eawf.kernel.state.epoch2.urns import TaskUrn
from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.gate_execution import (
    GateChildCrashError,
    GateExecutionContext,
    run_gate_out_of_process,
)
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.delivery import (
    IdempotencyKey,
    file_keyed_answer,
    keyed_answer,
    read_generation_ledger,
)
from eawf.runtime.daemon.methods.delivery_completion import (
    PROOF_KEY_PREFIX,
    PROOF_PAYLOAD_KIND,
    FiledProof,
    TaskCompletionAnswer,
    TaskCompletionInputs,
    TaskCompletionParams,
    adoption_of,
    compiled_contracts,
    completion_binding,
    filed_proofs,
    sealed_bundle_of,
    task_of,
)
from eawf.runtime.daemon.native_dispatch import run_ledger
from eawf.runtime.daemon.native_guard import native_mutator, native_params
from eawf.runtime.verification.receipts import ProofRuntimeFacts, VerificationLeg
from eawf.workflow.audit_dsl.models import CheckResult
from eawf.workflow.delivery.completion import CompletionRefusedError, completion_legs
from eawf.workflow.delivery.criteria import ExecutionContractSet

logger = logging.getLogger(__name__)


#: The verb that runs a Task's gates at the binding each leg is judged at.
DELIVERY_PROVE_METHOD: Final = "runtime.delivery.prove_task"

#: The verb that answers the completion assessment document of one Task.
DELIVERY_TASK_ASSESSMENT_METHOD: Final = "runtime.delivery.task_assessment"

#: Where a proof run's checkouts and gate claims live while it runs.
_PROOF_DIRNAME: Final = "proof-runs"


class TaskProveParams(BaseModel):
    """Params of :data:`DELIVERY_PROVE_METHOD`.

    Attributes:
        urn: The Task being proved.
        actor: Who asked.
        idempotency_key: The client's name for this request.
        gates: The gates the Task's criteria reference. Omitted, the gates
            of the Task's earlier proof runs are run again.
    """

    model_config = ConfigDict(extra="forbid")

    urn: TaskUrn
    actor: PrincipalKey
    idempotency_key: IdempotencyKey
    gates: tuple[GateSpec, ...] = ()


class ProvedLeg(BaseModel):
    """What one leg of a proof run did.

    Attributes:
        gate_id: The gate the leg ran.
        head_sha: The commit it ran at.
        result: The receipt's result, or ``reused`` when a passing filed
            receipt already proved the leg.
        receipt_id: The receipt that proves or refutes it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate_id: str
    head_sha: str
    result: str
    receipt_id: str


class TaskProveAnswer(BaseModel):
    """What one proof run answers with.

    Attributes:
        task_ref: The Task that was proved.
        legs: Every leg, in contract order.
        passed: Whether every leg now stands on a passing receipt.
        reason: One sentence an operator reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_ref: str
    legs: tuple[ProvedLeg, ...]
    passed: bool
    reason: str


class TaskAssessmentParams(BaseModel):
    """Params of :data:`DELIVERY_TASK_ASSESSMENT_METHOD`.

    Attributes:
        urn: The Task being assessed.
        actor: Who asked.
    """

    model_config = ConfigDict(extra="forbid")

    urn: TaskUrn
    actor: PrincipalKey


class TaskAssessmentAnswer(BaseModel):
    """The verdict of one assessment beside the document that produced it.

    Attributes:
        answer: The completion judgment.
        integrated_commit: The head the Batch's selected generation stands on.
        assessment: The document ``domain.task.complete`` is presented.
        integrated_binding: The binding a completion would record, or
            ``None`` while a leg is unproven.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    answer: TaskCompletionAnswer
    integrated_commit: str
    assessment: dict[str, Any]
    integrated_binding: dict[str, Any] | None = None


def daemon_proof_facts(contracts: ExecutionContractSet) -> ProofRuntimeFacts:
    """Return the runtime half of a freshness key, as this daemon observes it.

    Args:
        contracts: The Task's compiled contracts.

    Returns:
        The selector, policy, runner and environment digests.
    """
    gates = sorted(contracts.contracts, key=lambda item: item.gate_id)
    return ProofRuntimeFacts(
        selector_digest=canonical_digest([item.gate_id for item in gates]),
        policy_digest=canonical_digest([[item.gate_id, item.gate.policy] for item in gates]),
        runner_digest=canonical_digest(
            {
                "python": platform.python_version(),
                "implementation": platform.python_implementation(),
                "eawf": __version__,
            }
        ),
        environment_digest=canonical_digest(
            {"platform": sys.platform, "machine": platform.machine()}
        ),
    )


def _latest_gates(proofs: Sequence[FiledProof]) -> tuple[GateSpec, ...]:
    """Return the newest filed spec of every gate the proofs ran."""
    by_id = {item.gate.id: item.gate for item in proofs}
    return tuple(by_id[key] for key in sorted(by_id))


def _next_receipt_ordinal(session: RootSession) -> int:
    """Return the ordinal the next receipt this root files is numbered by.

    A receipt key is scoped to the repository, so the numbering counts the
    filed proofs of every Task, and it is read inside the session the new
    lines are appended under so two proof runs cannot take one number.

    Raises:
        ValidationError: A line claims to be a filed proof and does not
            validate as one, which means the ledger is corrupt.
    """
    lines = read_ledger_records(session.ledger_path(Epoch2Collection.RECEIPT))
    taken = [
        int(FiledProof.model_validate(item.payload).receipt.id.split("-")[1])
        for item in lines
        if item.payload.get("payload_kind") == PROOF_PAYLOAD_KIND
    ]
    return max(taken, default=0) + 1


@contextmanager
def _checkout(repository: Path, *, head_sha: str, where: Path) -> Iterator[Path]:
    """Check *head_sha* out into a detached worktree that is gone on exit.

    Raises:
        DaemonValidationError: The repository does not hold the commit.
    """
    added = subprocess.run(
        ["git", "-C", str(repository), "worktree", "add", "--detach", str(where), head_sha],
        capture_output=True,
        text=True,
        check=False,
    )
    if added.returncode != 0:
        raise DaemonValidationError(
            f"validation_failed: proof_checkout_failed: the repository cannot check out "
            f"{head_sha} ({added.stderr.strip()[:200]})"
        )
    try:
        yield where
    finally:
        subprocess.run(
            ["git", "-C", str(repository), "worktree", "remove", "--force", str(where)],
            capture_output=True,
            check=False,
        )
        shutil.rmtree(where, ignore_errors=True)


def _run_leg(leg: VerificationLeg, *, cwd: Path, gate_context: GateExecutionContext) -> CheckResult:
    """Run one leg's gate in *cwd* and return what the gate did.

    Raises:
        DaemonValidationError: The gate crashed before producing a result.
    """
    contract = leg.contract
    assert contract.check is not None, "only a deterministic leg is run"
    try:
        result = run_gate_out_of_process(
            contract.check,
            cwd=cwd,
            context=gate_context,
            criterion_id=contract.gate.criterion_id,
            gate_id=contract.gate_id,
        )
    except (GateChildCrashError, ValueError) as error:
        raise DaemonValidationError(
            f"validation_failed: proof_gate_crashed: gate {contract.gate_id} crashed before it "
            f"produced a result ({error!s})"
        ) from error
    return result


def _receipt(
    leg: VerificationLeg, result: CheckResult, *, ordinal: int, now: datetime
) -> ProofReceipt:
    """Return the receipt one run of *leg* earns, numbered *ordinal*.

    Raises:
        DaemonValidationError: The repository's receipt key space is full.
    """
    contract = leg.contract
    try:
        receipt_id = format_entity_key(EntityKind.RECEIPT, ordinal)
    except IdentityError as error:
        raise DaemonValidationError(
            f"validation_failed: receipt_key_space_saturated: {error}"
        ) from error
    key = leg.expected.digest()
    started = result.started_at or now
    return ProofReceipt(
        id=receipt_id,
        scope_id=contract.scope_id,
        gate_id=contract.gate_id,
        criterion_ids=contract.criterion_ids,
        evidence_kind=contract.evidence_kind,
        freshness=leg.expected,
        freshness_key=key,
        result=GateReceiptResult.PASS if result.passed else GateReceiptResult.FAIL,
        exit_status=result.exit_status,
        started_at=started,
        ended_at=max(result.ended_at or now, started),
    )


def _proof_line(task_ref: TaskUrn, gate: GateSpec, receipt: ProofReceipt) -> LedgerRecord:
    """Return the receipt-ledger line one proof is filed as."""
    proof = FiledProof(task_ref=task_ref, gate=gate, receipt=receipt)
    return LedgerRecord(
        collection=Epoch2Collection.RECEIPT,
        record_key=f"{PROOF_KEY_PREFIX}{receipt.freshness_key[:16]}-{task_ref.entity_key}",
        status=receipt.result.value,
        recorded_at=receipt.ended_at,
        payload=proof.model_dump(mode="json"),
    )


def _proof_legs(
    context: Epoch2RootContext, urn: TaskUrn, gates: Sequence[GateSpec]
) -> tuple[list[VerificationLeg], dict[str, FiledProof], tuple[GateSpec, ...]]:
    """Read the tree and return the Task's legs, its filed proofs, and its gates.

    Raises:
        DaemonValidationError: The Task is absent or unplaced, names no
            gate, its criteria do not compile, or no generation carries it.
    """
    with context.session([str(urn)]) as session:
        task = task_of(session, urn)
        if task.batch_ref is None:
            raise DaemonValidationError(
                f"validation_failed: completion_task_undelivered: task {urn.entity_key} is "
                "unplaced, so no Batch carries its work"
            )
        proofs = filed_proofs(session, urn)
        ledger = read_generation_ledger(session.ledger_path(Epoch2Collection.BATCH), task.batch_ref)
    named = tuple(gates) or _latest_gates(proofs)
    if not named:
        raise DaemonValidationError(
            f"validation_failed: proof_gates_unnamed: task {urn.entity_key} has no filed proof "
            "to take its gates from, so name them"
        )
    contracts = compiled_contracts(task, named)
    try:
        legs, _, _ = completion_legs(
            task, ledger=ledger, contracts=contracts, facts=daemon_proof_facts(contracts)
        )
    except CompletionRefusedError as error:
        raise DaemonValidationError(f"validation_failed: {error}") from error
    return legs, {item.receipt.freshness_key: item for item in proofs}, named


def prove_task(
    context: Epoch2RootContext, args: TaskProveParams, *, now: datetime
) -> TaskProveAnswer:
    """Run every unproven deterministic leg of one Task and file its receipt.

    Args:
        context: The native context of the tree the Task lives in.
        args: The validated request.
        now: The stamp a receipt falls back to when the runner gives none.

    Returns:
        Every leg's result, and whether all of them now pass.

    Raises:
        DaemonValidationError: The legs cannot be computed, a commit
            cannot be checked out, or a gate crashed.
    """
    params = args.model_dump(mode="json")
    replayed = keyed_answer(
        context, method=DELIVERY_PROVE_METHOD, key=args.idempotency_key, params=params
    )
    if replayed is not None:
        logger.info(f"prove_task task={args.urn.entity_key} replayed=True")
        return TaskProveAnswer.model_validate(replayed)
    legs, filed, _ = _proof_legs(context, args.urn, args.gates)
    repository = context.identity.tree_root.parent
    run_dir = context.identity.tree_root / "local" / _PROOF_DIRNAME / uuid.uuid4().hex
    gate_context = GateExecutionContext(
        state_path=run_dir / "claims" / "state.json", attempt_id=run_dir.name
    )
    ran: list[tuple[VerificationLeg, ProofReceipt | CheckResult]] = []
    try:
        for leg in legs:
            if leg.contract.check is None:
                continue
            standing = filed.get(leg.expected.digest())
            if standing is not None and standing.receipt.result is GateReceiptResult.PASS:
                ran.append((leg, standing.receipt))
                continue
            head = leg.expected.revision_binding.head_sha
            with _checkout(repository, head_sha=head, where=run_dir / head[:12]) as cwd:
                ran.append((leg, _run_leg(leg, cwd=cwd, gate_context=gate_context)))
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    outcomes = _file_proofs(context, args.urn, ran, now=now)
    passed = bool(outcomes) and all(item.result in {"pass", "reused"} for item in outcomes)
    logger.info(
        f"prove_task task={args.urn.entity_key} legs={len(outcomes)} "
        f"ran={sum(1 for item in outcomes if item.result != 'reused')} passed={passed}"
    )
    answer = TaskProveAnswer(
        task_ref=str(args.urn),
        legs=tuple(outcomes),
        passed=passed,
        reason=(
            f"every leg of task {args.urn.entity_key} stands on a passing receipt"
            if passed
            else f"task {args.urn.entity_key} has a leg that did not pass; fix it and prove again"
        ),
    )
    file_keyed_answer(
        context,
        method=DELIVERY_PROVE_METHOD,
        key=args.idempotency_key,
        params=params,
        answer=answer.model_dump(mode="json"),
        at=now,
    )
    return answer


def _file_proofs(
    context: Epoch2RootContext,
    urn: TaskUrn,
    ran: Sequence[tuple[VerificationLeg, ProofReceipt | CheckResult]],
    *,
    now: datetime,
) -> list[ProvedLeg]:
    """File a receipt for every leg that ran and return every leg's outcome.

    A leg paired with a receipt was already proved by that filed receipt
    and is reported as reused; a leg paired with a gate result is numbered
    and filed here, inside the one session every new line is appended under.

    Raises:
        DaemonValidationError: The repository's receipt key space is full.
    """
    outcomes: list[ProvedLeg] = []
    with context.session([str(urn)]) as session:
        ordinal = _next_receipt_ordinal(session)
        for leg, done in ran:
            if isinstance(done, ProofReceipt):
                receipt, result = done, "reused"
            else:
                receipt = _receipt(leg, done, ordinal=ordinal, now=now)
                ordinal += 1
                commit_ledger_append(session, _proof_line(urn, leg.contract.gate, receipt))
                result = receipt.result.value
            outcomes.append(
                ProvedLeg(
                    gate_id=leg.contract.gate_id,
                    head_sha=leg.expected.revision_binding.head_sha,
                    result=result,
                    receipt_id=receipt.id,
                )
            )
    return outcomes


def task_assessment(context: Epoch2RootContext, args: TaskAssessmentParams) -> TaskAssessmentAnswer:
    """Assemble and judge the completion document of one Task.

    Args:
        context: The native context of the tree the Task lives in.
        args: The validated request.

    Returns:
        The judgment, the head it stands on, the document that produced it
        and, when every leg is proved, the binding a completion records.

    Raises:
        DaemonValidationError: The Task stands on no seal or adoption, no
            generation carries it, no proof run filed its gates, or the
            judgment refused.
    """
    _, filed, gates = _proof_legs(context, args.urn, ())
    with context.session([str(args.urn)]) as session:
        task = task_of(session, args.urn)
        assert task.batch_ref is not None, "the legs were computed for a placed Task"
        proposal = sealed_bundle_of(
            read_ledger_records(run_ledger(session)), task_ref=task.urn
        ) or adoption_of(session, batch_ref=task.batch_ref, task_ref=str(task.urn))
        ledger = read_generation_ledger(session.ledger_path(Epoch2Collection.BATCH), task.batch_ref)
    if proposal is None:
        raise DaemonValidationError(
            f"validation_failed: completion_bundle_unsealed: task {args.urn.entity_key} stands "
            "on no sealed candidate and no adoption"
        )
    head = ledger.head
    assert head is not None, "the legs were computed on a selected generation"
    verdict = proposal.report_verdict if isinstance(proposal, LandedAdoption) else proposal.verdict
    inputs = TaskCompletionInputs(
        base=ledger.generations[0].source_base,
        report_verdict=verdict,
        gates=gates,
        receipts=tuple(item.receipt for item in filed.values()),
        proof_facts=daemon_proof_facts(compiled_contracts(task, gates)),
    )
    completion = TaskCompletionParams.model_validate(
        {**inputs.model_dump(mode="json"), "urn": str(args.urn), "actor": args.actor}
    )
    commit = head.integrated_revision.head_sha
    answer, binding = completion_binding(context, completion, integrated_commit=commit)
    return TaskAssessmentAnswer(
        answer=answer,
        integrated_commit=commit,
        assessment=inputs.model_dump(mode="json"),
        integrated_binding=None if binding is None else binding.model_dump(mode="json"),
    )


@native_mutator(DELIVERY_PROVE_METHOD)
async def _prove_task(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Run one Task's unproven legs and file their receipts."""
    args = native_params(TaskProveParams, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(prove_task, context, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


@native_mutator(DELIVERY_TASK_ASSESSMENT_METHOD)
async def _task_assessment(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Answer one Task's completion judgment beside its assessment document."""
    args = native_params(TaskAssessmentParams, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(task_assessment, context, args)
    return answer.model_dump(mode="json")


__all__ = [
    "DELIVERY_PROVE_METHOD",
    "DELIVERY_TASK_ASSESSMENT_METHOD",
    "ProvedLeg",
    "TaskAssessmentAnswer",
    "TaskAssessmentParams",
    "TaskProveAnswer",
    "TaskProveParams",
    "daemon_proof_facts",
    "prove_task",
    "task_assessment",
]
