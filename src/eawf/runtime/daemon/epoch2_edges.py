"""Facts a lifecycle edge writes or re-judges beyond what its caller supplies.

The transaction walks every native edge the same way; the few edges that
carry a fact of their own -- a claim stamp, a released lease's contract
revision, a Batch listing to undo, a predicate that reads sibling rows --
are decided here, against the locked document the transaction already
holds.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from eawf.kernel.state.epoch2.batch import BatchStatus, DeliveryBatch
from eawf.kernel.state.epoch2.task import Task, TaskStatus
from eawf.kernel.state.epoch2.transitions import LifecycleStatus, TransitionGuard
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.methods.domain_guards import batch_activation_admitted, task_run_open
from eawf.workflow.lifecycle.epoch2 import LifecycleRecord


def locked_unmet(
    document: dict[str, Any], *, record: LifecycleRecord, target: LifecycleStatus
) -> frozenset[TransitionGuard]:
    """Return the guards that fail when re-judged against the locked document.

    The per-verb preflight answers every computable guard from a read it
    releases before the commit. The guards that read sibling rows rather
    than the record being moved are judged again here: two activations
    over different Batches would each pass a preflight taken before the
    other committed, and a Run queued against a Task after the preflight
    of its release would otherwise lose its Task underneath it.

    Args:
        document: The locked document.
        record: The record as the document holds it.
        target: The status the request moves to.

    Returns:
        The unmet guards, which the reducer reports as a denial.
    """
    if (
        isinstance(record, DeliveryBatch)
        and target is BatchStatus.ACTIVE
        and not batch_activation_admitted(document, record)
    ):
        return frozenset({TransitionGuard.BATCH_WIP_ADMITS})
    if (
        isinstance(record, Task)
        and record.status is TaskStatus.CLAIMED
        and target is TaskStatus.PLANNED
        and task_run_open(document, record)
    ):
        return frozenset({TransitionGuard.NO_ACTIVE_RUN})
    return frozenset()


def edge_updates(
    record: LifecycleRecord,
    *,
    target: LifecycleStatus,
    updates: Mapping[str, Any],
    actor: str,
    now: datetime,
) -> dict[str, Any]:
    """Return the field values the edge writes, the request's own included.

    Two Task edges carry facts the caller never supplies. A claim records
    its actor as the lease holder, and the first claim stamps
    ``first_claimed_at``, which closes demotion for good. A released lease
    increments the contract revision, because the Task is planned again
    under a contract a new claimant has not yet taken.

    Args:
        record: The record as the document holds it.
        target: The status the request moves to.
        updates: The field values the request supplies.
        actor: The principal the request is attributed to.
        now: When the transition happened.

    Returns:
        The updates handed to the reducer.
    """
    written = dict(updates)
    if not isinstance(record, Task):
        return written
    if target is TaskStatus.CLAIMED:
        written["claimed_by"] = actor
        if record.first_claimed_at is None:
            written["first_claimed_at"] = now
    elif record.status is TaskStatus.CLAIMED and target is TaskStatus.PLANNED:
        written["contract_revision"] = record.contract_revision + 1
    return written


def batch_unlisting(
    document: dict[str, Any], *, record: Task, now: datetime
) -> tuple[str, dict[str, Any]] | None:
    """Return the Batch row a demotion takes its Task out of, or ``None``.

    The Batch decides readiness from the Tasks it lists, so a demoted Task
    left on the list would hold the Batch open for work that is back in
    the backlog. The row is rewritten under the document lock the session
    already holds, which serialises it against every other writer.

    Args:
        document: The locked document.
        record: The Task being demoted.
        now: When the demotion happened.

    Returns:
        The Batch key and its successor row, or ``None`` when the document
        holds no native Batch listing the Task.
    """
    if record.batch_ref is None:
        return None
    row = document_rows(document, Epoch2Collection.BATCH).get(record.batch_ref.entity_key)
    try:
        batch = None if row is None else DeliveryBatch.model_validate(row)
    except ValueError:
        batch = None
    task_ref = str(record.urn)
    if batch is None or all(str(ref) != task_ref for ref in batch.task_refs):
        return None
    successor = batch.model_dump(mode="json")
    successor["task_refs"] = [ref for ref in successor["task_refs"] if ref != task_ref]
    successor["revision"] = batch.revision + 1
    successor["updated_at"] = now
    return batch.key, DeliveryBatch.model_validate(successor).model_dump(mode="json")


__all__ = [
    "batch_unlisting",
    "edge_updates",
    "locked_unmet",
]
