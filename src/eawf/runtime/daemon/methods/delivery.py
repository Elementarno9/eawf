"""Integrating a Batch, judging a Task on the result, and verifying the Batch.

``runtime.delivery.integrate``: one Batch's sealed work, one delivery.

The verb reads the Batch's sealed candidates off the run ledger, puts
them in the one order their content decides, and turns them into the
delivery the repository's configured commit unit asks for -- a single
squashed commit under ``batch``, one per Task under ``task``. It then
runs the integration: the tree is materialized, the candidates are
applied and the commit is authored with no canonical lock held, and only
the selection of the new generation as the Batch head is taken under it.

Two halves of the request cannot be derived and are therefore named by
the caller. The subject lines are prose, and prose has an author; the
exit references are records the daemon does not mint, and a conflict
frame with no exit is the one thing a blocked attempt must never leave
behind. Everything else -- which candidates, in what order, under which
ordinal, against which base -- is read or derived.

The tree half has no implementation yet. Nothing in the tree resolves a
candidate's submission artifact back to bytes, so there is no honest way
for the daemon to reproduce a worker's tree in a workspace of its own,
and :data:`INTEGRATION_WORKSPACE` is therefore unset. The verb refuses
with a typed code rather than reporting an integration that touched no
tree; everything before and after that seam is real and runs.

``runtime.delivery.assess_completion``: whether one Task is finished.

The verb reads the durable half from the tree -- the Task, whether any of
its candidates sealed, and the Batch's generation history -- and is handed
the half no native record holds: the gates its criteria reference, the
proof receipts taken for them, and the runner and environment those
proofs ran under. No native record holds a proof receipt yet, so
presenting them is the only way to ask the question at all; a presented
receipt buys nothing unless its freshness key is the one the decision
computes from the ledger, which the caller does not get to choose.

It answers and does not move the Task. Performing the move belongs to the
lifecycle machine, which has no Task completion verb yet, so this is the
proof edge that verb will consult rather than a second way to write one.

``runtime.delivery.verify_batch``: whether the Batch may be merged.

The verb reads the durable half from the tree -- the head the Batch
actually delivers, the Runs that sealed its candidates, and the cycle it
last stood in -- and is handed the half no native record holds: the
independent verdicts. Nothing produces a
:class:`~eawf.kernel.delivery.batch_proof.BatchAudit` yet, so presenting
them is the only way to ask the question; a presented row buys nothing
unless its reviewer clears independence against the producing Runs read
from the ledger, which the caller does not get to choose.

The head is read rather than named. A cycle filed on an ordinal the Batch
has since passed is re-opened on the current one, and what that move
invalidated is taken from the moving generations' own affected sets, so
an audit the move did not touch keeps its verdict instead of being swept
away with the rest. That is also what takes a Batch back out of merge
readiness: readiness is a claim about one exact head, and the head moved.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from eawf.kernel.config.layered import merge_config
from eawf.kernel.delivery.batch_proof import (
    BatchAudit,
    BatchVerificationCycle,
    BatchVerificationStage,
    HeadInvalidation,
)
from eawf.kernel.delivery.integration import (
    ConflictExitKind,
    ConflictFile,
    IntegrationAttempt,
    IntegrationAttemptStatus,
    IntegrationConflict,
    IntegrationGeneration,
    IntegrationGenerationLedger,
)
from eawf.kernel.delivery.receipts import ProofReceipt, RevisionBinding, canonical_digest
from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.runtime.candidate import CandidateBundle, CandidateId
from eawf.kernel.spec.common import GateSpec
from eawf.kernel.state.enums import AgentReportVerdict
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import BranchName, PrincipalKey, StrictNonNegativeInt
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.state.epoch2.urns import AnyEntityUrn, BatchUrn, EvidenceUrn, TaskUrn
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.kinds.gate_receipt import GateIdentityStr
from eawf.kernel.store.ledger import (
    LedgerRecord,
    append_ledger_record,
    effective_records,
    read_ledger_records,
)
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.native_dispatch import run_ledger
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_mutator
from eawf.runtime.integration.apply import (
    IntegrationRefusal,
    IntegrationRefusedError,
    integration_order,
)
from eawf.runtime.integration.commit_policy import CommitSubject
from eawf.runtime.integration.recovery import (
    IntegrationOutcome,
    IntegrationOutcomeKind,
    IntegrationPlan,
    IntegrationWorkspace,
    generation_record_key,
    integrate_batch,
    plan_deliveries,
)
from eawf.runtime.vcs.coauthor import VcsConfig
from eawf.runtime.verification.receipts import ProofRuntimeFacts
from eawf.workflow.delivery.completion import (
    CompletionRefusal,
    CompletionRefusedError,
    decide_task_completion,
)
from eawf.workflow.delivery.criteria import (
    CriteriaAuthoring,
    CriteriaAuthoringError,
    ExecutionContractSet,
    compile_execution_contracts,
)
from eawf.workflow.delivery.verification_cycle import (
    BatchVerificationDecision,
    VerificationRefusedError,
    decide_batch_verification,
    rebase_cycle,
    require_reviewer_independence,
)
from eawf.workflow.integration.conflict import block_attempt

logger = logging.getLogger(__name__)


#: The verb that turns a Batch's sealed candidates into one delivery.
DELIVERY_INTEGRATE_METHOD: Final = "runtime.delivery.integrate"

#: The verb that judges whether one Task is finished on the Batch head.
DELIVERY_ASSESS_COMPLETION_METHOD: Final = "runtime.delivery.assess_completion"

#: The verb that walks one Batch's verification cycle on its exact head.
DELIVERY_VERIFY_BATCH_METHOD: Final = "runtime.delivery.verify_batch"

#: The ledger-line key prefix a conflict frame is filed under.
CONFLICT_KEY_PREFIX: Final = "INC-"

#: The ledger-line key prefix a verification cycle is filed under. One
#: key per Batch: a cycle is one evolving record, and its newest line is
#: where the Batch stands.
CYCLE_KEY_PREFIX: Final = "BVC-"

#: The status a generation line records.
GENERATION_STATUS: Final = "selected"

#: The status a conflict line records.
CONFLICT_STATUS: Final = "blocked"

#: The tree an integration runs in. Unset: see the module docstring. The
#: verb refuses rather than reporting a delivery no workspace produced.
INTEGRATION_WORKSPACE: IntegrationWorkspace | None = None

#: The client's name for one request.
IdempotencyKey = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]


class DeliveryIntegrateParams(BaseModel):
    """Params of :data:`DELIVERY_INTEGRATE_METHOD`.

    Attributes:
        urn: The Batch being delivered.
        actor: Who asked.
        idempotency_key: The client's name for this request.
        base: The revision the Batch starts from, used as the target base
            until the Batch has a generation of its own.
        branch: The integration branch a conflict would be seen on.
        subject: The squashed delivery commit's subject.
        subjects: One subject line per candidate, keyed by candidate ref.
        exit_refs: Where each conflict exit kind lands. Named by the
            caller because the daemon mints no Task and no pending
            action, and a conflict frame without an exit is unusable.
        affected_criterion_ids: The criteria this delivery invalidates.
    """

    model_config = ConfigDict(extra="forbid")

    urn: BatchUrn
    actor: PrincipalKey
    idempotency_key: IdempotencyKey
    base: RevisionBinding
    branch: BranchName
    subject: CommitSubject
    subjects: dict[CandidateId, CommitSubject] = Field(min_length=1)
    exit_refs: dict[ConflictExitKind, AnyEntityUrn] = Field(min_length=1)
    diagnostic_ref: EvidenceUrn
    affected_criterion_ids: tuple[GateIdentityStr, ...] = ()


class DeliveryIntegrateAnswer(BaseModel):
    """What one integration answers with.

    Attributes:
        batch_ref: The Batch that was delivered.
        candidates: The sealed candidates, in integration order.
        manifest_ids: The manifest of each delivery, in the same order.
        generation_ids: Every generation the run selected.
        commit_messages: The full message of each delivery commit.
        delivered: Whether every planned delivery landed.
        blocked_on: The candidate that conflicted, or ``None``.
        conflict: The conflict frame, or ``None``.
        reason: One sentence an operator reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_ref: str
    candidates: tuple[CandidateId, ...]
    manifest_ids: tuple[str, ...]
    generation_ids: tuple[str, ...] = ()
    commit_messages: tuple[str, ...] = ()
    delivered: bool
    blocked_on: CandidateId | None = None
    conflict: dict[str, Any] | None = None
    reason: str


def _refused(code: IntegrationRefusal | CompletionRefusal, detail: str) -> DaemonValidationError:
    """Return the wire form of one delivery refusal."""
    return DaemonValidationError(f"validation_failed: {code.value}: {detail}")


def _params(params: dict[str, Any]) -> DeliveryIntegrateParams:
    """Validate request params, dropping the key the fence already used.

    Raises:
        DaemonValidationError: The request does not parse. The pydantic
            detail is reduced to field paths so the refusal never repeats
            a submitted value into a log.
    """
    try:
        return DeliveryIntegrateParams.model_validate(
            {key: value for key, value in params.items() if key != REPO_ROOT_PARAM}
        )
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        raise DaemonValidationError(
            f"validation_failed: schema_validation_failed: check {', '.join(fields)}"
        ) from error


def _vcs_config(tree_root: Path) -> VcsConfig:
    """Return the repository's validated ``vcs`` block.

    The native root is the ``.ea`` directory, so the repository the
    layered config anchors on is its parent.
    """
    anchor = tree_root.parent
    merged, _sources = merge_config(repo=anchor, workspace=anchor)
    return VcsConfig.model_validate(merged.get("vcs", {}))


def _tasks(session: RootSession) -> tuple[Task, ...]:
    """Return every Task the tree holds, from both storage tiers.

    A Task that has terminated is compacted out of the document into the
    Task ledger, so both tiers are read: a reader that looked only in the
    document would stop answering for exactly the Tasks whose work is
    ready to be delivered.
    """
    rows = document_rows(session.read_document(), Epoch2Collection.TASK)
    lines = read_ledger_records(session.ledger_path(Epoch2Collection.TASK))
    payloads = [*rows.values(), *(item.payload for item in effective_records(lines))]
    return tuple(
        Task.model_validate(payload) for payload in payloads if "payload_kind" not in payload
    )


def _batch_of_task(session: RootSession) -> Mapping[str, str]:
    """Return which Batch each Task is placed in, by Task key."""
    return {
        task.urn.entity_key: str(task.batch_ref)
        for task in _tasks(session)
        if task.batch_ref is not None
    }


def sealed_bundles(
    records: Sequence[LedgerRecord], *, batch_ref: BatchUrn, placement: Mapping[str, str]
) -> tuple[CandidateBundle, ...]:
    """Return every sealed candidate whose Task is placed in *batch_ref*.

    Args:
        records: Every line the run ledger holds.
        batch_ref: The Batch being delivered.
        placement: Which Batch each Task key is placed in.

    Returns:
        The bundles, in ledger order; ordering them is
        :func:`~eawf.runtime.integration.apply.integration_order`'s job.

    Raises:
        ValidationError: A line claims to be a bundle and does not
            validate as one, which means the ledger is corrupt rather
            than merely unfamiliar.
    """
    wanted = str(batch_ref)
    sealed: list[CandidateBundle] = []
    for item in records:
        if item.payload.get("payload_kind") != "candidate_bundle":
            continue
        bundle = CandidateBundle.model_validate(item.payload)
        if placement.get(bundle.task_ref.entity_key) == wanted:
            sealed.append(bundle)
    return tuple(sealed)


class _RootLock:
    """The canonical state lock, taken as one native root session.

    The session is exposed only while the lock is held, so a store that
    reads or writes outside the hold has no path to read or write
    through, and the discipline is a missing object rather than a
    convention somebody has to remember.
    """

    def __init__(self, context: Epoch2RootContext, urns: Sequence[str]) -> None:
        """Bind the lock to the entities its session will hold."""
        self._context = context
        self._urns = list(urns)
        self.session: RootSession | None = None

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Hold the canonical state lock for the block's duration."""
        with self._context.session(self._urns) as session:
            self.session = session
            try:
                yield
            finally:
                self.session = None


class _LedgerGenerationStore:
    """One Batch's generation history, kept in the Batch ledger.

    Every read and every write goes through the lock's live session, so
    the swap cannot be performed with the lock released.
    """

    def __init__(self, lock: _RootLock, batch_ref: BatchUrn) -> None:
        """Bind the store to the lock whose session it reads through."""
        self._lock = lock
        self._batch_ref = batch_ref

    def _ledger(self) -> Path:
        """Return the Batch ledger of the held session.

        Raises:
            RuntimeError: The lock is not held, so the swap would run
                unguarded.
        """
        session = self._lock.session
        if session is None:
            raise RuntimeError("the generation store is only read under the state lock")
        return session.ledger_path(Epoch2Collection.BATCH)

    def read(self) -> IntegrationGenerationLedger:
        """Return the Batch's generation history as the ledger holds it."""
        return _read_generation_ledger(self._ledger(), self._batch_ref)

    def write(self, ledger: IntegrationGenerationLedger) -> None:
        """Append the new head; the lines already written are history."""
        head = ledger.head
        assert head is not None, "a written ledger always has a head"
        _append_generation(self._ledger(), head)


def _read_generation_ledger(path: Path, batch_ref: BatchUrn) -> IntegrationGenerationLedger:
    """Return one Batch's generation history from the Batch ledger.

    The selection flag is taken from position rather than from the line:
    every generation was the head when it was written, and exactly the
    newest one is the head now.
    """
    wanted = str(batch_ref)
    payloads = [
        dict(item.payload)
        for item in read_ledger_records(path)
        if item.record_key.startswith("ING-") and item.payload.get("batch_ref") == wanted
    ]
    for index, payload in enumerate(payloads):
        payload["selected"] = index == len(payloads) - 1
    return IntegrationGenerationLedger.model_validate(
        {"batch_ref": wanted, "generations": payloads}
    )


def _append_generation(path: Path, generation: IntegrationGeneration) -> None:
    """File one selected generation as a line of the Batch ledger."""
    append_ledger_record(
        path,
        LedgerRecord(
            collection=Epoch2Collection.BATCH,
            record_key=generation_record_key(generation),
            status=GENERATION_STATUS,
            recorded_at=generation.created_at,
            payload=generation.model_dump(mode="json"),
        ),
    )


def _append_conflict(path: Path, conflict: IntegrationConflict, *, at: datetime) -> None:
    """File one conflict frame as a line of the Batch ledger."""
    append_ledger_record(
        path,
        LedgerRecord(
            collection=Epoch2Collection.BATCH,
            record_key=f"{conflict.id}-{conflict.batch_ref.entity_key}",
            status=CONFLICT_STATUS,
            recorded_at=at,
            payload=conflict.model_dump(mode="json"),
        ),
    )


def _derived_key(prefix: str, *parts: str) -> str:
    """Return a six-digit key derived from *parts* under *prefix*.

    Derived rather than minted so a retried integration of the same
    candidate names the same attempt instead of a second one.
    """
    body = canonical_digest(list(parts)).removeprefix("sha256:")
    return f"{prefix}{int(body[:8], 16) % 1000000:06d}"


def _applying_attempt(
    plan: IntegrationPlan, bundle: CandidateBundle, *, now: datetime
) -> IntegrationAttempt:
    """Return the attempt that was applying *bundle* when it conflicted."""
    subject = (str(plan.batch_ref), bundle.candidate_ref, str(plan.generation))
    return IntegrationAttempt(
        id=_derived_key("INA-", *subject),
        operation_attempt_id=f"OPR-INT-{_derived_key('', *subject)}",
        batch_ref=plan.batch_ref,
        task_ref=bundle.task_ref,
        candidate_bundle_id=f"CB-{_derived_key('', bundle.candidate_ref)}00",
        generation=plan.generation,
        source_base=plan.source_base,
        selected_batch_base=plan.target_base,
        candidate_patch_digest=canonical_digest(list(bundle.changed_paths)),
        candidate_tree_digest=bundle.resulting_tree_digest,
        changed_paths=bundle.changed_paths,
        idempotency_key=bundle.candidate_ref,
        status=IntegrationAttemptStatus.APPLYING,
        requested_at=now,
        updated_at=now,
    )


def _record_conflict(
    context: Epoch2RootContext,
    plan: IntegrationPlan,
    outcome: IntegrationOutcome,
    args: DeliveryIntegrateParams,
    *,
    now: datetime,
) -> IntegrationConflict:
    """Block the conflicting attempt and file the frame it leaves behind.

    Raises:
        DaemonValidationError: The request names no exit reference for
            the exit kind the cause routes to, so the frame would carry
            no way out.
    """
    blocking = next(
        bundle for bundle in plan.ordered if bundle.candidate_ref == outcome.applied.blocked_on
    )

    def exits(kind: ConflictExitKind) -> QualifiedUrn:
        ref = args.exit_refs.get(kind)
        if ref is None:
            raise _refused(
                IntegrationRefusal.EXIT_UNNAMED,
                f"the request names no {kind.value} reference, so the conflict frame would "
                "carry no way out",
            )
        return ref

    files: tuple[ConflictFile, ...] = outcome.applied.conflict_files
    blocked = block_attempt(
        _applying_attempt(plan, blocking, now=now),
        conflict_id=_derived_key("INC-", str(plan.batch_ref), blocking.candidate_ref),
        repository_ref=args.base.repository_ref,
        branch=args.branch,
        ahead=outcome.applied.ahead,
        behind=outcome.applied.behind,
        files=files,
        diagnostic_ref=args.diagnostic_ref,
        exits=exits,
        at=now,
    )
    with context.session([str(plan.batch_ref)]) as session:
        _append_conflict(session.ledger_path(Epoch2Collection.BATCH), blocked.conflict, at=now)
    return blocked.conflict


def integrate_delivery(
    context: Epoch2RootContext,
    args: DeliveryIntegrateParams,
    *,
    workspace: IntegrationWorkspace | None,
    now: datetime,
) -> DeliveryIntegrateAnswer:
    """Turn one Batch's sealed candidates into its next delivery.

    Args:
        context: The native context of the tree the Batch lives in.
        args: The validated request.
        workspace: The tree the integration runs in.
        now: The stamp the generation is created at.

    Returns:
        The deliveries that landed, or the conflict that stopped them.

    Raises:
        DaemonValidationError: The Batch has no sealed candidate, the
            candidates disagree about their base, a candidate has no
            subject, no workspace is available, or the Batch head moved
            while the integration ran.
    """
    with context.session([str(args.urn)]) as session:
        placement = _batch_of_task(session)
        records = read_ledger_records(run_ledger(session))
    try:
        ordered = integration_order(
            sealed_bundles(records, batch_ref=args.urn, placement=placement)
        )
    except IntegrationRefusedError as error:
        raise _refused(error.code, str(error)) from error
    if ordered[0].base_commit != args.base.head_sha:
        raise _refused(
            IntegrationRefusal.BASE_DIVERGENT,
            f"the candidates were produced from another commit than the base binding names, so "
            f"batch {args.urn.entity_key} cannot be delivered from it",
        )
    lock = _RootLock(context, [str(args.urn)])
    store = _LedgerGenerationStore(lock, args.urn)
    with lock.hold():
        head = store.read().head
    vcs = _vcs_config(context.identity.tree_root)
    try:
        plans = plan_deliveries(
            ordered,
            repository_ref=args.base.repository_ref,
            batch_ref=args.urn,
            source_base=args.base,
            target_base=head.integrated_revision if head is not None else args.base,
            parent_generation_id=None if head is None else head.id,
            subjects=dict(args.subjects),
            batch_subject=args.subject,
            unit=vcs.integration_commit_unit,
            task_reference=vcs.task_reference,
            affected_criterion_ids=args.affected_criterion_ids,
        )
    except IntegrationRefusedError as error:
        raise _refused(error.code, str(error)) from error
    except KeyError as error:
        raise _refused(
            IntegrationRefusal.CANDIDATES_ABSENT,
            f"candidate {error.args[0]} has no subject, so its row could not say what it did",
        ) from error
    if workspace is None:
        raise _refused(
            IntegrationRefusal.WORKSPACE_ABSENT,
            f"no integration workspace is available, so batch {args.urn.entity_key} cannot be "
            "applied without reporting a delivery that touched no tree",
        )
    return _run_plans(context, plans, args, workspace=workspace, lock=lock, store=store, now=now)


def _run_plans(
    context: Epoch2RootContext,
    plans: Sequence[IntegrationPlan],
    args: DeliveryIntegrateParams,
    *,
    workspace: IntegrationWorkspace,
    lock: _RootLock,
    store: _LedgerGenerationStore,
    now: datetime,
) -> DeliveryIntegrateAnswer:
    """Integrate each planned delivery in turn, stopping at the first block."""
    generations: list[str] = []
    messages: list[str] = []
    for plan in plans:
        try:
            outcome = integrate_batch(plan, workspace=workspace, lock=lock, store=store, now=now)
        except IntegrationRefusedError as error:
            raise _refused(error.code, str(error)) from error
        if outcome.kind is IntegrationOutcomeKind.BLOCKED:
            conflict = _record_conflict(context, plan, outcome, args, now=now)
            logger.info(
                f"integrate_delivery blocked batch={args.urn.entity_key} "
                f"candidate={outcome.applied.blocked_on}"
            )
            return _answer(
                plans,
                args,
                generation_ids=tuple(generations),
                commit_messages=tuple(messages),
                delivered=False,
                blocked_on=outcome.applied.blocked_on,
                conflict=conflict.model_dump(mode="json"),
            )
        assert outcome.generation is not None, "a delivered outcome carries its generation"
        generations.append(outcome.generation.id)
        messages.append(plan.delivery.message)
    logger.info(
        f"integrate_delivery delivered batch={args.urn.entity_key} generations={len(generations)}"
    )
    return _answer(
        plans,
        args,
        generation_ids=tuple(generations),
        commit_messages=tuple(messages),
        delivered=True,
        blocked_on=None,
        conflict=None,
    )


def _answer(
    plans: Sequence[IntegrationPlan],
    args: DeliveryIntegrateParams,
    *,
    generation_ids: tuple[str, ...],
    commit_messages: tuple[str, ...],
    delivered: bool,
    blocked_on: CandidateId | None,
    conflict: dict[str, Any] | None,
) -> DeliveryIntegrateAnswer:
    """Build the answer one integration run returns."""
    candidates = tuple(bundle.candidate_ref for plan in plans for bundle in plan.ordered)
    reason = (
        f"batch {args.urn.entity_key} is delivered in {len(generation_ids)} generation(s)"
        if delivered
        else f"candidate {blocked_on} conflicts, so no canonical ref moved"
    )
    return DeliveryIntegrateAnswer(
        batch_ref=str(args.urn),
        candidates=candidates,
        manifest_ids=tuple(plan.manifest.manifest_id for plan in plans),
        generation_ids=generation_ids,
        commit_messages=commit_messages,
        delivered=delivered,
        blocked_on=blocked_on,
        conflict=conflict,
        reason=reason,
    )


@native_mutator(DELIVERY_INTEGRATE_METHOD)
async def _integrate_delivery(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Integrate one Batch's sealed candidates into its next delivery."""
    args = _params(params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(
        integrate_delivery,
        context,
        args,
        workspace=INTEGRATION_WORKSPACE,
        now=datetime.now(UTC),
    )
    return answer.model_dump(mode="json")


class TaskCompletionParams(BaseModel):
    """Params of :data:`DELIVERY_ASSESS_COMPLETION_METHOD`.

    Attributes:
        urn: The Task being judged.
        actor: Who asked.
        idempotency_key: The client's name for this request.
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

    urn: TaskUrn
    actor: PrincipalKey
    idempotency_key: IdempotencyKey
    base: RevisionBinding
    report_verdict: AgentReportVerdict
    gates: tuple[GateSpec, ...] = Field(min_length=1)
    receipts: tuple[ProofReceipt, ...] = ()
    proof_facts: ProofRuntimeFacts


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


def _task_of(session: RootSession, urn: TaskUrn) -> Task:
    """Return the Task *urn* names, from whichever tier holds it.

    Raises:
        DaemonValidationError: No Task of that key lives in this tree.
    """
    wanted = str(urn)
    for task in _tasks(session):
        if str(task.urn) == wanted:
            return task
    raise DaemonValidationError(
        f"validation_failed: task_absent: no task {urn.entity_key} lives in this tree"
    )


def _sealed_bundle(records: Sequence[LedgerRecord], *, task_ref: TaskUrn) -> CandidateBundle | None:
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


def _contracts(task: Task, gates: Sequence[GateSpec]) -> ExecutionContractSet:
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
    with context.session([str(args.urn)]) as session:
        task = _task_of(session, args.urn)
        if task.batch_ref is None:
            raise _refused(
                CompletionRefusal.TASK_UNDELIVERED,
                f"task {task.urn.entity_key} is unplaced, so no Batch carries its work",
            )
        bundle = _sealed_bundle(read_ledger_records(run_ledger(session)), task_ref=task.urn)
        ledger = _read_generation_ledger(
            session.ledger_path(Epoch2Collection.BATCH), task.batch_ref
        )
    try:
        decision = decide_task_completion(
            task,
            report_verdict=args.report_verdict,
            bundle=bundle,
            ledger=ledger,
            base=args.base,
            contracts=_contracts(task, args.gates),
            receipts=args.receipts,
            facts=args.proof_facts,
        )
    except CompletionRefusedError as error:
        raise DaemonValidationError(f"validation_failed: {error}") from error
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


@native_mutator(DELIVERY_ASSESS_COMPLETION_METHOD)
async def _assess_task_completion(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Judge whether one Task may move to COMPLETED on its Batch head."""
    args = _completion_params(params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(assess_task_completion, context, args)
    return answer.model_dump(mode="json")


class BatchVerifyParams(BaseModel):
    """Params of :data:`DELIVERY_VERIFY_BATCH_METHOD`.

    Attributes:
        urn: The Batch being verified.
        actor: Who asked.
        idempotency_key: The client's name for this request.
        audits: The independent verdicts held for the Batch. Presented
            rather than read because no native record holds one yet; a
            presented row buys nothing unless its reviewer clears the
            independence check against the Runs that actually produced
            this Batch's candidates.
        judgment_criterion_ids: The criteria no deterministic proof can
            settle, which the audit must therefore cover. Named by the
            caller because they come from the compiled contracts of the
            Batch's Tasks rather than from the Batch record.
        exit_refs: Where each exit kind lands. Named by the caller
            because the daemon mints no Task and no pending action, and a
            blocked cycle with no exit is unusable.
        repair_budget: How many repairs the Batch may open without
            asking, used only when a cycle is opened.
    """

    model_config = ConfigDict(extra="forbid")

    urn: BatchUrn
    actor: PrincipalKey
    idempotency_key: IdempotencyKey
    audits: tuple[BatchAudit, ...] = ()
    judgment_criterion_ids: tuple[GateIdentityStr, ...] = ()
    exit_refs: dict[ConflictExitKind, AnyEntityUrn] = Field(default_factory=dict)
    repair_budget: StrictNonNegativeInt = 1


class BatchVerifyAnswer(BaseModel):
    """What one verification pass answers with.

    Attributes:
        batch_ref: The Batch that was verified.
        stage: Where its cycle stands after the pass.
        head_generation: The exact ordinal the whole pass was taken on.
        blocking_criterion_ids: The criteria a required row leaves open.
        settled_criterion_ids: The judgment criteria an audit row cleared.
        repair_criterion_ids: What a repair opened by this pass is bounded
            to address.
        repairs_spent: How much of the repair budget the Batch has used.
        invalidated_criterion_ids: What a head move before this pass
            invalidated, named rather than counted.
        carried_criterion_ids: What that move left standing.
        exit: The typed way out this pass opened, or ``None``.
        merge_ready: Whether the Batch cleared on this head.
        reason: One sentence an operator reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_ref: str
    stage: str
    head_generation: int
    blocking_criterion_ids: tuple[str, ...] = ()
    settled_criterion_ids: tuple[str, ...] = ()
    repair_criterion_ids: tuple[str, ...] = ()
    repairs_spent: int
    invalidated_criterion_ids: tuple[str, ...] = ()
    carried_criterion_ids: tuple[str, ...] = ()
    exit: dict[str, Any] | None = None
    merge_ready: bool
    reason: str


def _verify_params(params: dict[str, Any]) -> BatchVerifyParams:
    """Validate request params, dropping the key the fence already used.

    Raises:
        DaemonValidationError: The request does not parse. The pydantic
            detail is reduced to field paths so the refusal never repeats
            a submitted value into a log.
    """
    try:
        return BatchVerifyParams.model_validate(
            {key: value for key, value in params.items() if key != REPO_ROOT_PARAM}
        )
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        raise DaemonValidationError(
            f"validation_failed: schema_validation_failed: check {', '.join(fields)}"
        ) from error


def _cycle_record_key(batch_ref: BatchUrn) -> str:
    """Return the one Batch-ledger key every cycle line of *batch_ref* is filed under."""
    return f"{CYCLE_KEY_PREFIX}{batch_ref.entity_key}"


def _read_cycle(path: Path, batch_ref: BatchUrn) -> BatchVerificationCycle | None:
    """Return the Batch's current verification cycle, or ``None`` before the first.

    The ledger keeps every pass, so the current cycle is the newest line
    filed under the Batch's cycle key rather than a row edited in place.
    """
    wanted = _cycle_record_key(batch_ref)
    payloads = [item.payload for item in read_ledger_records(path) if item.record_key == wanted]
    if not payloads:
        return None
    return BatchVerificationCycle.model_validate(payloads[-1])


def _append_cycle(path: Path, cycle: BatchVerificationCycle, *, at: datetime) -> None:
    """File one verification cycle as a line of the Batch ledger."""
    append_ledger_record(
        path,
        LedgerRecord(
            collection=Epoch2Collection.BATCH,
            record_key=_cycle_record_key(cycle.batch_ref),
            status=cycle.stage.value,
            recorded_at=at,
            payload=cycle.model_dump(mode="json"),
        ),
    )


def _merge_audits(
    carried: Sequence[BatchAudit], presented: Sequence[BatchAudit]
) -> tuple[BatchAudit, ...]:
    """Return the rows a cycle holds, a presented verdict replacing a carried one.

    A criterion carries exactly one verdict, so a freshly presented row
    for a criterion the cycle already held supersedes it rather than
    joining it.
    """
    by_criterion = {item.criterion_id: item for item in carried}
    by_criterion.update({item.criterion_id: item for item in presented})
    return tuple(by_criterion[key] for key in sorted(by_criterion))


def _holding(
    cycle: BatchVerificationCycle, presented: Sequence[BatchAudit]
) -> BatchVerificationCycle:
    """Return *cycle* holding the presented rows, revalidated rather than copied.

    The merged cycle is built through validation because a presented row
    is request data: one bound to an ordinal past the head, or to another
    Batch, has to become a typed refusal rather than an unhandled error
    on the way out of the handler.

    Raises:
        DaemonValidationError: The presented rows do not make a coherent
            cycle on this head.
    """
    try:
        return BatchVerificationCycle.model_validate(
            {
                **cycle.model_dump(),
                "audits": [item.model_dump() for item in _merge_audits(cycle.audits, presented)],
            }
        )
    except ValidationError as error:
        reasons = sorted({str(row["msg"]) for row in error.errors()})
        raise DaemonValidationError(
            f"validation_failed: verification_audit_unbound: {'; '.join(reasons)}"
        ) from error


def _cycle_at_head(
    stored: BatchVerificationCycle | None,
    *,
    head: IntegrationGeneration,
    ledger: IntegrationGenerationLedger,
    args: BatchVerifyParams,
) -> tuple[BatchVerificationCycle, HeadInvalidation | None]:
    """Return the cycle to walk on the current head, and what a move cost.

    Raises:
        DaemonValidationError: A generation between the stored cycle and
            the head names an affected set its own freshness key was not
            written under, so what the move invalidated cannot be read.
    """
    revision = head.integrated_revision
    if stored is None:
        return (
            BatchVerificationCycle(
                batch_ref=args.urn,
                head=revision,
                stage=BatchVerificationStage.CHECKING,
                repair_budget=args.repair_budget,
            ),
            None,
        )
    if stored.generation == head.generation:
        return stored, None
    moved = [item for item in ledger.generations if item.generation > stored.generation]
    try:
        return rebase_cycle(stored, head=revision, moved_through=moved)
    except CompletionRefusedError as error:
        raise DaemonValidationError(f"validation_failed: {error}") from error


def _verify_answer(
    decision: BatchVerificationDecision, *, invalidation: HeadInvalidation | None
) -> BatchVerifyAnswer:
    """Build the answer one verification pass returns."""
    return BatchVerifyAnswer(
        batch_ref=decision.batch_ref,
        stage=decision.stage.value,
        head_generation=decision.head_generation,
        blocking_criterion_ids=decision.blocking_criterion_ids,
        settled_criterion_ids=decision.settled_criterion_ids,
        repair_criterion_ids=decision.repair_criterion_ids,
        repairs_spent=decision.repairs_spent,
        invalidated_criterion_ids=(
            () if invalidation is None else invalidation.invalidated_criterion_ids
        ),
        carried_criterion_ids=(() if invalidation is None else invalidation.carried_criterion_ids),
        exit=None if decision.exit is None else decision.exit.model_dump(mode="json"),
        merge_ready=decision.merge_ready,
        reason=decision.reason,
    )


def _settled_answer(
    cycle: BatchVerificationCycle, *, invalidation: HeadInvalidation | None
) -> BatchVerifyAnswer:
    """Return the standing answer of a cycle that has nothing left to walk."""
    return BatchVerifyAnswer(
        batch_ref=str(cycle.batch_ref),
        stage=cycle.stage.value,
        head_generation=cycle.generation,
        blocking_criterion_ids=cycle.blocking_criterion_ids,
        repairs_spent=cycle.repairs_spent,
        invalidated_criterion_ids=(
            () if invalidation is None else invalidation.invalidated_criterion_ids
        ),
        carried_criterion_ids=(() if invalidation is None else invalidation.carried_criterion_ids),
        exit=None if cycle.exit is None else cycle.exit.model_dump(mode="json"),
        merge_ready=cycle.stage is BatchVerificationStage.READY_TO_MERGE,
        reason=(
            f"batch {cycle.batch_ref.entity_key} already stands in {cycle.stage.value} on "
            f"generation {cycle.generation}, which nothing since has moved"
        ),
    )


def verify_batch(
    context: Epoch2RootContext, args: BatchVerifyParams, *, now: datetime
) -> BatchVerifyAnswer:
    """Walk one Batch's verification cycle on the head it currently delivers.

    Args:
        context: The native context of the tree the Batch lives in.
        args: The validated request.
        now: The stamp the cycle line is filed at.

    Returns:
        Where the Batch stands after the pass, and the exit it opened.

    Raises:
        DaemonValidationError: The Batch has selected no generation, a
            presented row's reviewer produced the work it judges or could
            have changed it, a judgment criterion carries no row, the walk
            needs an exit the request does not name, or a generation's
            affected set is unbound from its own key.
    """
    with context.session([str(args.urn)]) as session:
        placement = _batch_of_task(session)
        runs = read_ledger_records(run_ledger(session))
        batch_ledger = session.ledger_path(Epoch2Collection.BATCH)
        ledger = _read_generation_ledger(batch_ledger, args.urn)
        stored = _read_cycle(batch_ledger, args.urn)
    head = ledger.head
    if head is None:
        raise _refused(
            CompletionRefusal.GENERATION_UNSELECTED,
            f"batch {args.urn.entity_key} has selected no integration generation, so there is no "
            "exact head to verify it on",
        )
    producers = tuple(
        item.run_ref for item in sealed_bundles(runs, batch_ref=args.urn, placement=placement)
    )
    cycle, invalidation = _cycle_at_head(stored, head=head, ledger=ledger, args=args)
    if cycle.stage is not BatchVerificationStage.CHECKING:
        return _settled_answer(cycle, invalidation=invalidation)
    try:
        require_reviewer_independence(args.audits, producer_run_refs=producers)
        walked, decision = decide_batch_verification(
            _holding(cycle, args.audits),
            judgment_criterion_ids=args.judgment_criterion_ids,
            exits=args.exit_refs,
            invalidation=invalidation,
        )
    except VerificationRefusedError as error:
        raise DaemonValidationError(f"validation_failed: {error}") from error
    with context.session([str(args.urn)]) as session:
        _append_cycle(session.ledger_path(Epoch2Collection.BATCH), walked, at=now)
    logger.info(
        f"verify_batch batch={args.urn.entity_key} head={walked.generation} "
        f"stage={walked.stage.value} blocking={len(decision.blocking_criterion_ids)}"
    )
    return _verify_answer(decision, invalidation=invalidation)


@native_mutator(DELIVERY_VERIFY_BATCH_METHOD)
async def _verify_batch(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Walk one Batch's verification cycle and file where it landed."""
    args = _verify_params(params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(verify_batch, context, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


__all__ = [
    "DELIVERY_ASSESS_COMPLETION_METHOD",
    "DELIVERY_INTEGRATE_METHOD",
    "DELIVERY_VERIFY_BATCH_METHOD",
    "BatchVerifyAnswer",
    "BatchVerifyParams",
    "DeliveryIntegrateAnswer",
    "DeliveryIntegrateParams",
    "TaskCompletionAnswer",
    "TaskCompletionParams",
    "assess_task_completion",
    "integrate_delivery",
    "sealed_bundles",
    "verify_batch",
]
