"""DeliveryBatch: one repository's integration unit.

A Batch is the only thing that merges, and it merges into exactly one
repository. That is why the repository reference is single and required
rather than a list: two repositories behind one Batch would make the
exact-head binding ambiguous about which head it binds, and the merge
authorisation ambiguous about what it authorises.

Three fields are required only from the state that makes them facts. A
target branch is required before activation, an exact head binding from
the moment the Batch claims to be mergeable, and a failure reason only
when the Batch actually failed. Requiring any of them earlier would force
a caller to invent a value, and inventing a head binding is how a stale
proof becomes a fresh-looking one.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Self

from pydantic import model_validator

from eawf.kernel.state.epoch2.base import BatchKey, BranchName
from eawf.kernel.state.epoch2.milestone import reject_duplicate_refs
from eawf.kernel.state.epoch2.urns import BatchUrn, MilestoneUrn, RepositoryUrn, TaskUrn
from eawf.kernel.state.epoch2.values import (
    Epoch2Record,
    ExactRevisionBinding,
    TransitionReason,
)


class BatchStatus(StrEnum):
    """The stored lifecycle states of a DeliveryBatch.

    ``MERGED_PENDING_RECONCILIATION`` exists because an observed merge and
    a reconciled one are different facts: the host may have integrated
    while the authorisation and the integration record have not yet been
    matched against each other.
    """

    PLANNED = "PLANNED"
    ACTIVE = "ACTIVE"
    READY_TO_MERGE = "READY_TO_MERGE"
    MERGING = "MERGING"
    MERGED_PENDING_RECONCILIATION = "MERGED_PENDING_RECONCILIATION"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


#: The states from which a Batch claims to be mergeable or beyond, so the
#: exact head it was checked at is a recorded fact.
_HEAD_BOUND: Final = frozenset(
    {
        BatchStatus.READY_TO_MERGE,
        BatchStatus.MERGING,
        BatchStatus.MERGED_PENDING_RECONCILIATION,
        BatchStatus.COMPLETED,
    }
)

#: The one state in which a target branch has not been chosen yet.
_UNTARGETED: Final = frozenset({BatchStatus.PLANNED})


class DeliveryBatch(Epoch2Record):
    """One Milestone's integration unit inside one repository."""

    key: BatchKey
    urn: BatchUrn
    milestone_ref: MilestoneUrn
    repository_ref: RepositoryUrn
    task_refs: tuple[TaskUrn, ...] = ()
    target_branch: BranchName | None = None
    status: BatchStatus
    current_head_binding: ExactRevisionBinding | None = None
    failure: TransitionReason | None = None

    @model_validator(mode="after")
    def _task_refs_are_unique(self) -> Self:
        """Require each Task to appear at most once in the Batch.

        Raises:
            ValueError: A Task is listed twice, which would double-count
                it in every progress figure the Batch feeds.
        """
        reject_duplicate_refs(self.task_refs, field="task_refs")
        return self

    @model_validator(mode="after")
    def _state_dependent_fields_are_present(self) -> Self:
        """Require the branch, head binding and failure reason where the status implies them.

        Raises:
            ValueError: An activated Batch has no target branch, a
                mergeable-or-later Batch has no head binding, or the
                failure reason and the FAILED status disagree.
        """
        if self.status not in _UNTARGETED and self.target_branch is None:
            raise ValueError(f"status {self.status.value} requires target_branch")
        if self.status in _HEAD_BOUND and self.current_head_binding is None:
            raise ValueError(f"status {self.status.value} requires current_head_binding")
        failed = self.status is BatchStatus.FAILED
        if failed and self.failure is None:
            raise ValueError("a FAILED Batch requires a failure reason")
        if not failed and self.failure is not None:
            raise ValueError(f"a failure reason belongs to a FAILED Batch, not {self.status.value}")
        return self


__all__ = [
    "BatchStatus",
    "DeliveryBatch",
]
