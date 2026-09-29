"""Filing the record a released Task lease leaves behind.

``domain.task.release`` moves a claimed Task back to ``PLANNED`` through
the one native transaction every lifecycle verb commits through. The
:class:`~eawf.kernel.state.epoch2.task.TaskReleaseRecord` saying why is a
line of the Task ledger, filed right after that commit under a key
derived from the Task and the revision the release landed on. A retry of
the release replays the transaction's receipt and files the line only if
it is missing, so a daemon that died between the two writes finishes the
record on the retry rather than losing it.
"""

from __future__ import annotations

import logging
from typing import Final

from eawf.kernel.state.epoch2.task import TaskReleaseRecord, TaskStatus
from eawf.kernel.state.epoch2.urns import TaskUrn
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import MutationReceipt, commit_ledger_append

logger = logging.getLogger(__name__)


#: The one verb that releases a Task's lease.
TASK_RELEASE_METHOD: Final = "domain.task.release"

#: The prefix of every release line's key in the Task ledger.
RELEASE_KEY_PREFIX: Final = "TRL-"


def release_record_key(task_ref: TaskUrn, new_revision: int) -> str:
    """Return the ledger key the release landing on *new_revision* is filed under.

    Args:
        task_ref: The released Task.
        new_revision: The revision the release moved the Task to.

    Returns:
        A ``TRL-``-prefixed key, one per release of one Task.
    """
    return f"{RELEASE_KEY_PREFIX}{task_ref.entity_key}-r{new_revision}"


def file_task_release(
    context: Epoch2RootContext,
    *,
    task_ref: TaskUrn,
    cause: str,
    actor: str,
    receipt: MutationReceipt,
) -> Envelope | None:
    """File the release record of one committed release, once.

    Args:
        context: The native context of the addressed root.
        task_ref: The released Task.
        cause: The reason code the release committed with.
        actor: The principal that released the lease.
        receipt: The receipt of the committed (or replayed) release.

    Returns:
        The firehose row the append wrote, or ``None`` when the line was
        already filed.

    Raises:
        ValidationError: The document no longer holds the Task as a
            planned row carrying its Batch, so the record's target Batch
            cannot be read.
    """
    key = release_record_key(task_ref, receipt.revision_after)
    with context.session([task_ref]) as session:
        lines = read_ledger_records(session.ledger_path(Epoch2Collection.TASK))
        if any(item.record_key == key for item in lines):
            return None
        rows = document_rows(session.read_document(), Epoch2Collection.TASK)
        row = rows.get(task_ref.entity_key, {})
        record = TaskReleaseRecord.model_validate(
            {
                "task_ref": str(task_ref),
                "cause": cause,
                "actor": actor,
                "prior_revision": receipt.revision_before,
                "new_revision": receipt.revision_after,
                "target_batch_ref": row.get("batch_ref"),
                "recorded_at": receipt.occurred_at,
            }
        )
        envelope = commit_ledger_append(
            session,
            LedgerRecord(
                collection=Epoch2Collection.TASK,
                record_key=key,
                status=TaskStatus.PLANNED.value,
                recorded_at=receipt.occurred_at,
                payload=record.model_dump(mode="json"),
            ),
        )
    logger.info(f"file_task_release task={task_ref.entity_key} key={key!r}")
    return envelope


__all__ = [
    "RELEASE_KEY_PREFIX",
    "TASK_RELEASE_METHOD",
    "file_task_release",
    "release_record_key",
]
