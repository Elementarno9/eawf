"""Delivery requests assembled from the records a Batch plan names.

The three delivery verbs -- integrate, verify and assess completion --
each take a request whose halves are partly durable facts and partly
references. Handing a caller the whole request to fill in is how a
subject line, an exit or a verdict ends up invented: nothing checks that
the Task a repair exit names exists, or that the verdict a completion
presents is the one the Run's sealed report carried.

This module takes the other road. :func:`resolve_batch_plan` reads the
Batch record, every Task its plan lists, the Run behind each sealed
candidate and every record a reference names, in one read-only session,
and refuses the whole plan when any of them does not resolve. The
builders then derive every request field from the resolved records: the
branch from the Batch, each commit subject from its Task's intent, the
affected and judgment criteria from the Tasks' own criteria, the verdict
from the sealed bundle. What no record holds -- the observed base
revision, the exit and diagnostic references, the gates a Task's
criteria name, the proof runtime -- arrives typed and is checked against
the records rather than trusted, so no field is caller prose.

A refusal happens before any request exists, so it writes nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.delivery.integration import EXIT_REF_KINDS, ConflictExitKind
from eawf.kernel.delivery.receipts import RevisionBinding, canonical_digest
from eawf.kernel.identity import EntityKind, QualifiedUrn
from eawf.kernel.runtime.candidate import CandidateBundle
from eawf.kernel.spec.common import GateSpec
from eawf.kernel.state.epoch2.base import PrincipalKey
from eawf.kernel.state.epoch2.batch import DeliveryBatch
from eawf.kernel.state.epoch2.run import Run, TaskScope
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.state.epoch2.urns import AnyEntityUrn, BatchUrn, EvidenceUrn, TaskUrn
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.tiers import ENTITY_COLLECTIONS, LEDGER_COLLECTIONS, Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.methods.delivery import (
    BatchVerifyParams,
    DeliveryIntegrateParams,
    TaskCompletionParams,
)
from eawf.runtime.integration.commit_policy import MAX_SUBJECT_LENGTH
from eawf.runtime.verification.receipts import ProofRuntimeFacts

logger = logging.getLogger(__name__)

#: The evidence kind whose criteria only an audit verdict can settle.
_JUDGMENT_EVIDENCE: Final = "jury"


class AssemblyRefusal(StrEnum):
    """Why a Batch plan could not be turned into delivery requests."""

    BATCH_ABSENT = "batch_absent"
    BATCH_UNTARGETED = "batch_untargeted"
    TASK_REFERENCE_UNRESOLVED = "task_reference_unresolved"
    TASK_PLACEMENT_MISMATCH = "task_placement_mismatch"
    RUN_REFERENCE_UNRESOLVED = "run_reference_unresolved"
    EXIT_REFERENCE_UNRESOLVED = "exit_reference_unresolved"
    DIAGNOSTIC_REFERENCE_UNRESOLVED = "diagnostic_reference_unresolved"
    BASE_UNBOUND = "base_unbound"
    CANDIDATES_ABSENT = "candidates_absent"
    TASK_UNSEALED = "task_unsealed"
    GATE_REFERENCE_UNRESOLVED = "gate_reference_unresolved"


class AssemblyRefusedError(ValueError):
    """A Batch plan names a record that does not resolve, or disagrees with one.

    Attributes:
        code: The typed reason, which the daemon renders on the wire.
    """

    def __init__(self, code: AssemblyRefusal, detail: str) -> None:
        """Bind the typed code to its one-sentence detail.

        Args:
            code: Why the plan was refused.
            detail: What an operator reads.
        """
        super().__init__(f"{code.value}: {detail}")
        self.code = code


class DeliveryReferences(BaseModel):
    """The typed half of a delivery request that no Batch record carries.

    Every member is a reference or an observed fact, never prose, and
    :func:`resolve_batch_plan` checks each against the tree.

    Attributes:
        batch_ref: The Batch whose plan is being delivered.
        base: The exact revision the Batch starts from. It must address
            the Batch and its repository, and every sealed candidate must
            have been produced from its commit.
        exit_refs: Where each conflict or verification exit lands. Each
            must resolve to a record of the kind the exit requires.
        diagnostic_ref: The evidence record a conflict diagnostic is
            filed against. It must resolve.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_ref: BatchUrn
    base: RevisionBinding
    exit_refs: dict[ConflictExitKind, AnyEntityUrn] = Field(min_length=1)
    diagnostic_ref: EvidenceUrn


class BatchPlan(BaseModel):
    """One Batch with every record its plan names, resolved.

    Attributes:
        batch: The Batch record.
        tasks: Its Tasks, in the order the Batch plan lists them.
        bundles: The newest sealed candidate of each Task that sealed one,
            in plan order.
        runs: The Run behind each of those candidates, by candidate ref.
        references: The typed references, already checked against the tree.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch: DeliveryBatch
    tasks: tuple[Task, ...]
    bundles: tuple[CandidateBundle, ...]
    runs: dict[str, Run]
    references: DeliveryReferences

    def task(self, task_ref: TaskUrn) -> Task:
        """Return the planned Task *task_ref* names.

        Raises:
            AssemblyRefusedError: The Batch plan does not list the Task.
        """
        wanted = str(task_ref)
        for task in self.tasks:
            if str(task.urn) == wanted:
                return task
        raise AssemblyRefusedError(
            AssemblyRefusal.TASK_REFERENCE_UNRESOLVED,
            f"batch {self.batch.key} does not plan task {task_ref.entity_key}",
        )

    def bundle_of(self, task_ref: TaskUrn) -> CandidateBundle | None:
        """Return the sealed candidate of *task_ref*, or ``None`` when none sealed."""
        wanted = str(task_ref)
        return next((item for item in self.bundles if str(item.task_ref) == wanted), None)


def _stored_row(session: RootSession, urn: QualifiedUrn) -> dict[str, Any] | None:
    """Return the stored payload *urn* addresses, from whichever tier holds it.

    A record that terminated is compacted out of the document into its
    ledger, so both tiers are read; ledger lines that carry a
    ``payload_kind`` are auxiliary lines filed beside the records, such as
    a sealed bundle, and never a record of their own.
    """
    collection = ENTITY_COLLECTIONS[urn.kind]
    row = document_rows(session.read_document(), collection).get(urn.entity_key)
    if row is not None:
        return dict(row)
    if collection not in LEDGER_COLLECTIONS:
        return None
    lines = effective_records(read_ledger_records(session.ledger_path(collection)))
    matches = [
        item.payload
        for item in lines
        if item.record_key == urn.entity_key and "payload_kind" not in item.payload
    ]
    return dict(matches[-1]) if matches else None


def _placed_task_keys(session: RootSession, batch_ref: BatchUrn) -> set[str]:
    """Return the key of every Task the tree places in *batch_ref*."""
    rows = document_rows(session.read_document(), Epoch2Collection.TASK)
    lines = effective_records(read_ledger_records(session.ledger_path(Epoch2Collection.TASK)))
    payloads = [*rows.values(), *(item.payload for item in lines)]
    wanted = str(batch_ref)
    return {
        str(payload["key"])
        for payload in payloads
        if "payload_kind" not in payload and payload.get("batch_ref") == wanted
    }


def _resolve_tasks(session: RootSession, batch: DeliveryBatch) -> tuple[Task, ...]:
    """Return every Task the Batch plan lists, each placed in that Batch.

    Raises:
        AssemblyRefusedError: A listed Task does not resolve, is placed in
            another Batch, or a Task placed in this Batch is missing from
            its plan -- the integrate verb reads placement, so a Task the
            plan omits would be delivered with no subject of its own.
    """
    tasks: list[Task] = []
    for ref in batch.task_refs:
        row = _stored_row(session, ref)
        if row is None:
            raise AssemblyRefusedError(
                AssemblyRefusal.TASK_REFERENCE_UNRESOLVED,
                f"batch {batch.key} plans task {ref.entity_key}, which no record holds",
            )
        task = Task.model_validate(row)
        if task.batch_ref is None or str(task.batch_ref) != str(batch.urn):
            raise AssemblyRefusedError(
                AssemblyRefusal.TASK_PLACEMENT_MISMATCH,
                f"batch {batch.key} plans task {task.key}, which is not placed in it",
            )
        tasks.append(task)
    unplanned = sorted(_placed_task_keys(session, batch.urn) - {task.key for task in tasks})
    if unplanned:
        raise AssemblyRefusedError(
            AssemblyRefusal.TASK_PLACEMENT_MISMATCH,
            f"task(s) {', '.join(unplanned)} are placed in batch {batch.key} but its plan does "
            "not list them",
        )
    return tuple(tasks)


def _resolve_bundles(
    session: RootSession, tasks: Sequence[Task]
) -> tuple[tuple[CandidateBundle, ...], dict[str, Run]]:
    """Return each planned Task's newest sealed candidate and the Run behind it.

    Raises:
        AssemblyRefusedError: A candidate's Run does not resolve, or the
            Run was not scoped to the Task the candidate claims.
    """
    lines = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
    newest: dict[str, CandidateBundle] = {}
    for item in lines:
        if item.payload.get("payload_kind") == "candidate_bundle":
            bundle = CandidateBundle.model_validate(item.payload)
            newest[str(bundle.task_ref)] = bundle
    bundles = tuple(newest[str(task.urn)] for task in tasks if str(task.urn) in newest)
    runs: dict[str, Run] = {}
    for bundle in bundles:
        row = _stored_row(session, bundle.run_ref)
        if row is None:
            raise AssemblyRefusedError(
                AssemblyRefusal.RUN_REFERENCE_UNRESOLVED,
                f"candidate {bundle.candidate_ref} names run {bundle.run_ref.entity_key}, which "
                "no record holds",
            )
        run = Run.model_validate(row)
        if not isinstance(run.scope, TaskScope) or str(run.scope.task_ref) != str(bundle.task_ref):
            raise AssemblyRefusedError(
                AssemblyRefusal.RUN_REFERENCE_UNRESOLVED,
                f"run {run.key} was not scoped to task {bundle.task_ref.entity_key}, so it "
                f"cannot have produced candidate {bundle.candidate_ref}",
            )
        runs[bundle.candidate_ref] = run
    return bundles, runs


def _check_references(
    session: RootSession, batch: DeliveryBatch, references: DeliveryReferences
) -> None:
    """Require every reference to resolve and the base to bind this Batch.

    Raises:
        AssemblyRefusedError: An exit names a record of the wrong kind or
            one no record holds, the diagnostic evidence does not resolve,
            or the base addresses another Batch or repository.
    """
    for kind, ref in sorted(references.exit_refs.items()):
        wanted: EntityKind = EXIT_REF_KINDS[kind]
        if ref.kind is not wanted or _stored_row(session, ref) is None:
            raise AssemblyRefusedError(
                AssemblyRefusal.EXIT_REFERENCE_UNRESOLVED,
                f"the {kind.value} exit names {ref.entity_key}, which no {wanted.value} record "
                "holds",
            )
    if _stored_row(session, references.diagnostic_ref) is None:
        raise AssemblyRefusedError(
            AssemblyRefusal.DIAGNOSTIC_REFERENCE_UNRESOLVED,
            f"diagnostic evidence {references.diagnostic_ref.entity_key} is held by no record",
        )
    base = references.base
    if str(base.batch_ref) != str(batch.urn) or str(base.repository_ref) != str(
        batch.repository_ref
    ):
        raise AssemblyRefusedError(
            AssemblyRefusal.BASE_UNBOUND,
            f"the base binding addresses another batch or repository than batch {batch.key}",
        )


def resolve_batch_plan(context: Epoch2RootContext, references: DeliveryReferences) -> BatchPlan:
    """Read one Batch plan and every record it names, refusing a dangling one.

    The whole read happens in one session and nothing is written, so a
    refused plan leaves the tree exactly as it was.

    Args:
        context: The native context of the tree the Batch lives in.
        references: The typed references the requests will carry.

    Returns:
        The resolved plan every builder below derives from.

    Raises:
        AssemblyRefusedError: The Batch, a planned Task, a candidate's Run,
            an exit or the diagnostic evidence does not resolve; a Task's
            placement disagrees with the plan; a candidate was produced
            from another commit than the base; or the base binds another
            Batch.
    """
    batch_ref = references.batch_ref
    with context.session([str(batch_ref)]) as session:
        row = _stored_row(session, batch_ref)
        if row is None:
            raise AssemblyRefusedError(
                AssemblyRefusal.BATCH_ABSENT, f"no batch {batch_ref.entity_key} lives in this tree"
            )
        batch = DeliveryBatch.model_validate(row)
        tasks = _resolve_tasks(session, batch)
        bundles, runs = _resolve_bundles(session, tasks)
        _check_references(session, batch, references)
    divergent = sorted(
        item.candidate_ref for item in bundles if item.base_commit != references.base.head_sha
    )
    if divergent:
        raise AssemblyRefusedError(
            AssemblyRefusal.BASE_UNBOUND,
            f"candidate(s) {', '.join(divergent)} were produced from another commit than the "
            f"base of batch {batch.key}",
        )
    logger.debug(
        f"resolve_batch_plan batch={batch.key} tasks={len(tasks)} candidates={len(bundles)}"
    )
    return BatchPlan(batch=batch, tasks=tasks, bundles=bundles, runs=runs, references=references)


def _subject(text: str) -> str:
    """Return *text* as one commit subject: first line, trimmed, no final period.

    The result is cut at the subject width on a word boundary where one
    exists, since a Task intent is written to be read, not to fit.
    """
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    if len(line) > MAX_SUBJECT_LENGTH:
        cut = line[:MAX_SUBJECT_LENGTH]
        line = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return line.rstrip(" .")


def _request_key(verb: str, payload: Mapping[str, Any]) -> str:
    """Return an idempotency key derived from the request's own content.

    Derived rather than minted so assembling the same plan twice names
    the same request, and the daemon's replay answers the second.
    """
    return f"{verb}-{canonical_digest(dict(payload)).removeprefix('sha256:')[:48]}"


def assemble_integrate_request(plan: BatchPlan, *, actor: PrincipalKey) -> DeliveryIntegrateParams:
    """Build the integrate request of a resolved Batch plan.

    Args:
        plan: The resolved plan.
        actor: The principal the request is attributed to.

    Returns:
        The validated request, every field derived from the plan.

    Raises:
        AssemblyRefusedError: The Batch has no target branch yet, or no
            planned Task has sealed a candidate.
    """
    batch = plan.batch
    if batch.target_branch is None:
        raise AssemblyRefusedError(
            AssemblyRefusal.BATCH_UNTARGETED,
            f"batch {batch.key} has no target branch, so a conflict has no branch to be seen on",
        )
    if not plan.bundles:
        raise AssemblyRefusedError(
            AssemblyRefusal.CANDIDATES_ABSENT,
            f"no task planned in batch {batch.key} has sealed a candidate",
        )
    subjects = {
        item.candidate_ref: _subject(plan.task(item.task_ref).intent) for item in plan.bundles
    }
    delivered = [plan.task(item.task_ref) for item in plan.bundles]
    affected = sorted({criterion.id for task in delivered for criterion in task.criteria})
    references = plan.references
    payload: dict[str, Any] = {
        "urn": str(batch.urn),
        "actor": actor,
        "base": references.base.model_dump(mode="json"),
        "branch": batch.target_branch,
        "subject": _subject(f"Deliver {batch.key} ({len(delivered)} task(s))"),
        "subjects": subjects,
        "exit_refs": {kind.value: str(ref) for kind, ref in references.exit_refs.items()},
        "diagnostic_ref": str(references.diagnostic_ref),
        "affected_criterion_ids": affected,
    }
    return DeliveryIntegrateParams.model_validate(
        {**payload, "idempotency_key": _request_key("integrate", payload)}
    )


def assemble_verify_request(plan: BatchPlan, *, actor: PrincipalKey) -> BatchVerifyParams:
    """Build the verify request of a resolved Batch plan.

    The judgment criteria are the planned Tasks' jury criteria, which no
    deterministic proof can settle and an audit row must therefore cover.
    No native record holds an audit row yet, so the request presents
    none; the verb then blocks through the named exit rather than
    clearing a Batch nobody judged.

    Args:
        plan: The resolved plan.
        actor: The principal the request is attributed to.

    Returns:
        The validated request, every field derived from the plan.
    """
    judgment = sorted(
        {
            criterion.id
            for task in plan.tasks
            for criterion in task.criteria
            if criterion.evidence_kind == _JUDGMENT_EVIDENCE
        }
    )
    payload: dict[str, Any] = {
        "urn": str(plan.batch.urn),
        "actor": actor,
        "judgment_criterion_ids": judgment,
        "exit_refs": {kind.value: str(ref) for kind, ref in plan.references.exit_refs.items()},
    }
    return BatchVerifyParams.model_validate(
        {**payload, "idempotency_key": _request_key("verify", payload)}
    )


def _task_gates(task: Task, gates: Sequence[GateSpec]) -> tuple[GateSpec, ...]:
    """Return exactly the gates *task*'s criteria reference, in id order.

    Raises:
        AssemblyRefusedError: A criterion references a gate none of
            *gates* is, or a gate belongs to no criterion of the Task.
    """
    by_id = {gate.id: gate for gate in gates}
    referenced = {gate_id for criterion in task.criteria for gate_id in criterion.gate_ids}
    criterion_ids = {criterion.id for criterion in task.criteria}
    missing = sorted(referenced - by_id.keys())
    stray = sorted(
        gate.id
        for gate in gates
        if gate.id not in referenced or gate.criterion_id not in criterion_ids
    )
    if missing or stray:
        raise AssemblyRefusedError(
            AssemblyRefusal.GATE_REFERENCE_UNRESOLVED,
            f"task {task.key} references gate(s) {missing or 'none'} that were not given and "
            f"was given gate(s) {stray or 'none'} its criteria do not reference",
        )
    return tuple(by_id[gate_id] for gate_id in sorted(referenced))


def assemble_completion_request(
    plan: BatchPlan,
    task_ref: TaskUrn,
    *,
    actor: PrincipalKey,
    gates: Sequence[GateSpec],
    proof_facts: ProofRuntimeFacts,
) -> TaskCompletionParams:
    """Build the completion request of one planned Task.

    The verdict is the one the Task's sealed report carried, read from
    the bundle rather than asserted by the caller.

    Args:
        plan: The resolved plan.
        task_ref: The planned Task to judge.
        actor: The principal the request is attributed to.
        gates: The gates the Task's criteria reference, exactly.
        proof_facts: The runner, environment, selector and policy the
            proofs run under.

    Returns:
        The validated request.

    Raises:
        AssemblyRefusedError: The plan does not list the Task, the Task
            has sealed no candidate, or the gates do not match the gate
            references of its criteria.
    """
    task = plan.task(task_ref)
    bundle = plan.bundle_of(task.urn)
    if bundle is None:
        raise AssemblyRefusedError(
            AssemblyRefusal.TASK_UNSEALED,
            f"task {task.key} has sealed no candidate, so no report verdict is on record",
        )
    payload: dict[str, Any] = {
        "urn": str(task.urn),
        "actor": actor,
        "base": plan.references.base.model_dump(mode="json"),
        "report_verdict": bundle.verdict.value,
        "gates": [gate.model_dump(mode="json") for gate in _task_gates(task, gates)],
        "proof_facts": proof_facts.model_dump(mode="json"),
    }
    return TaskCompletionParams.model_validate(
        {**payload, "idempotency_key": _request_key("complete", payload)}
    )


__all__ = [
    "AssemblyRefusal",
    "AssemblyRefusedError",
    "BatchPlan",
    "DeliveryReferences",
    "assemble_completion_request",
    "assemble_integrate_request",
    "assemble_verify_request",
    "resolve_batch_plan",
]
