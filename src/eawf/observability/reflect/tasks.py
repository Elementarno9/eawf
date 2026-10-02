"""Read a tree's open Tasks, without a daemon and without a lock.

After the cutover the epoch-1 ``state.json`` is frozen: it still loads, but
no native write ever reaches it, so a reader that wants the work in flight
reads the selected generation's document instead. The document is read
with its machine-local status projection laid over, because a claimed or
running Task records that status only there.

A Task row is stored in one of two shapes -- a native record, or the
wrapper the cutover imported an epoch-1 backlog row or wave into -- so the
rows are read through the console's backlog projection, which renders both.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.projection.compute import ProjectionRow, build_route_projection
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.epoch2.task import TERMINAL_TASK_STATUSES, TaskStatus
from eawf.kernel.store.compaction import read_document
from eawf.observability.reflect.runs import CANONICAL_SEQUENCE_KEY

logger = logging.getLogger(__name__)

#: The console route whose read model lists every Task the document holds.
TASK_ROUTE: Final = "backlog"

#: The statuses a Task is no longer open in: the terminal three, and a draft
#: that was dropped before it entered delivery.
CLOSED_TASK_STATUSES: Final = frozenset(
    status.value for status in (*TERMINAL_TASK_STATUSES, TaskStatus.DROPPED)
)


def read_open_tasks(tree_root: Path) -> tuple[ProjectionRow, ...]:
    """Return every Task the tree's selected generation holds open, in key order.

    Args:
        tree_root: The tree's ``.ea`` directory.

    Returns:
        One backlog row per open Task of the document. A tree still at
        epoch 1 holds no native Task, so it reads as empty.

    Raises:
        ValueError: The document is not a JSON object, or it holds a Task
            row the projection cannot render.
        OSError: The document could not be read.
    """
    authority = resolve_authority(tree_root)
    if authority.target is None or authority.generation_id is None:
        logger.debug(f"read_open_tasks root={tree_root.name} epoch={authority.epoch} tasks=0")
        return ()
    document_path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    document = read_document(document_path)
    projection = build_route_projection(
        route=TASK_ROUTE,
        document=document,
        cursor=int(document.get(CANONICAL_SEQUENCE_KEY, 0)),
        scope_id=authority.generation_id,
        generated_at=datetime.now(UTC),
    )
    tasks = tuple(row for row in projection.rows if row.status.value not in CLOSED_TASK_STATUSES)
    logger.debug(f"read_open_tasks root={tree_root.name} tasks={len(tasks)}")
    return tasks


__all__ = ["CLOSED_TASK_STATUSES", "TASK_ROUTE", "read_open_tasks"]
