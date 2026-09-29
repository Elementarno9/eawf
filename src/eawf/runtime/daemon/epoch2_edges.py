"""Facts a lifecycle edge writes or re-judges beyond what its caller supplies.

The transaction walks every native edge the same way; the few edges that
carry a fact of their own -- a first-claim stamp, a Batch listing to undo,
a ceiling that reads sibling rows -- are decided here, against the locked
document the transaction already holds.
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
from eawf.runtime.daemon.methods.domain_guards import batch_activation_admitted
from eawf.workflow.lifecycle.epoch2 import LifecycleRecord


def locked_unmet(
    document: dict[str, Any], *, record: LifecycleRecord, target: LifecycleStatus
) -> frozenset[TransitionGuard]:
    """Return the guards that fail when re-judged against the locked document.

    The per-verb preflight answers every computable guard from a read it
    releases before the commit. The hard WIP ceiling is judged again here,
    because it reads sibling Batches rather than the record being moved:
    two activations over different Batches would each pass a preflight
    taken before the other committed.

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
    return frozenset()


def edge_updates(
    record: LifecycleRecord,
    *,
    target: LifecycleStatus,
    updates: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Return the field values the edge writes, the request's own included.

    The first claim of a Task carries a fact the caller never supplies: it
    stamps ``first_claimed_at``, which closes demotion for good.

    Args:
        record: The record as the document holds it.
        target: The status the request moves to.
        updates: The field values the request supplies.
        now: When the transition happened.

    Returns:
        The updates handed to the reducer.
    """
    written = dict(updates)
    if (
        isinstance(record, Task)
        and target is TaskStatus.CLAIMED
        and record.first_claimed_at is None
    ):
        written["first_claimed_at"] = now
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
