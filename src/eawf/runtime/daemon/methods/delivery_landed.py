"""Adopting work that already landed, and reading a merge back from the repository.

``runtime.delivery.adopt_landed``: a generation for a head that already exists.

Integration applies sealed candidates and authors a commit, so it cannot
produce a generation whose head is a commit that landed outside the
native loop: replaying that work would author a second commit nobody's
branch carries, and sealing it needs a lease that was never issued. This
verb is the one other way a Batch gains a generation. The caller names the
landed commit, the commit the change started from, the Tasks it carries
and the recorded evidence the adopted verdict rests on; the daemon reads
everything else back from the tree's own repository -- that both commits
exist, that the base is an ancestor of the head, that the head is
contained in the Batch's target branch, the head's tree and parent, and
the paths between them -- and resolves every evidence reference against
the ledgers. Only then does it file the adoption and the generation it
selects. The Task still completes only on receipts its proof run takes at
that head, so an adoption proves integration and nothing more.

``runtime.delivery.read_back_merge``: the host read-back, done locally.

The reconciliation verb is handed an observation because nothing in the
tree talks to a host. When the target branch is a branch of the tree's
own repository -- a fast-forward landing, or a phase branch merged
locally -- the daemon can read it back itself. This verb reads the
branch's head and whether the Batch's pinned head is among its commits,
builds the observation from that, and hands it to the same
reconciliation, which decides the outcome exactly as it would for an
observation presented from a host.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from eawf.kernel.delivery.adoption import ADOPTION_STATUS, EvidenceRef, LandedAdoption
from eawf.kernel.delivery.integration import IntegrationGeneration
from eawf.kernel.delivery.receipts import RevisionBinding, RevisionRefKind, canonical_digest
from eawf.kernel.migration.epoch2.continuation import ledger_ids
from eawf.kernel.runtime.candidate import DELIVERABLE_VERDICTS
from eawf.kernel.state.enums import AgentReportVerdict
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey, StrictPositiveInt
from eawf.kernel.state.epoch2.batch import BatchStatus, DeliveryBatch
from eawf.kernel.state.epoch2.task import Task, TaskStatus
from eawf.kernel.state.epoch2.urns import BatchUrn, TaskUrn
from eawf.kernel.state.models import ShaStr
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.kinds.gate_receipt import GateIdentityStr
from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.delivery import (
    GENERATION_STATUS,
    IdempotencyKey,
    file_keyed_answer,
    keyed_answer,
    read_generation_ledger,
)
from eawf.runtime.daemon.methods.delivery_acceptance import (
    MergeReconcileAnswer,
    MergeReconcileParams,
    reconcile_batch_merge,
)
from eawf.runtime.daemon.methods.delivery_anchor import require_anchor
from eawf.runtime.daemon.native_guard import native_mutator, native_params
from eawf.runtime.integration.recovery import generation_record_key, integration_generation_id
from eawf.workflow.integration.reconcile import HostMergeObservation

logger = logging.getLogger(__name__)


#: The verb that adopts an already-landed change as a Batch's next generation.
DELIVERY_ADOPT_LANDED_METHOD: Final = "runtime.delivery.adopt_landed"

#: The verb that reads a Batch's target branch back from the tree's repository.
DELIVERY_READ_BACK_MERGE_METHOD: Final = "runtime.delivery.read_back_merge"

#: The ledgers an adoption's evidence references resolve against.
_EVIDENCE_COLLECTIONS: Final = (
    Epoch2Collection.AUDIT,
    Epoch2Collection.DECISION,
    Epoch2Collection.ARTIFACT,
    Epoch2Collection.EVIDENCE,
)

#: The Task statuses whose work an adoption may carry: started and not yet
#: finished, so an adoption never rewrites a completed or abandoned Task.
_ADOPTABLE_TASK_STATUSES: Final = frozenset({TaskStatus.RUNNING, TaskStatus.READY_TO_INTEGRATE})

#: The integration policy an adopted generation records.
_ADOPTION_POLICY: Final = canonical_digest({"integration": "adopt_landed", "version": 1})


class AdoptLandedParams(BaseModel):
    """Params of :data:`DELIVERY_ADOPT_LANDED_METHOD`.

    Attributes:
        urn: The Batch the change is adopted onto.
        actor: Who asked.
        idempotency_key: The client's name for this request.
        expected_revision: The revision the caller read the subject at, or
            ``None`` for a caller that sends no anchor. A stale one is
            refused with ``revision_conflict``.
        task_refs: The Tasks whose work the landed change carries.
        base_commit: The commit the change started from.
        head_sha: The landed commit.
        report_verdict: The verdict the adopted Runs' reports carried.
        evidence_refs: The recorded audits, decisions, artifacts or
            evidence rows that verdict rests on.
        affected_criterion_ids: The criteria the change invalidates.
            Omitted, every criterion of the adopted Tasks.
    """

    model_config = ConfigDict(extra="forbid")

    urn: BatchUrn
    actor: PrincipalKey
    idempotency_key: IdempotencyKey
    expected_revision: StrictPositiveInt | None = None
    task_refs: tuple[TaskUrn, ...] = Field(min_length=1)
    base_commit: ShaStr
    head_sha: ShaStr
    report_verdict: AgentReportVerdict
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    affected_criterion_ids: tuple[GateIdentityStr, ...] = ()

    @field_validator("report_verdict")
    @classmethod
    def _verdict_delivers(cls, value: AgentReportVerdict) -> AgentReportVerdict:
        """Refuse a verdict that does not propose the work for delivery.

        Raises:
            ValueError: The verdict is not one a delivery may rest on.
        """
        if value not in DELIVERABLE_VERDICTS:
            raise ValueError(f"verdict {value.value} does not propose the work for delivery")
        return value


class AdoptLandedAnswer(BaseModel):
    """What one adoption answers with.

    Attributes:
        batch_ref: The Batch the change was adopted onto.
        adoption_key: The Batch-ledger key the adoption is filed under.
        generation_id: The generation it selected.
        generation: That generation's ordinal.
        head_sha: The adopted head.
        tree_sha: Its tree.
        target_branch: The branch the head was found on.
        target_head_sha: That branch's head when it was read.
        changed_paths: How many paths the change touched.
        replayed: Whether the adoption was already filed.
        reason: One sentence an operator reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_ref: str
    adoption_key: str
    generation_id: str
    generation: int
    head_sha: str
    tree_sha: str
    target_branch: str
    target_head_sha: str
    changed_paths: int
    replayed: bool
    reason: str


class ReadBackParams(BaseModel):
    """Params of :data:`DELIVERY_READ_BACK_MERGE_METHOD`.

    Attributes:
        urn: The merging Batch.
        actor: Who asked.
        idempotency_key: The client's name for this request.
        expected_revision: The revision the caller read the subject at, or
            ``None`` for a caller that sends no anchor. A stale one is
            refused with ``revision_conflict``.
    """

    model_config = ConfigDict(extra="forbid")

    urn: BatchUrn
    actor: PrincipalKey
    idempotency_key: IdempotencyKey
    expected_revision: StrictPositiveInt | None = None


def _refused(code: str, detail: str) -> DaemonValidationError:
    """Return the wire form of one adoption or read-back refusal."""
    return DaemonValidationError(f"validation_failed: {code}: {detail}")


def _git(repository: Path, *args: str) -> str | None:
    """Return the trimmed output of one read-only git command, or ``None`` on failure."""
    done = subprocess.run(
        ["git", "-C", str(repository), *args], capture_output=True, text=True, check=False
    )
    return done.stdout.strip() if done.returncode == 0 else None


def _commit(repository: Path, sha: str) -> str:
    """Return *sha* when the repository holds it as a commit.

    Raises:
        DaemonValidationError: The repository holds no such commit.
    """
    resolved = _git(repository, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    if resolved != sha:
        raise _refused("adoption_commit_unknown", f"the repository holds no commit {sha}")
    return resolved


def _ancestor(repository: Path, older: str, newer: str) -> bool:
    """Return whether *older* is an ancestor of, or equal to, *newer*."""
    done = subprocess.run(
        ["git", "-C", str(repository), "merge-base", "--is-ancestor", older, newer],
        capture_output=True,
        check=False,
    )
    return done.returncode == 0


def _branch_head(repository: Path, branch: str) -> str | None:
    """Return the head of *branch*, local first, then its ``origin`` tracking ref."""
    for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        head = _git(repository, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if head:
            return head
    return None


def _batch_row(session: RootSession, urn: BatchUrn) -> DeliveryBatch:
    """Return the live Batch *urn* names.

    Raises:
        DaemonValidationError: The document holds no readable live Batch.
    """
    row = document_rows(session.read_document(), Epoch2Collection.BATCH).get(urn.entity_key)
    try:
        return DeliveryBatch.model_validate(row)
    except ValidationError as error:
        raise _refused(
            "adoption_batch_absent", f"the tree holds no readable live batch {urn.entity_key}"
        ) from error


def _adopted_tasks(
    session: RootSession, batch: DeliveryBatch, refs: tuple[TaskUrn, ...]
) -> tuple[Task, ...]:
    """Return the named Tasks, each live, placed in *batch* and started.

    Raises:
        DaemonValidationError: A Task is absent, placed elsewhere, or not
            in a status whose work an adoption may carry.
    """
    rows = document_rows(session.read_document(), Epoch2Collection.TASK)
    tasks: list[Task] = []
    for ref in refs:
        row = rows.get(ref.entity_key)
        task = None if row is None else Task.model_validate(row)
        if task is None or str(task.batch_ref) != str(batch.urn):
            raise _refused(
                "adoption_task_unplaced",
                f"task {ref.entity_key} is not a live task placed in batch {batch.key}",
            )
        if task.status not in _ADOPTABLE_TASK_STATUSES:
            raise _refused(
                "adoption_task_not_started",
                f"task {ref.entity_key} is {task.status.value}; only started work is adopted",
            )
        tasks.append(task)
    return tuple(tasks)


def _unresolved_evidence(session: RootSession, refs: tuple[str, ...]) -> list[str]:
    """Return the evidence references no ledger holds a record for."""
    held: set[str] = set()
    for collection in _EVIDENCE_COLLECTIONS:
        for line in read_ledger_records(session.ledger_path(collection)):
            held |= ledger_ids(line)
    return sorted(set(refs) - held)


def _binding(
    batch: DeliveryBatch,
    *,
    ref_kind: RevisionRefKind,
    head_sha: str,
    tree_sha: str,
    parent_sha: str | None,
    generation: int,
    manifest_digest: str,
    affected: tuple[str, ...],
    now: datetime,
) -> RevisionBinding:
    """Return one revision binding of *batch* at *generation*."""
    return RevisionBinding(
        repository_ref=batch.repository_ref,
        ref_kind=ref_kind,
        head_sha=head_sha,
        tree_sha=tree_sha,
        parent_sha=parent_sha,
        batch_ref=batch.urn,
        integration_generation=generation,
        manifest_digest=manifest_digest,
        criteria_digest=canonical_digest(list(affected)),
        policy_digest=_ADOPTION_POLICY,
        environment_digest=None,
        bound_at=now,
    )


def _read_repository(
    repository: Path, *, batch: DeliveryBatch, args: AdoptLandedParams, head_of: str | None
) -> tuple[str, str | None, str, str, str | None, tuple[str, ...], str]:
    """Read back everything the adoption states about the repository.

    Returns:
        The head's tree and parent, the target branch head, the base's
        tree and parent, the changed paths, and the digest of the diff.

    Raises:
        DaemonValidationError: A commit is unknown, the base does not
            precede the head, the target branch is unreadable or does not
            contain the head, the Batch head is not under the adopted head,
            or nothing changed.
    """
    head = _commit(repository, args.head_sha)
    base = _commit(repository, args.base_commit)
    if not _ancestor(repository, base, head):
        raise _refused("adoption_base_diverged", f"{base} is not an ancestor of {head}")
    branch = batch.target_branch
    assert branch is not None, "a live non-planned Batch always carries its target branch"
    target_head = _branch_head(repository, branch)
    if target_head is None:
        raise _refused("adoption_branch_unread", f"the repository holds no branch {branch}")
    if not _ancestor(repository, head, target_head):
        raise _refused(
            "adoption_head_unlanded",
            f"branch {branch} at {target_head} does not contain {head}, so it has not landed",
        )
    if head_of is not None and not _ancestor(repository, head_of, head):
        raise _refused(
            "adoption_head_behind",
            f"batch {batch.key} already stands on {head_of}, which {head} does not contain",
        )
    paths = tuple(
        line for line in (_git(repository, "diff", "--name-only", base, head) or "").splitlines()
    )
    if not paths:
        raise _refused("adoption_empty", f"{base}..{head} changes no path")
    diff = subprocess.run(
        ["git", "-C", str(repository), "diff", "--binary", base, head],
        capture_output=True,
        check=False,
    ).stdout
    return (
        _git(repository, "rev-parse", f"{head}^{{tree}}") or "",
        _git(repository, "rev-parse", "--verify", "--quiet", f"{head}^"),
        target_head,
        _git(repository, "rev-parse", f"{base}^{{tree}}") or "",
        _git(repository, "rev-parse", "--verify", "--quiet", f"{base}^"),
        paths,
        f"sha256:{hashlib.sha256(diff).hexdigest()}",
    )


def adopt_landed(
    context: Epoch2RootContext, args: AdoptLandedParams, *, now: datetime
) -> AdoptLandedAnswer:
    """Adopt one already-landed change as the Batch's next selected generation.

    Args:
        context: The native context of the tree the Batch lives in.
        args: The validated request.
        now: The stamp the adoption and its generation are filed at.

    Returns:
        The adoption and the generation it selected, or the standing ones
        when the same change was already adopted. A retry under the same
        idempotency key answers what the first call answered.

    Raises:
        DaemonValidationError: The key already answered other parameters,
            the Batch is not active, a Task cannot be adopted, evidence
            does not resolve, the repository does not show the change
            landed on the Batch's target branch, or another generation was
            selected while the repository was being read.
    """
    params = args.model_dump(mode="json")
    replayed = keyed_answer(
        context, method=DELIVERY_ADOPT_LANDED_METHOD, key=args.idempotency_key, params=params
    )
    if replayed is not None:
        logger.info(f"adopt_landed batch={args.urn.entity_key} replayed_key=True")
        return AdoptLandedAnswer.model_validate({**replayed, "replayed": True})
    with context.session([str(args.urn)]) as session:
        batch = _batch_row(session, args.urn)
        if batch.status is not BatchStatus.ACTIVE:
            raise _refused(
                "adoption_batch_not_active",
                f"batch {batch.key} is {batch.status.value}; a change is adopted onto an "
                "ACTIVE one",
            )
        tasks = _adopted_tasks(session, batch, args.task_refs)
        unresolved = _unresolved_evidence(session, args.evidence_refs)
        ledger = read_generation_ledger(session.ledger_path(Epoch2Collection.BATCH), args.urn)
    if unresolved:
        raise _refused(
            "adoption_evidence_unheld", f"no ledger holds a record for {', '.join(unresolved)}"
        )
    head = ledger.head
    branch = batch.target_branch
    assert branch is not None, "an ACTIVE Batch always carries its target branch"
    repository = context.identity.tree_root.parent
    tree, parent, target_head, base_tree, base_parent, paths, diff_digest = _read_repository(
        repository,
        batch=batch,
        args=args,
        head_of=None if head is None else head.integrated_revision.head_sha,
    )
    affected = args.affected_criterion_ids or tuple(
        sorted({criterion.id for task in tasks for criterion in task.criteria})
    )
    adoption = LandedAdoption(
        batch_ref=args.urn,
        task_refs=args.task_refs,
        base_commit=args.base_commit,
        head_sha=args.head_sha,
        tree_sha=tree,
        parent_sha=parent,
        target_branch=branch,
        target_head_sha=target_head,
        changed_paths=paths,
        report_verdict=args.report_verdict,
        evidence_refs=args.evidence_refs,
        adopted_by=args.actor,
        adopted_at=now,
    )
    if head is not None and head.integrated_revision.head_sha == args.head_sha:
        return _answer(adoption, head, target_head=target_head, replayed=True)
    ordinal = 2 if head is None else head.generation + 1
    manifest = adoption.digest()
    base = _binding(
        batch,
        ref_kind=RevisionRefKind.TARGET_BRANCH,
        head_sha=args.base_commit,
        tree_sha=base_tree,
        parent_sha=base_parent,
        generation=1,
        manifest_digest=manifest,
        affected=(),
        now=now,
    )
    generation = IntegrationGeneration(
        id=integration_generation_id(ordinal),
        batch_ref=args.urn,
        generation=ordinal,
        parent_generation_id=None if head is None else head.id,
        source_candidate_bundle_id=adoption.bundle_key(),
        source_base=base,
        target_base=base if head is None else head.integrated_revision,
        integrated_revision=_binding(
            batch,
            ref_kind=RevisionRefKind.INTEGRATION,
            head_sha=args.head_sha,
            tree_sha=tree,
            parent_sha=parent,
            generation=ordinal,
            manifest_digest=manifest,
            affected=affected,
            now=now,
        ),
        patch_digest=diff_digest,
        diff_digest=diff_digest,
        tree_digest=canonical_digest(tree),
        changed_paths=paths,
        affected_task_refs=args.task_refs,
        affected_criterion_ids=affected,
        integration_policy_digest=_ADOPTION_POLICY,
        selected=True,
        created_at=now,
    )
    with context.session([str(args.urn)]) as session:
        # The repository was read with no lock held, so the head the new
        # generation extends is checked again under the lock it is
        # appended under; a head that moved would give two generations one
        # ordinal, or this one the wrong parent.
        now_head = read_generation_ledger(
            session.ledger_path(Epoch2Collection.BATCH), args.urn
        ).head
        if now_head is not None and now_head.integrated_revision.head_sha == args.head_sha:
            return _answer(adoption, now_head, target_head=target_head, replayed=True)
        if (None if now_head is None else now_head.id) != (None if head is None else head.id):
            raise _refused(
                "adoption_head_moved",
                f"batch {batch.key} selected {now_head.id if now_head else 'no generation'} "
                f"while {args.head_sha} was being read back, so adopt it again on that head",
            )
        commit_ledger_append(
            session,
            LedgerRecord(
                collection=Epoch2Collection.BATCH,
                record_key=adoption.record_key(),
                status=ADOPTION_STATUS,
                recorded_at=now,
                payload=adoption.model_dump(mode="json"),
            ),
        )
        commit_ledger_append(
            session,
            LedgerRecord(
                collection=Epoch2Collection.BATCH,
                record_key=generation_record_key(generation),
                status=GENERATION_STATUS,
                recorded_at=now,
                payload=generation.model_dump(mode="json"),
            ),
        )
    logger.info(
        f"adopt_landed batch={batch.key} head={args.head_sha} generation={ordinal} "
        f"tasks={len(tasks)} paths={len(paths)}"
    )
    answer = _answer(adoption, generation, target_head=target_head, replayed=False)
    file_keyed_answer(
        context,
        method=DELIVERY_ADOPT_LANDED_METHOD,
        key=args.idempotency_key,
        params=params,
        answer=answer.model_dump(mode="json"),
        at=now,
    )
    return answer


def _answer(
    adoption: LandedAdoption,
    generation: IntegrationGeneration,
    *,
    target_head: str,
    replayed: bool,
) -> AdoptLandedAnswer:
    """Build the answer one adoption returns."""
    return AdoptLandedAnswer(
        batch_ref=str(adoption.batch_ref),
        adoption_key=adoption.record_key(),
        generation_id=generation.id,
        generation=generation.generation,
        head_sha=adoption.head_sha,
        tree_sha=adoption.tree_sha,
        target_branch=adoption.target_branch,
        target_head_sha=target_head,
        changed_paths=len(adoption.changed_paths),
        replayed=replayed,
        reason=(
            f"{adoption.head_sha} is already the head of batch {adoption.batch_ref.entity_key}"
            if replayed
            else f"{adoption.head_sha} landed on {adoption.target_branch} and is adopted as "
            f"generation {generation.generation} of batch {adoption.batch_ref.entity_key}"
        ),
    )


def read_back_merge(
    context: Epoch2RootContext, args: ReadBackParams, *, now: datetime
) -> MergeReconcileAnswer:
    """Read a merging Batch's target branch back from the repository and reconcile it.

    Args:
        context: The native context of the tree the Batch lives in.
        args: The validated request.
        now: When the branch was read.

    Returns:
        The reconciliation the observation decided.

    Raises:
        DaemonValidationError: The Batch is not a readable live Batch
            pinning a head, or the reconciliation refused.
    """
    with context.session([str(args.urn)]) as session:
        batch = _batch_row(session, args.urn)
    binding = batch.current_head_binding
    branch = batch.target_branch
    if binding is None or branch is None:
        raise _refused(
            "reconcile_batch_not_merging", f"batch {batch.key} pins no head to read back"
        )
    repository = context.identity.tree_root.parent
    target_head = _branch_head(repository, branch)
    carried = target_head is not None and _ancestor(repository, binding.head_sha, target_head)
    contained: tuple[str, ...] = ()
    if target_head is not None:
        contained = (target_head,) + (
            (binding.head_sha,) if carried and binding.head_sha != target_head else ()
        )
    observation = HostMergeObservation(
        batch_ref=args.urn,
        target_branch=branch,
        observed_at=now,
        target_head_sha=target_head,
        contained_shas=contained,
    )
    logger.info(f"read_back_merge batch={batch.key} branch_read={target_head is not None}")
    return reconcile_batch_merge(
        context,
        MergeReconcileParams(
            urn=args.urn,
            actor=args.actor,
            idempotency_key=args.idempotency_key,
            observation=observation,
        ),
    )


@native_mutator(DELIVERY_ADOPT_LANDED_METHOD)
async def _adopt_landed(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Adopt one already-landed change as a Batch's next generation."""
    args = native_params(AdoptLandedParams, params)
    context = ctx.native_root_context(authority.root)
    await asyncio.to_thread(require_anchor, context, args.urn, args.expected_revision)
    answer = await asyncio.to_thread(adopt_landed, context, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


@native_mutator(DELIVERY_READ_BACK_MERGE_METHOD)
async def _read_back_merge(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Read a merging Batch's target branch back and reconcile it."""
    args = native_params(ReadBackParams, params)
    context = ctx.native_root_context(authority.root)
    await asyncio.to_thread(require_anchor, context, args.urn, args.expected_revision)
    answer = await asyncio.to_thread(read_back_merge, context, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


__all__ = [
    "DELIVERY_ADOPT_LANDED_METHOD",
    "DELIVERY_READ_BACK_MERGE_METHOD",
    "AdoptLandedAnswer",
    "AdoptLandedParams",
    "ReadBackParams",
    "adopt_landed",
    "read_back_merge",
]
