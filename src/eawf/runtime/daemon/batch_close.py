"""Closing a Delivery Batch as soon as its Tasks are ready and their work has landed.

Work reaches the phase branch by cherry-pick, so by the time every Task of a Batch is
ready and its commits are on that branch there is nothing left for the merge verbs to
decide. The daemon then pins the branch head and walks the Batch PLANNED -> ACTIVE ->
READY_TO_MERGE -> MERGING -> MERGED_PENDING_RECONCILIATION -> COMPLETED itself, and
completes its ready Tasks on that same head, so a console never shows landed work as a
Batch still waiting to start.

A Task's work has landed when a commit carrying its ``Task:`` trailer is an ancestor of
the head. A Task for which no local branch carries a trailered commit counts as landed
only on positive evidence it delivered a decision or bookkeeping rather than code: every
workspace leased to its Run holds nothing, or, with no lease, it claims only paths the
commit policy keeps local or git ignores. The phase
branch is the Batch's target branch, or the repository's current branch for a Batch not
yet activated, which the activation then pins.

Every edge is judged as its own verb judges it. The guards a document answers are
evaluated against a locked read before each move, the active-Batch ceiling is judged
again by the transaction under its own lock, and the merge is observed only on a landed
read-back of the branch, filed by the same reconciliation ``batch reconcile`` files. That
ceiling counts only Batches holding or building an exact-head proof, so an ACTIVE Batch
still collecting Tasks never keeps a landed one open. The closes are serialised in this
process, so a Batch holds its repository's slot only for the length of its own close; a
Batch kept out by another Batch's proof is left PLANNED and says why.

A Batch's Tasks complete before the Batch's own last edge, so an interrupted close
leaves a Batch that the next attempt finishes rather than a completed Batch with ready
Tasks nothing revisits.

Nothing moves until every criterion of a ready Task that names a verify command is
proved at the head, by :mod:`~eawf.runtime.daemon.batch_proof`. A proof still running,
or a criterion unmet, leaves the Batch where it stands and says so, so a failing proof
never leaves a Batch half walked; each Task completes citing the receipts it passed on.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise, takewhile
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from eawf.kernel.delivery.receipts import canonical_digest
from eawf.kernel.identity import EntityKind, QualifiedUrn
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.runtime.lease import WorkLease
from eawf.kernel.state.epoch2.base import BatchKey, PrincipalKey
from eawf.kernel.state.epoch2.batch import BatchStatus, DeliveryBatch
from eawf.kernel.state.epoch2.lifecycle import LifecycleRecord
from eawf.kernel.state.epoch2.milestone import Milestone, MilestoneStatus
from eawf.kernel.state.epoch2.run import Run, TaskScope
from eawf.kernel.state.epoch2.task import Task, TaskStatus
from eawf.kernel.state.epoch2.track import Track
from eawf.kernel.state.epoch2.transitions import (
    LifecycleEntity,
    LifecycleStatus,
    ObservedFact,
    TransitionGuard,
    row_for,
)
from eawf.kernel.state.epoch2.values import ExactRevisionBinding
from eawf.kernel.store.commit_policy import (
    CENSUS_SURFACE_PREFIX,
    PROBE_SEGMENT,
    CommitPolicy,
    CommitPolicyError,
    classify_path,
)
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.runtime.daemon.batch_proof import (
    ProofOutcome,
    discard_checkout,
    judge_proof,
    prune_proofs,
)
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import (
    TransactionRefusedError,
    TransitionRequest,
    run_transaction,
)
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.delivery_landed import ReadBackParams, read_back_merge
from eawf.runtime.daemon.methods.domain_guards import GUARD_COMPUTERS, GuardInputs
from eawf.runtime.integration.git_workspace import COMMIT_ARTIFACT_PREFIX
from eawf.runtime.workspace.lease import lease_residue, root_leases
from eawf.workflow.integration.reconcile import MergeOutcome

logger = logging.getLogger(__name__)

#: The edges a close walks, in order; a Batch resumes from wherever it stands.
_CLOSE_PATH: Final = (
    BatchStatus.PLANNED,
    BatchStatus.ACTIVE,
    BatchStatus.READY_TO_MERGE,
    BatchStatus.MERGING,
    BatchStatus.MERGED_PENDING_RECONCILIATION,
    BatchStatus.COMPLETED,
)

#: The Task statuses a closing Batch may hold. A completed or cancelled Task needs no
#: landing; a ready one is completed by the close.
_CLOSABLE_TASK_STATUSES: Final = frozenset(
    {
        TaskStatus.READY_TO_INTEGRATE.value,
        TaskStatus.COMPLETED.value,
        TaskStatus.CANCELLED.value,
    }
)

#: The characters that make a write-claim segment a pattern rather than a literal name.
_WILDCARD: Final = frozenset("*?[")

#: One close at a time in this process: the supervisor's trigger and the operator's verb
#: may race, and two closes interleaving their edges would contend for the one slot.
_CLOSING: Final = threading.Lock()


class BatchClosure(BaseModel):
    """What one close attempt did to one Batch.

    Attributes:
        batch_key: The Batch.
        closed: Whether the Batch is COMPLETED after the attempt.
        reason: One sentence an operator reads: the head it closed on, or why not.
        head_sha: The head the Batch was closed on, when it closed.
        completed_task_keys: The Tasks the close moved to COMPLETED.
        proof: How the proof of its criteria stood: ``proving``, ``proved``, ``unmet``,
            ``over budget`` or ``crashed``; ``None`` when no criterion names a verify command or
            the close stopped before proving.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_key: str
    closed: bool
    reason: str
    head_sha: str | None = None
    completed_task_keys: tuple[str, ...] = ()
    proof: str | None = None


class _HaltedError(Exception):
    """A close stopped short, with the sentence naming why."""


@dataclass(frozen=True, slots=True)
class _Member:
    """One Task of the Batch: its key, where it stands, and its live row if it has one."""

    key: str
    status: str | None
    task: Task | None


def _git(repository: Path, *args: str) -> str | None:
    """Return the trimmed output of one read-only git command, or ``None`` on failure."""
    done = subprocess.run(
        ["git", "-C", str(repository), *args],
        capture_output=True,
        text=True,
        check=False,
        **no_window_kwargs(),
    )
    return done.stdout.strip() if done.returncode == 0 else None


def _trailered(repository: Path, task_key: str, *revisions: str) -> str | None:
    """Return the newest commit reachable from *revisions* naming *task_key* in a trailer."""
    pattern = f"--grep=^Task:[[:space:]]+{task_key}[[:space:]]*$"
    found = _git(repository, "log", "-n1", "--format=%H", "-E", pattern, *revisions)
    return found or None


def _phase_branch(repository: Path, batch: DeliveryBatch) -> str:
    """Return the branch the Batch's work lands on.

    Raises:
        _HaltedError: The Batch names no branch and ``HEAD`` is detached.
    """
    if batch.target_branch is not None:
        return batch.target_branch
    current = _git(repository, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not current:
        raise _HaltedError("names no target branch and the repository's HEAD names none")
    return current


def _members(
    document: dict[str, Any], ledger: Mapping[str, str], batch: DeliveryBatch
) -> tuple[_Member, ...]:
    """Return every Task of *batch*, listed by it or placed in it, in key order.

    A Task that completed or was cancelled has left the document for its ledger, so its
    status is read there; a Task neither tier holds stands with no status at all.
    """
    rows = document_rows(document, Epoch2Collection.TASK)
    wanted = str(batch.urn)
    placed = {key for key, row in rows.items() if row.get("batch_ref") == wanted}
    members: list[_Member] = []
    for key in sorted(placed | {ref.entity_key for ref in batch.task_refs}):
        row = rows.get(key)
        if row is None:
            members.append(_Member(key=key, status=ledger.get(key), task=None))
            continue
        task = Task.model_validate(row)
        members.append(_Member(key=key, status=task.status.value, task=task))
    return tuple(members)


def _unsettled(members: Sequence[_Member]) -> str | None:
    """Return why the Batch's Tasks do not let it close, or ``None`` when they do."""
    if not members:
        return "holds no Task"
    for member in members:
        if member.status not in _CLOSABLE_TASK_STATUSES:
            return f"{member.key} {member.status or 'missing'}"
    return None


def _untrackable(repository: Path, claim: str) -> bool:
    """Return whether nothing written under the write claim *claim* can ever be committed.

    The claim is judged on its literal prefix, the segments before its first wildcard,
    probed one segment below: under ``.ea/`` the commit policy must declare it not
    committed, elsewhere git must ignore it. A claim with no literal prefix covers
    trackable paths.
    """
    segments = claim.split("/")
    literal = list(takewhile(lambda segment: not _WILDCARD.intersection(segment), segments))
    if not literal:
        return False
    probe = "/".join((*literal, PROBE_SEGMENT))
    if probe.startswith(CENSUS_SURFACE_PREFIX):
        try:
            return classify_path(probe).policy is CommitPolicy.NOT_COMMITTED
        except CommitPolicyError:
            return False
    return _git(repository, "check-ignore", "-q", "--no-index", probe) is not None


def _left_no_code(context: Epoch2RootContext, task: Task, leases: Sequence[WorkLease]) -> bool:
    """Return whether *task* shows positive evidence that it produced no code.

    The workspaces leased to the Task's Run must each hold nothing; a Task whose Run held
    no workspace -- a host-driven one -- shows it only by claiming no path a commit could
    carry.
    """
    run = None if task.active_run_ref is None else str(task.active_run_ref)
    held = [
        lease
        for lease in leases
        if str(lease.task_ref) == str(task.urn) and (run is None or str(lease.run_ref) == run)
    ]
    if not held:
        repository = context.identity.tree_root.parent
        return all(_untrackable(repository, claim) for claim in task.write_claims)
    return all(lease_residue(context, lease=lease) is None for lease in held)


def _landings(
    context: Epoch2RootContext, members: Sequence[_Member], *, head: str, branch: str
) -> dict[str, str | None]:
    """Return the commit carrying each ready Task on *head*, ``None`` for an evidence-only one.

    A ready Task no local branch carries a trailered commit for is evidence-only only on
    positive evidence that it produced no code, so edits a Run left uncommitted, or a
    host Task readied before its commit exists, keep the Batch open.

    Raises:
        _HaltedError: A ready Task's trailered commit is on a local branch but not under
            *head*, or a ready Task with no such commit may have produced code.
    """
    repository = context.identity.tree_root.parent
    leases = root_leases(context)
    landed: dict[str, str | None] = {}
    for member in members:
        if member.status != TaskStatus.READY_TO_INTEGRATE.value:
            continue
        commit = _trailered(repository, member.key, head)
        if commit is None:
            stray = _trailered(repository, member.key, "--branches")
            if stray is not None:
                raise _HaltedError(f"{member.key} commit {stray[:12]} not on {branch}")
            assert member.task is not None, "a ready Task is read from its live row"
            if not _left_no_code(context, member.task, leases):
                raise _HaltedError(f"{member.key} has no commit on {branch}")
        landed[member.key] = commit
    return landed


def _contract(task: Task) -> dict[str, Any]:
    """Return the part of *task* a binding's contract digest covers."""
    return {
        "task": task.key,
        "contract_revision": task.contract_revision,
        "criteria": [criterion.model_dump(mode="json") for criterion in task.criteria],
    }


def _policy_revision(document: dict[str, Any], batch: DeliveryBatch) -> int:
    """Return the policy revision of the Track the Batch's Milestone sits under.

    Raises:
        _HaltedError: The Milestone or its Track is not a live row.
    """
    milestone = document_rows(document, Epoch2Collection.MILESTONE).get(
        batch.milestone_ref.entity_key
    )
    track = None
    if milestone is not None:
        track_key = Milestone.model_validate(milestone).primary_track_ref.entity_key
        track = document_rows(document, Epoch2Collection.TRACK).get(track_key)
    if track is None:
        raise _HaltedError(f"cannot read the Track policy of {batch.milestone_ref.entity_key}")
    return Track.model_validate(track).policy.revision


def _head_binding(
    repository: Path,
    head: str,
    *,
    members: Sequence[_Member],
    landed: Mapping[str, str | None],
    policy_revision: int,
) -> ExactRevisionBinding:
    """Return the exact binding the Batch is declared ready on.

    The contract digest covers the contracts of the Batch's live Tasks and the evidence
    digest covers which commit carries each ready Task, so a later Task change or a
    different landing produces a different binding.
    """
    tree = _git(repository, "rev-parse", f"{head}^{{tree}}")
    assert tree, "a head read off a branch always has a tree"
    return ExactRevisionBinding(
        head_sha=head,
        tree_sha=tree,
        contract_digest=canonical_digest(
            [_contract(member.task) for member in members if member.task is not None]
        ),
        policy_revision=policy_revision,
        evidence_digest=canonical_digest(
            [{"task": key, "commit": commit} for key, commit in sorted(landed.items())]
        ),
    )


def _task_binding(head: ExactRevisionBinding, task: Task, commit: str | None) -> dict[str, Any]:
    """Return the integrated binding a Task completes on, at the Batch's pinned head."""
    return ExactRevisionBinding(
        head_sha=head.head_sha,
        tree_sha=head.tree_sha,
        contract_digest=canonical_digest(_contract(task)),
        policy_revision=task.contract_revision,
        evidence_digest=canonical_digest({"task": task.key, "commit": commit}),
    ).model_dump(mode="json")


def _unmet(
    document: dict[str, Any],
    document_path: Path,
    *,
    entity: LifecycleEntity,
    record: LifecycleRecord,
    to: LifecycleStatus,
    updates: Mapping[str, Any],
    actor: str,
    binding_refs: tuple[str, ...],
) -> TransitionGuard | None:
    """Return the first guard of the edge the document answers as unmet, if any."""
    row = row_for(entity, record.status, to)
    assert row is not None, "the close walks only registered edges"
    inputs = GuardInputs(
        document=document,
        document_path=document_path,
        record=record,
        updates=updates,
        reason_code=None,
        actor=actor,
        binding_refs=binding_refs,
    )
    for guard in row.guards:
        computer = GUARD_COMPUTERS.get(guard)
        if computer is not None and not computer(inputs):
            return guard
    return None


def _move(
    context: Epoch2RootContext,
    urn: QualifiedUrn,
    *,
    to: LifecycleStatus,
    actor: PrincipalKey,
    now: datetime,
    updates: Mapping[str, Any] | None = None,
    observations: tuple[ObservedFact, ...] = (),
    binding_refs: tuple[str, ...] = (),
) -> Envelope | None:
    """Take one edge of a Batch or Task, judged against a fresh locked read.

    Returns:
        The committed envelope, or ``None`` when the transaction replayed one.

    Raises:
        _HaltedError: A guard the document answers does not hold.
        TransactionRefusedError: The transaction refused the edge.
    """
    with context.session([urn]) as session:
        document = session.read_document()
        document_path = session.document_path
    batch = urn.kind is EntityKind.BATCH
    collection = Epoch2Collection.BATCH if batch else Epoch2Collection.TASK
    row = document_rows(document, collection)[urn.entity_key]
    record: LifecycleRecord = (DeliveryBatch if batch else Task).model_validate(row)
    entity = LifecycleEntity.DELIVERY_BATCH if batch else LifecycleEntity.TASK
    written = dict(updates or {})
    unmet = _unmet(
        document,
        document_path,
        entity=entity,
        record=record,
        to=to,
        updates=written,
        actor=actor,
        binding_refs=binding_refs,
    )
    if unmet is not None:
        raise _HaltedError(f"{record.key} {record.status!s} -> {to!s} is blocked by {unmet.value}")
    committed = run_transaction(
        context=context,
        request=TransitionRequest(
            urn=urn,
            to_status=str(to),
            expected_revision=record.revision,
            idempotency_key=f"close-{record.key}-{str(to).lower()}-r{record.revision}",
            actor=actor,
            observations=observations,
            updates=written,
            binding_refs=binding_refs,
        ),
        now=now,
    )
    return committed.envelope


def _read_back(
    context: Epoch2RootContext, batch: DeliveryBatch, *, actor: PrincipalKey, now: datetime
) -> None:
    """File the landed read-back of the Batch's branch that its merge is observed on.

    Raises:
        _HaltedError: The branch does not carry the head the Batch pinned.
        DaemonValidationError: The reconciliation refused.
    """
    answer = read_back_merge(
        context,
        ReadBackParams(
            urn=batch.urn,
            actor=actor,
            idempotency_key=f"close-{batch.key}-read-back-r{batch.revision}",
        ),
        now=now,
    )
    if answer.outcome != MergeOutcome.LANDED.value:
        raise _HaltedError(answer.reason)


def _batch_now(context: Epoch2RootContext, urn: QualifiedUrn) -> DeliveryBatch:
    """Return the live Batch *urn* names, as the document holds it now."""
    with context.session([urn]) as session:
        row = document_rows(session.read_document(), Epoch2Collection.BATCH)[urn.entity_key]
    return DeliveryBatch.model_validate(row)


def _complete_tasks(
    context: Epoch2RootContext,
    batch: DeliveryBatch,
    *,
    landed: Mapping[str, str | None],
    citations: Mapping[str, tuple[str, ...]],
    progress: _Progress,
    actor: PrincipalKey,
    now: datetime,
) -> None:
    """Complete each ready Task of the Batch on the head the Batch pinned, citing its receipts."""
    binding = batch.current_head_binding
    assert binding is not None, "a merged Batch always carries its head binding"
    with context.session([batch.urn]) as session:
        rows = document_rows(session.read_document(), Epoch2Collection.TASK)
    for key in sorted(landed):
        row = rows.get(key)
        task = None if row is None else Task.model_validate(row)
        if task is None or task.status is not TaskStatus.READY_TO_INTEGRATE:
            continue
        commit = landed[key]
        carried = () if commit is None else (f"{COMMIT_ARTIFACT_PREFIX}{commit}",)
        progress.add(
            _move(
                context,
                task.urn,
                to=TaskStatus.COMPLETED,
                actor=actor,
                now=now,
                updates={"integrated_binding": _task_binding(binding, task, commit)},
                binding_refs=(str(batch.urn), *carried, *citations.get(key, ())),
            )
        )
        progress.completed.append(key)


@dataclass(frozen=True, slots=True)
class _Landing:
    """Where a closable Batch lands: its branch, the binding it closes on, its ready Tasks."""

    branch: str
    binding: ExactRevisionBinding
    commits: Mapping[str, str | None]
    ready: tuple[Task, ...]


@dataclass(slots=True)
class _Progress:
    """What a close has committed so far, kept when it stops partway."""

    envelopes: list[Envelope] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)

    def add(self, envelope: Envelope | None) -> None:
        """Keep one committed edge's envelope; a replayed edge has none."""
        if envelope is not None:
            self.envelopes.append(envelope)


def _landing(
    context: Epoch2RootContext,
    batch: DeliveryBatch,
    *,
    document: dict[str, Any],
    ledger: Mapping[str, str],
) -> _Landing:
    """Return where *batch* lands, refusing a Batch whose Tasks are not ready or not landed.

    A Batch that already pinned a head is judged on that head, so a close resumed after
    the branch moved on finishes on the head it started from.

    Raises:
        _HaltedError: A Task is not ready, a ready Task has not landed, or the branch or
            the Track policy cannot be read.
    """
    members = _members(document, ledger, batch)
    unsettled = _unsettled(members)
    if unsettled is not None:
        raise _HaltedError(unsettled)
    repository = context.identity.tree_root.parent
    branch = _phase_branch(repository, batch)
    pinned = batch.current_head_binding
    head = pinned.head_sha if pinned is not None else _branch_head(repository, branch)
    commits = _landings(context, members, head=head, branch=branch)
    binding = pinned or _head_binding(
        repository,
        head,
        members=members,
        landed=commits,
        policy_revision=_policy_revision(document, batch),
    )
    ready = tuple(
        member.task
        for member in members
        if member.task is not None and member.status == TaskStatus.READY_TO_INTEGRATE.value
    )
    return _Landing(branch=branch, binding=binding, commits=commits, ready=ready)


def _branch_head(repository: Path, branch: str) -> str:
    """Return the head of the local *branch*.

    Raises:
        _HaltedError: The repository holds no such branch.
    """
    head = _git(repository, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}")
    if not head:
        raise _HaltedError(f"finds no branch {branch} in the repository")
    return head


def _walk(
    context: Epoch2RootContext,
    batch: DeliveryBatch,
    landing: _Landing,
    progress: _Progress,
    *,
    proof: ProofOutcome,
    actor: PrincipalKey,
    now: datetime,
) -> None:
    """Take every edge from where *batch* stands to COMPLETED, recording each in *progress*.

    Raises:
        _HaltedError: A guard the document answers does not hold, or the read-back did
            not find the pinned head on the branch.
        TransactionRefusedError: The transaction refused an edge.
        DaemonValidationError: The reconciliation refused.
    """
    for frm, to in pairwise(_CLOSE_PATH[_CLOSE_PATH.index(batch.status) :]):
        updates: dict[str, Any] = {}
        observations: tuple[ObservedFact, ...] = ()
        if frm is BatchStatus.PLANNED:
            updates = {"target_branch": landing.branch}
        elif frm is BatchStatus.ACTIVE:
            updates = {"current_head_binding": landing.binding.model_dump(mode="json")}
        elif frm is BatchStatus.MERGING:
            _read_back(context, _batch_now(context, batch.urn), actor=actor, now=now)
            observations = (ObservedFact.HOST_MERGE_OBSERVED,)
        elif frm is BatchStatus.MERGED_PENDING_RECONCILIATION:
            _complete_tasks(
                context,
                _batch_now(context, batch.urn),
                landed=landing.commits,
                citations=proof.citations,
                progress=progress,
                actor=actor,
                now=now,
            )
        progress.add(
            _move(
                context,
                batch.urn,
                to=to,
                actor=actor,
                now=now,
                updates=updates,
                observations=observations,
            )
        )


def _proof(
    context: Epoch2RootContext,
    batch: DeliveryBatch,
    landing: _Landing,
    *,
    rerun_failed: bool,
    now: datetime,
) -> ProofOutcome:
    """Return how the proof of the Batch's ready Tasks stands at the head it lands on."""
    repository = context.identity.tree_root.parent
    head = landing.binding.head_sha
    return judge_proof(
        context,
        batch,
        landing.ready,
        head=head,
        tree=landing.binding.tree_sha,
        parent=_git(repository, "rev-parse", "--verify", "--quiet", f"{head}^"),
        rerun_failed=rerun_failed,
        now=now,
    )


def _close(
    context: Epoch2RootContext,
    urn: QualifiedUrn,
    *,
    actor: PrincipalKey,
    now: datetime,
    rerun_failed: bool,
) -> tuple[BatchClosure, tuple[Envelope, ...]]:
    """Close one Batch if its Tasks are ready and landed; see :func:`close_landed_batch`."""
    with context.session([urn]) as session:
        document = session.read_document()
        lines = read_ledger_records(session.ledger_path(Epoch2Collection.TASK))
    ledger = {item.record_key: item.status for item in effective_records(lines)}
    row = document_rows(document, Epoch2Collection.BATCH).get(urn.entity_key)
    if row is None:
        return BatchClosure(
            batch_key=urn.entity_key, closed=False, reason="is not a live batch"
        ), ()
    batch = DeliveryBatch.model_validate(row)
    progress = _Progress()
    proof: ProofOutcome | None = None
    try:
        landing = _landing(context, batch, document=document, ledger=ledger)
        proof = _proof(context, batch, landing, rerun_failed=rerun_failed, now=now)
        if proof.state not in {None, "proved"}:
            raise _HaltedError(proof.reason)
        _walk(context, batch, landing, progress, proof=proof, actor=actor, now=now)
    except _HaltedError as halted:
        reason = str(halted)
    except (TransactionRefusedError, DaemonValidationError) as refusal:
        reason = str(refusal)
    else:
        head = landing.binding.head_sha
        discard_checkout(context, str(batch.urn))
        logger.info(f"close_landed_batch closed batch={batch.key} head={head}")
        closure = BatchClosure(
            batch_key=batch.key,
            closed=True,
            reason=f"closed on {head[:12]}",
            head_sha=head,
            completed_task_keys=tuple(progress.completed),
            proof=proof.state,
        )
        return closure, tuple(progress.envelopes)
    logger.info(f"close_landed_batch kept batch={batch.key} reason={reason!r}")
    closure = BatchClosure(
        batch_key=batch.key,
        closed=False,
        reason=reason,
        completed_task_keys=tuple(progress.completed),
        proof=None if proof is None else proof.state,
    )
    return closure, tuple(progress.envelopes)


def close_landed_batch(
    context: Epoch2RootContext,
    urn: QualifiedUrn,
    *,
    actor: PrincipalKey,
    now: datetime,
    rerun_failed: bool = False,
) -> tuple[BatchClosure, tuple[Envelope, ...]]:
    """Close one Batch whose Tasks are all ready and whose work is on its phase branch.

    Nothing moves unless every Task is ready, completed or cancelled, every ready Task
    has landed and every criterion naming a verify command is proved at the head; a
    refused edge stops the walk where it stands, and the next attempt resumes from there.

    Args:
        context: The native context of the tree the Batch lives in.
        urn: The Batch.
        actor: The principal the close is attributed to.
        now: The stamp every edge of the close shares.
        rerun_failed: Prove again a criterion whose receipt at this head failed.

    Returns:
        What the attempt did, beside the envelopes its committed edges produced.
    """
    with _CLOSING:
        return _close(context, urn, actor=actor, now=now, rerun_failed=rerun_failed)


def _open_batches(context: Epoch2RootContext) -> dict[str, tuple[DeliveryBatch, str | None]]:
    """Return every open Batch of an ACTIVE Milestone, by key, with why it cannot close.

    The document is read without its lock, so a sweep over many Batches takes the lock
    only for the Batches whose Tasks already let them close; the close itself judges
    each one again under the lock.

    Returns:
        Each Batch beside the reason its Tasks keep it open, or ``None`` when they do not.
    """
    authority = context.require_selected_generation()
    assert authority.target is not None and authority.generation_id is not None
    path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    document = read_document(path)
    lines = read_ledger_records(ledger_path(path, Epoch2Collection.TASK))
    ledger = {item.record_key: item.status for item in effective_records(lines)}
    milestones = document_rows(document, Epoch2Collection.MILESTONE)
    batches: dict[str, tuple[DeliveryBatch, str | None]] = {}
    for key, row in document_rows(document, Epoch2Collection.BATCH).items():
        batch = DeliveryBatch.model_validate(row)
        milestone = milestones.get(batch.milestone_ref.entity_key)
        if batch.status not in _CLOSE_PATH[:-1] or milestone is None:
            continue
        if milestone.get("status") == MilestoneStatus.ACTIVE.value:
            batches[key] = (batch, _unsettled(_members(document, ledger, batch)))
    return batches


def close_landed_batches(
    context: Epoch2RootContext,
    batch_keys: Sequence[BatchKey],
    *,
    actor: PrincipalKey,
    now: datetime,
    rerun_failed: bool = False,
) -> tuple[tuple[BatchClosure, ...], tuple[Envelope, ...]]:
    """Close each named Batch, or every open Batch of an ACTIVE Milestone, that has landed.

    Batches already past PLANNED go first: one an interrupted close left on its pinned
    head holds the repository's slot, and closing it is what lets a PLANNED Batch behind
    it through. A pass over every open Batch also forgets the proof of each Batch that
    is no longer eligible for one, so its console note and checkout go with it.

    Args:
        context: The native context of the tree.
        batch_keys: The Batches to close; empty means every open Batch of every ACTIVE
            Milestone.
        actor: The principal the closes are attributed to.
        now: The stamp every edge shares.
        rerun_failed: Prove again a criterion whose receipt at its head failed.

    Returns:
        One closure per Batch attempted, in attempt order, beside the envelopes.
    """
    live = _open_batches(context)
    closures: list[BatchClosure] = []
    envelopes: list[Envelope] = []
    if batch_keys:
        wanted = list(dict.fromkeys(batch_keys))
    else:
        wanted = sorted(live, key=lambda key: (live[key][0].status is BatchStatus.PLANNED, key))
        eligible = frozenset(
            str(batch.urn) for batch, unsettled in live.values() if unsettled is None
        )
        prune_proofs(context, eligible)
    for key in wanted:
        if key not in live:
            reason = "is not an open batch of an active milestone"
            closures.append(BatchClosure(batch_key=key, closed=False, reason=reason))
            continue
        batch, unsettled = live[key]
        if unsettled is not None:
            closures.append(BatchClosure(batch_key=key, closed=False, reason=unsettled))
            continue
        closure, moved = close_landed_batch(
            context, batch.urn, actor=actor, now=now, rerun_failed=rerun_failed
        )
        closures.append(closure)
        envelopes.extend(moved)
    return tuple(closures), tuple(envelopes)


def close_after_ready(
    context: Epoch2RootContext, run: Run, *, actor: PrincipalKey, now: datetime
) -> tuple[Envelope, ...]:
    """Close the Batch a completed Run's ready Task sits in, then any Batch that waited on it.

    Args:
        context: The native context of the tree.
        run: The Run that just completed on a delivering report.
        actor: The principal the Run was dispatched under.
        now: The stamp every edge shares.

    Returns:
        The envelopes the committed edges produced; none when the Task is not ready.
    """
    assert isinstance(run.scope, TaskScope), "only a task-scoped Run is dispatched natively"
    task_ref = run.scope.task_ref
    with context.session([task_ref]) as session:
        row = document_rows(session.read_document(), Epoch2Collection.TASK).get(task_ref.entity_key)
    task = None if row is None else Task.model_validate(row)
    if task is None or task.status is not TaskStatus.READY_TO_INTEGRATE:
        return ()
    assert task.batch_ref is not None, "a ready Task always sits in a Batch"
    closure, envelopes = close_landed_batch(context, task.batch_ref, actor=actor, now=now)
    if not closure.closed:
        return envelopes
    _, more = close_landed_batches(context, (), actor=actor, now=now)
    return envelopes + more


__all__ = [
    "BatchClosure",
    "close_after_ready",
    "close_landed_batch",
    "close_landed_batches",
]
