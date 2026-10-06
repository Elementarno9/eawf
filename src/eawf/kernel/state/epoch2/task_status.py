"""The stored lifecycle states of a Task.

Kept apart from the Task model so a reader that only compares statuses --
the store's document compaction, which every statusline redraw runs --
does not build the Task model and the criterion specs it carries.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


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


#: The three states a Task never leaves, and therefore the three from
#: which its record moves out of the document and into the ledger. The
#: draft-head states are not here: a ``DROPPED`` Task never entered
#: delivery, so it carries no history a ledger line would preserve.
TERMINAL_TASK_STATUSES: Final[frozenset[TaskStatus]] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED}
)

__all__ = ["TERMINAL_TASK_STATUSES", "TaskStatus"]
