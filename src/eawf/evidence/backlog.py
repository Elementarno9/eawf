"""Backlog-area mutators: add / close.

* ``add`` registers a new backlog item with a priority + scope.
* ``close`` closes it with a resolution + commit and *requires* ``--audit``
  of a complete audit per the audit-evidence guard.

Mutators take a typed :class:`State` and mutate it in place; the CLI handler
runs them inside :func:`eawf.cli._mutation.state_transaction`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from eawf.cli.errors import UserError
from eawf.evidence import _io
from eawf.evidence.guards import require_complete_audit
from eawf.state.enums import BacklogPriority, BacklogStatus
from eawf.state.models import BacklogItem, State
from eawf.store.envelope import Envelope

logger = logging.getLogger(__name__)


def add_backlog(
    state: State,
    *,
    item_id: str,
    title: str,
    priority: BacklogPriority,
    scope_id: str,
) -> Envelope:
    """Register a new backlog item in place."""
    backlog: dict[str, BacklogItem] = dict(state.backlog or {})
    if item_id in backlog:
        raise UserError(f"backlog item {item_id!r} already exists", kind="InvalidInput")

    now = datetime.now(UTC)
    item = BacklogItem(
        id=item_id,
        scope_id=scope_id,
        title=title,
        priority=priority,
        status=BacklogStatus.OPEN,
        created_at=now,
        closed_at=None,
        resolution=None,
        commit=None,
    )
    backlog[item_id] = item
    state.backlog = backlog
    state.updated_at = now

    return _io.event_envelope(
        event_id=f"EVT-backlog-add-{item_id}-{int(now.timestamp() * 1000)}",
        scope_id=scope_id,
        event_type="backlog.add",
        actor="cli",
        command="backlog add",
        args={
            "item_id": item_id,
            "title": title,
            "priority": priority.value,
        },
        summary=f"backlog {item_id} added priority={priority.value}",
    )


def set_priority(
    state: State,
    *,
    item_id: str,
    priority: BacklogPriority,
) -> Envelope:
    """Update the priority of an open backlog item in place.

    Raises:
        NotFound: when ``item_id`` is absent from :attr:`State.backlog`.
        InvalidInput: when the item is :attr:`BacklogStatus.CLOSED` (closed
            items are frozen) or when the requested ``priority`` already
            equals the current value (no-op rejected so the event log does
            not silently churn).
    """
    backlog: dict[str, BacklogItem] = dict(state.backlog or {})
    if item_id not in backlog:
        raise UserError(f"backlog item {item_id!r} not found", kind="NotFound")

    prior = backlog[item_id]
    if prior.status == BacklogStatus.CLOSED:
        raise UserError(
            f"backlog item {item_id!r} is closed; cannot change priority", kind="InvalidInput"
        )
    if prior.priority == priority:
        raise UserError(
            f"backlog item {item_id!r} already has priority={priority.value!r}", kind="InvalidInput"
        )

    now = datetime.now(UTC)
    updated = prior.model_copy(update={"priority": priority})
    backlog[item_id] = updated
    state.backlog = backlog
    state.updated_at = now

    return _io.event_envelope(
        event_id=f"EVT-backlog-set-priority-{item_id}-{int(now.timestamp() * 1000)}",
        scope_id=updated.scope_id,
        event_type="backlog.set_priority",
        actor="cli",
        command="backlog set-priority",
        args={
            "item_id": item_id,
            "priority": priority.value,
        },
        summary=f"backlog {item_id} priority={priority.value}",
    )


def close_backlog(
    state: State,
    *,
    item_id: str,
    resolution: str,
    commit: str,
    audit_id: str,
) -> Envelope:
    """Close a backlog item in place; requires complete audit."""
    backlog: dict[str, BacklogItem] = dict(state.backlog or {})
    if item_id not in backlog:
        raise UserError(f"backlog item {item_id!r} not found", kind="NotFound")
    if backlog[item_id].status == BacklogStatus.CLOSED:
        raise UserError(f"backlog item {item_id!r} already closed", kind="InvalidInput")

    require_complete_audit(state, audit_id)

    now = datetime.now(UTC)
    prior = backlog[item_id]
    updated = prior.model_copy(
        update={
            "status": BacklogStatus.CLOSED,
            "resolution": resolution,
            "commit": commit,
            "closed_at": now,
        }
    )
    backlog[item_id] = updated
    state.backlog = backlog
    state.updated_at = now

    return _io.event_envelope(
        event_id=f"EVT-backlog-close-{item_id}-{int(now.timestamp() * 1000)}",
        scope_id=updated.scope_id,
        event_type="backlog.close",
        actor="cli",
        command="backlog close",
        args={
            "item_id": item_id,
            "resolution": resolution,
            "commit": commit,
            "audit_id": audit_id,
        },
        summary=f"backlog {item_id} closed",
    )
