"""The guards a per-entity lifecycle verb computes from the document.

The transition registry is a pure table and its reducer defaults a
non-observation guard to satisfied, which means the entity-agnostic path
admits an edge whose computable predicate is false. The predicates here
are the ones a document can answer; :mod:`eawf.runtime.daemon.methods.domain`
evaluates them in the registry's declaration order before a verb's
transaction runs.

A predicate about a sibling record reads the ledgers as well as the
document. A record that completes is compacted out of the document into
its append-only ledger, so the lookup falls through to the collection's
standing ledger lines, and a ledger it cannot read answers nothing at
all -- which shuts the edge, exactly as an absent row does.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from eawf.kernel.identity import EntityKind, IdentityError, parse_qualified_urn
from eawf.kernel.state.epoch2.batch import BatchStatus, DeliveryBatch
from eawf.kernel.state.epoch2.milestone import Milestone, MilestoneStatus
from eawf.kernel.state.epoch2.policy import TrackPolicy
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.kernel.state.epoch2.task import Task, TaskStatus
from eawf.kernel.state.epoch2.track import Track, TrackStatus
from eawf.kernel.state.epoch2.transitions import TransitionGuard
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.ledger import LedgerError, effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.workflow.integration.reconcile import (
    RECONCILIATION_KEY_PREFIX,
    MergeOutcome,
    MergeReconciliation,
)
from eawf.workflow.lifecycle.epoch2 import LifecycleRecord

logger = logging.getLogger(__name__)


#: The Milestone statuses that stop a Track from counting it as open. The
#: pair is spelled out rather than read off the terminal set because the
#: remediation an operator is handed says "complete or cancel", and a
#: Milestone that reached neither is still work under the Track.
_CLOSED_MILESTONE_STATUSES: Final[frozenset[str]] = frozenset(
    {MilestoneStatus.COMPLETED.value, MilestoneStatus.CANCELLED.value}
)

#: The Batch statuses that satisfy a Milestone's required-Batch guard. A
#: failed Batch is terminal and is not a completed one, so acceptance
#: review stays shut until it is redriven or cancelled.
_CLOSED_BATCH_STATUSES: Final[frozenset[str]] = frozenset(
    {BatchStatus.COMPLETED.value, BatchStatus.CANCELLED.value}
)

#: The Task statuses that let a Batch claim every Task in it is settled.
#: A failed Task is not settled: the Batch that carries it is not ready to
#: merge until the failure is replanned or the Task is cancelled.
_SETTLED_TASK_STATUSES: Final[frozenset[str]] = frozenset(
    {
        TaskStatus.READY_TO_INTEGRATE.value,
        TaskStatus.COMPLETED.value,
        TaskStatus.CANCELLED.value,
    }
)


@dataclass(frozen=True, slots=True)
class GuardInputs:
    """Everything a computable predicate is allowed to read.

    Attributes:
        document: The locked document the mutation would land in.
        document_path: The selected generation's document, which the
            ledgers holding its compacted records resolve against.
        record: The subject, validated through its own model.
        updates: The field values the request supplies.
        reason_code: The stable reason the request carries, if any.
        actor: The principal the request is attributed to.
        binding_refs: The proofs the request says the move was taken on.
        ledger_statuses: What each ledger says about the records it has
            taken out of the document, filled on first read. One request
            may ask about several siblings of one collection, and reading
            the whole ledger once per sibling would make a guard cost a
            file read per reference.
    """

    document: dict[str, Any]
    document_path: Path
    record: LifecycleRecord
    updates: Mapping[str, Any]
    reason_code: str | None
    actor: str
    binding_refs: tuple[str, ...] = ()
    ledger_statuses: dict[Epoch2Collection, Mapping[str, str]] = field(default_factory=dict)


#: The code a watchlist line leads with, so a client can tell an advisory
#: ceiling from any other note on an accepted answer.
WIP_WATCHLIST_SIGNAL: Final = "wip_active_milestones_advisory"


#: A predicate over the document and the request. ``True`` means the guard
#: holds; a predicate that cannot show its fact answers ``False``, so a
#: document missing the row it needs shuts the edge rather than opening it.
GuardComputer = Callable[[GuardInputs], bool]


def _row_field(row: Any, name: str) -> Any:
    """Return one field of a stored row, or ``None`` when it has none.

    Sibling rows are read raw rather than validated: a predicate about
    one record must not fail because an unrelated record in the same
    collection is malformed, and every caller here compares the result
    against a known value, so an unreadable row reads as not matching.

    Args:
        row: A stored row, or whatever the document held in its place.
        name: The field to read.

    Returns:
        The field's value, or ``None``.
    """
    return row.get(name) if isinstance(row, dict) else None


def _ledger_statuses(inputs: GuardInputs, collection: Epoch2Collection) -> Mapping[str, str]:
    """Return the standing status of every record *collection* has compacted.

    Args:
        inputs: The guard inputs, which carry the memo this fills.
        collection: The ledger collection to read.

    Returns:
        Each compacted record's status, keyed by public key. Superseded
        lines are dropped, because a corrected line is the one that
        stands. A ledger that cannot be read answers empty, so the
        predicate that asked it sees the record as absent.
    """
    cached = inputs.ledger_statuses.get(collection)
    if cached is not None:
        return cached
    statuses: Mapping[str, str]
    try:
        path = ledger_path(inputs.document_path, collection)
        statuses = {
            item.record_key: item.status for item in effective_records(read_ledger_records(path))
        }
    except LedgerError, OSError, ValueError:
        logger.warning(f"_ledger_statuses unreadable collection={collection.value}")
        statuses = {}
    inputs.ledger_statuses[collection] = statuses
    return statuses


def _status_of(inputs: GuardInputs, collection: Epoch2Collection, key: str) -> str | None:
    """Return one record's status, from the document or from its ledger.

    A terminal record leaves the document for its ledger, so a predicate
    reading document rows alone would see a completed Batch as absent and
    shut the very edge its completion opens.

    Args:
        inputs: The guard inputs.
        collection: The collection the record is stored under.
        key: The record's public key.

    Returns:
        The status, or ``None`` when neither tier holds the record or the
        row it holds carries no readable status.
    """
    row = document_rows(inputs.document, collection).get(key)
    if row is None:
        return _ledger_statuses(inputs, collection).get(key)
    status = _row_field(row, "status")
    return status if isinstance(status, str) else None


def _supplied(inputs: GuardInputs, name: str) -> bool:
    """Return whether the request supplies a usable value for *name*.

    Args:
        inputs: The guard inputs.
        name: The update field to look for.

    Returns:
        ``True`` when the field is present and not empty or null.
    """
    return bool(inputs.updates.get(name))


def _no_open_milestones(inputs: GuardInputs) -> bool:
    """Return whether no Milestone under this Track is still open."""
    if not isinstance(inputs.record, Track):
        return False
    track_ref = str(inputs.record.urn)
    rows = document_rows(inputs.document, Epoch2Collection.MILESTONE)
    return not any(
        _row_field(row, "primary_track_ref") == track_ref
        and _row_field(row, "status") not in _CLOSED_MILESTONE_STATUSES
        for row in rows.values()
    )


def _track_active(inputs: GuardInputs) -> bool:
    """Return whether this Milestone's primary Track is still active."""
    if not isinstance(inputs.record, Milestone):
        return False
    rows = document_rows(inputs.document, Epoch2Collection.TRACK)
    row = rows.get(inputs.record.primary_track_ref.entity_key)
    return bool(_row_field(row, "status") == TrackStatus.ACTIVE.value)


def _required_batches_completed(inputs: GuardInputs) -> bool:
    """Return whether every Batch this Milestone requires has closed.

    A closed Batch is terminal and has been compacted into its ledger, so
    this predicate is the one that reads both tiers most often.
    """
    if not isinstance(inputs.record, Milestone):
        return False
    return all(
        _status_of(inputs, Epoch2Collection.BATCH, ref.entity_key) in _CLOSED_BATCH_STATUSES
        for ref in inputs.record.required_batch_refs
    )


def _tasks_ready_to_integrate(inputs: GuardInputs) -> bool:
    """Return whether every Task in this Batch has finished or been cancelled.

    A completed or cancelled Task has left the document for its ledger,
    so the settled statuses are read from whichever tier holds each Task.
    """
    if not isinstance(inputs.record, DeliveryBatch):
        return False
    batch_ref = str(inputs.record.urn)
    # A Task placed before promotion appended to task_refs is still this
    # Batch's work, so placement read off the Task counts as membership.
    placed = {
        key
        for key, row in document_rows(inputs.document, Epoch2Collection.TASK).items()
        if _row_field(row, "batch_ref") == batch_ref
    }
    listed = {ref.entity_key for ref in inputs.record.task_refs}
    return all(
        _status_of(inputs, Epoch2Collection.TASK, key) in _SETTLED_TASK_STATUSES
        for key in sorted(listed | placed)
    )


def _target_branch_pinned(inputs: GuardInputs) -> bool:
    """Return whether the Batch knows which branch it integrates into."""
    if not isinstance(inputs.record, DeliveryBatch):
        return False
    return _supplied(inputs, "target_branch") or inputs.record.target_branch is not None


def _head_binding_pinned(inputs: GuardInputs) -> bool:
    """Return whether the Batch records the exact head it was checked at."""
    if not isinstance(inputs.record, DeliveryBatch):
        return False
    return (
        _supplied(inputs, "current_head_binding") or inputs.record.current_head_binding is not None
    )


def _promotion_contract_complete(inputs: GuardInputs) -> bool:
    """Return whether a promoted Task carries a Batch, criteria and a due scope."""
    if not isinstance(inputs.record, Task):
        return False
    return all(_supplied(inputs, name) for name in ("batch_ref", "criteria", "due_scope"))


def _run_bound(inputs: GuardInputs) -> bool:
    """Return whether the Run the Task would start under is a record that exists."""
    if not isinstance(inputs.record, Task):
        return False
    named = inputs.updates.get("active_run_ref")
    if not isinstance(named, str) or not named:
        return False
    try:
        parsed = parse_qualified_urn(named)
    except IdentityError:
        return False
    if parsed.kind is not EntityKind.RUN:
        return False
    return parsed.entity_key in document_rows(inputs.document, Epoch2Collection.RUN)


def _entity_key(ref: Any) -> str | None:
    """Return the public key a stored reference addresses, or ``None``."""
    if not isinstance(ref, str):
        return None
    try:
        return parse_qualified_urn(ref).entity_key
    except IdentityError:
        return None


def _track_policy(document: dict[str, Any], track_ref: Any) -> TrackPolicy | None:
    """Return the policy of the Track *track_ref* names, or ``None``.

    Args:
        document: The document to read.
        track_ref: A stored Track reference.

    Returns:
        The validated policy, or ``None`` when the reference, the Track
        row or its policy is not readable.
    """
    key = _entity_key(track_ref)
    track = None if key is None else document_rows(document, Epoch2Collection.TRACK).get(key)
    try:
        return TrackPolicy.model_validate(_row_field(track, "policy"))
    except ValidationError:
        return None


def _milestone_track(milestones: Mapping[str, Any], milestone_ref: Any) -> Any:
    """Return the primary Track reference of the Milestone *milestone_ref* names."""
    key = _entity_key(milestone_ref)
    return None if key is None else _row_field(milestones.get(key), "primary_track_ref")


def batch_activation_admitted(document: dict[str, Any], batch: DeliveryBatch) -> bool:
    """Return whether *batch* may activate under its Track's hard WIP ceiling.

    The ceiling counts the Track's Batches already ACTIVE in the same
    repository, because integration is serialised per repository and a
    second active Batch there keeps invalidating the first one's
    exact-head proof. Batches of other Tracks, and of other repositories,
    are not counted: the ceiling is per Track per repository, and there is
    no workspace-wide one.

    Args:
        document: The locked document.
        batch: The Batch asking to activate.

    Returns:
        ``True`` when the ceiling admits one more; ``False`` when it does
        not, or when the Batch's Milestone or Track cannot be read, since
        an unreadable ceiling cannot be shown to admit anything.
    """
    milestones = document_rows(document, Epoch2Collection.MILESTONE)
    track_ref = _milestone_track(milestones, str(batch.milestone_ref))
    policy = _track_policy(document, track_ref)
    if policy is None:
        return False
    active = sum(
        1
        for key, row in document_rows(document, Epoch2Collection.BATCH).items()
        if key != batch.key
        and _row_field(row, "status") == BatchStatus.ACTIVE.value
        and _row_field(row, "repository_ref") == str(batch.repository_ref)
        and _milestone_track(milestones, _row_field(row, "milestone_ref")) == track_ref
    )
    return policy.admits_batch_activation(active_batches_in_repo=active)


def milestone_watchlist(document: dict[str, Any], milestone: Milestone) -> tuple[str, ...]:
    """Return the advisory signals activating *milestone* raises, if any.

    The active-Milestone ceiling never refuses anything: it bounds split
    attention rather than protecting a proof, so exceeding it is reported
    beside an accepted answer instead of shutting the edge.

    Args:
        document: A document holding the Milestone at its new status.
        milestone: The Milestone just activated.

    Returns:
        One watchlist line when the Track now carries more ACTIVE
        Milestones than its policy advises, else nothing.
    """
    track_ref = str(milestone.primary_track_ref)
    policy = _track_policy(document, track_ref)
    if policy is None:
        return ()
    active = sum(
        1
        for row in document_rows(document, Epoch2Collection.MILESTONE).values()
        if _row_field(row, "primary_track_ref") == track_ref
        and _row_field(row, "status") == MilestoneStatus.ACTIVE.value
    )
    if not policy.exceeds_milestone_advisory(active_milestones=active):
        return ()
    return (
        f"{WIP_WATCHLIST_SIGNAL}: track {milestone.primary_track_ref.entity_key} carries "
        f"{active} active milestones, past its advisory {policy.wip.active_milestones}",
    )


def _batch_wip_admits(inputs: GuardInputs) -> bool:
    """Return whether the Batch's Track admits one more active Batch in its repository."""
    if not isinstance(inputs.record, DeliveryBatch):
        return False
    return batch_activation_admitted(inputs.document, inputs.record)


def _never_claimed(inputs: GuardInputs) -> bool:
    """Return whether the Task has never been claimed, which demotion requires."""
    return isinstance(inputs.record, Task) and inputs.record.first_claimed_at is None


def _releaser_holds_lease(inputs: GuardInputs) -> bool:
    """Return whether the releasing principal is the one holding the Task's lease.

    A claim taken before the holder was recorded names nobody, so there is
    no holder for the release to be taken from and any principal may
    release it.
    """
    if not isinstance(inputs.record, Task):
        return False
    holder = inputs.record.claimed_by
    return holder is None or holder == inputs.actor


#: The Run statuses a Task's work may still be in. A terminal Run has
#: left the document for its ledger, so only these are ever found there.
_OPEN_RUN_STATUSES: Final = frozenset(
    {RunStatus.QUEUED.value, RunStatus.RUNNING.value, RunStatus.SUSPENDED.value}
)


def task_run_open(document: dict[str, Any], task: Task) -> bool:
    """Return whether a Run scoped to *task* is queued, running or suspended.

    Args:
        document: The document the release would land in.
        task: The Task being released.

    Returns:
        ``True`` when any open Run in the document works for the Task.
    """
    wanted = str(task.urn)
    return any(
        _row_field(_row_field(row, "scope"), "task_ref") == wanted
        and _row_field(row, "status") in _OPEN_RUN_STATUSES
        for row in document_rows(document, Epoch2Collection.RUN).values()
    )


def _no_active_run(inputs: GuardInputs) -> bool:
    """Return whether no open Run works for the Task the release would free."""
    return isinstance(inputs.record, Task) and not task_run_open(inputs.document, inputs.record)


def _criteria_evidence_bound(inputs: GuardInputs) -> bool:
    """Return whether the request binds evidence for the Task's criteria.

    The predicate is that some evidence is named at all: a Task that
    declares itself ready to integrate on nothing but a Run report is the
    report standing in for the proof.
    """
    return isinstance(inputs.record, Task) and bool(inputs.binding_refs)


def _landed_reconciliation(inputs: GuardInputs) -> MergeReconciliation | None:
    """Return the newest landed read-back filed for this Batch, if any.

    A reconciliation line is filed by ``runtime.delivery.reconcile_merge``
    from what reading the target branch back found, so its presence is the
    host observation and its matched commit is the reconciliation. A ledger
    that cannot be read answers nothing, which shuts both edges.
    """
    if not isinstance(inputs.record, DeliveryBatch):
        return None
    wanted = str(inputs.record.urn)
    try:
        lines = read_ledger_records(ledger_path(inputs.document_path, Epoch2Collection.BATCH))
    except LedgerError, OSError, ValueError:
        logger.warning(f"_landed_reconciliation unreadable batch={inputs.record.key}")
        return None
    landed: MergeReconciliation | None = None
    for item in lines:
        if not item.record_key.startswith(RECONCILIATION_KEY_PREFIX):
            continue
        if item.status != MergeOutcome.LANDED.value or item.payload.get("batch_ref") != wanted:
            continue
        try:
            landed = MergeReconciliation.model_validate(item.payload)
        except ValidationError:
            continue
    return landed


def _host_merge_observed(inputs: GuardInputs) -> bool:
    """Return whether a read-back of the target branch found the merge."""
    return _landed_reconciliation(inputs) is not None


def _reconciliation_matched(inputs: GuardInputs) -> bool:
    """Return whether the landed commit is the exact head the Batch pinned."""
    landed = _landed_reconciliation(inputs)
    if landed is None or not isinstance(inputs.record, DeliveryBatch):
        return False
    binding = inputs.record.current_head_binding
    return binding is not None and landed.matched_head_sha == binding.head_sha


def _reason_recorded(inputs: GuardInputs) -> bool:
    """Return whether the request names why the move happened."""
    return inputs.reason_code is not None


#: Which guards this preflight can answer from the document and the
#: request. A guard absent from this map is left to the reducer, which
#: either requires an observation for it or defaults it to satisfied.
GUARD_COMPUTERS: Final[Mapping[TransitionGuard, GuardComputer]] = {
    TransitionGuard.NO_OPEN_MILESTONES: _no_open_milestones,
    TransitionGuard.TRACK_ACTIVE: _track_active,
    TransitionGuard.REQUIRED_BATCHES_COMPLETED: _required_batches_completed,
    TransitionGuard.TASKS_READY_TO_INTEGRATE: _tasks_ready_to_integrate,
    TransitionGuard.TARGET_BRANCH_PINNED: _target_branch_pinned,
    TransitionGuard.BATCH_WIP_ADMITS: _batch_wip_admits,
    TransitionGuard.NEVER_CLAIMED: _never_claimed,
    TransitionGuard.HEAD_BINDING_PINNED: _head_binding_pinned,
    TransitionGuard.PROMOTION_CONTRACT_COMPLETE: _promotion_contract_complete,
    TransitionGuard.RUN_BOUND: _run_bound,
    TransitionGuard.REASON_RECORDED: _reason_recorded,
    TransitionGuard.CRITERIA_EVIDENCE_BOUND: _criteria_evidence_bound,
    TransitionGuard.HOST_MERGE_OBSERVED: _host_merge_observed,
    TransitionGuard.RECONCILIATION_MATCHED: _reconciliation_matched,
    TransitionGuard.RELEASER_HOLDS_LEASE: _releaser_holds_lease,
    TransitionGuard.NO_ACTIVE_RUN: _no_active_run,
}


__all__ = [
    "GUARD_COMPUTERS",
    "WIP_WATCHLIST_SIGNAL",
    "GuardComputer",
    "GuardInputs",
    "batch_activation_admitted",
    "milestone_watchlist",
    "task_run_open",
]
