"""Task, and the draft head that makes the backlog the same entity.

A backlog row is a ``DRAFT`` Task. It holds a key, an intent and a
priority, and it holds no Batch, no criteria and no due scope until it is
promoted. One identifier therefore survives from first idea to completed
delivery, and history never changes identity at a promotion boundary --
which is what a separate backlog record would force, because promoting it
would mean minting a second id and hoping every reader follows the link.

The draft head is what the field rules below encode. ``batch_ref`` and
``criteria`` are absent while the Task is unplaced and required from
``PLANNED`` onward; ``priority`` and ``intent`` are required from the very
first draft, so a backlog row is readable and orderable without opening a
plan. A draft is never dispatchable: every dispatch guard requires
``PLANNED``, so the missing contract cannot be mistaken for an empty one.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Self

from pydantic import model_validator

from eawf.kernel.spec.common import CriterionSpec
from eawf.kernel.state.epoch2.base import NonEmptyStr, StrictPositiveInt
from eawf.kernel.state.epoch2.urns import BatchUrn, DueScopeUrn, RunUrn, TaskUrn
from eawf.kernel.state.epoch2.values import Epoch2Record, ExactRevisionBinding


class TaskPriority(StrEnum):
    """How a Task is ordered against its siblings.

    An ordering aid only. It never overrides a dependency edge and never
    resolves a lease conflict, both of which are correctness constraints
    rather than preferences.
    """

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class TaskStatus(StrEnum):
    """The stored lifecycle states of a Task, draft head included."""

    DRAFT = "DRAFT"
    DEFERRED = "DEFERRED"
    DROPPED = "DROPPED"
    PLANNED = "PLANNED"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    READY_TO_INTEGRATE = "READY_TO_INTEGRATE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


#: The backlog states: the Task is unplaced by definition, so it holds no
#: Batch and no criteria. ``DROPPED`` belongs here because it is reachable
#: only from ``DRAFT`` and ``DEFERRED``, so a dropped row never had the
#: contract a planned one carries.
_DRAFT_HEAD: Final = frozenset({TaskStatus.DRAFT, TaskStatus.DEFERRED, TaskStatus.DROPPED})

#: The states in which a Task may legally be undated. A promoted Task
#: inherits its Batch's Milestone when the field was left null, so from
#: ``PLANNED`` onward the due scope is always a recorded fact.
_UNDATED: Final = frozenset({TaskStatus.DRAFT, TaskStatus.DEFERRED, TaskStatus.DROPPED})

#: The three states a Task never leaves, and therefore the three from
#: which its record moves out of the document and into the ledger. The
#: draft-head states are not here: a ``DROPPED`` Task never entered
#: delivery, so it carries no history a ledger line would preserve.
TERMINAL_TASK_STATUSES: Final[frozenset[TaskStatus]] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED}
)


class Task(Epoch2Record):
    """One unit of work, from backlog idea to integrated delivery.

    The public key is stable across move, replan and retry: moving a Task
    between two PLANNED Batches changes its placement and nothing about
    its identity.
    """

    urn: TaskUrn
    batch_ref: BatchUrn | None = None
    priority: TaskPriority
    due_scope: DueScopeUrn | None = None
    intent: NonEmptyStr
    contract_revision: StrictPositiveInt
    criteria: tuple[CriterionSpec, ...] = ()
    status: TaskStatus
    active_run_ref: RunUrn | None = None
    integrated_binding: ExactRevisionBinding | None = None

    @model_validator(mode="after")
    def _placement_matches_draft_head(self) -> Self:
        """Require a Batch and criteria exactly from the planned states onward.

        Raises:
            ValueError: A backlog Task carries a Batch or criteria, or a
                planned-or-later Task carries neither.
        """
        unplaced = self.status in _DRAFT_HEAD
        if unplaced:
            if self.batch_ref is not None:
                raise ValueError(f"a {self.status.value} Task is unplaced and takes no batch_ref")
            if self.criteria:
                raise ValueError(
                    f"a {self.status.value} Task carries no criteria until it is promoted"
                )
            return self
        if self.batch_ref is None:
            raise ValueError(f"status {self.status.value} requires batch_ref")
        if not self.criteria:
            raise ValueError(f"status {self.status.value} requires at least one criterion")
        return self

    @model_validator(mode="after")
    def _due_scope_matches_status(self) -> Self:
        """Require a due scope from the moment the Task is planned.

        Raises:
            ValueError: A planned-or-later Task is undated.
        """
        if self.due_scope is None and self.status not in _UNDATED:
            raise ValueError(
                f"due_scope is optional only in the backlog states; {self.status.value} "
                "requires one"
            )
        return self

    @model_validator(mode="after")
    def _integration_proof_matches_status(self) -> Self:
        """Require the integrated binding exactly where completion implies it.

        Raises:
            ValueError: A completed Task carries no integrated binding, or
                an unfinished Task carries one.
        """
        completed = self.status is TaskStatus.COMPLETED
        if completed and self.integrated_binding is None:
            raise ValueError("a COMPLETED Task requires integrated_binding")
        if not completed and self.integrated_binding is not None:
            raise ValueError(
                f"integrated_binding belongs to a COMPLETED Task, not {self.status.value}"
            )
        return self


__all__ = [
    "TERMINAL_TASK_STATUSES",
    "Task",
    "TaskPriority",
    "TaskStatus",
]
