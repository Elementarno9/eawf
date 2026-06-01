"""Backlog-area mutators: add / close.

* ``add`` registers a new backlog item with a priority + scope.
* ``close`` closes it with a resolution + commit and *requires* ``--audit``
  of a complete audit per the audit-evidence guard.

Mutators take a typed :class:`State` and mutate it in place; the CLI handler
runs them inside :func:`eawf.surfaces.cli._mutation.state_transaction`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.state.enums import BacklogPriority, BacklogStatus
from eawf.kernel.state.models import BacklogItem, State
from eawf.kernel.store.envelope import Envelope
from eawf.surfaces.cli.errors import UserError
from eawf.surfaces.render.agents_md import lint_entity_title, normalize_entity_title
from eawf.workflow.evidence import _io
from eawf.workflow.evidence.guards import require_complete_audit

logger = logging.getLogger(__name__)


class BacklogTitleRow(BaseModel):
    """Per-item row in a backlog title-backfill / sweep report.

    Captures one :class:`BacklogItem`'s title before and after the
    entity-title normalization, the style-lint violations the *current*
    title trips (the read-only "sweep" signal), and whether the normalized
    title would change the stored value.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: str
    before: str
    after: str
    changed: bool
    violations: list[str]


class BacklogTitleReport(BaseModel):
    """Aggregate report for a backlog title sweep / backfill run.

    ``rows`` carries every backlog item in id-sorted order; ``applied`` is
    ``True`` only when the run mutated state. The summary counters let the CLI
    render a one-line headline without re-walking ``rows``.
    """

    model_config = ConfigDict(extra="forbid")

    applied: bool
    total: int
    changed: int
    violations: int
    rows: list[BacklogTitleRow]


def add_backlog(
    state: State,
    *,
    item_id: str,
    title: str,
    priority: BacklogPriority,
    scope_id: str,
    description: str | None = None,
) -> Envelope:
    """Register a new backlog item in place.

    Args:
        description: Optional long-form purpose for the item. Bounded at
            500 characters by :class:`BacklogItem`; ``None`` leaves the
            item title-only.

    Raises:
        UserError: when ``item_id`` already exists (``kind="InvalidInput"``),
            or when ``title`` / ``description`` violate the
            :class:`BacklogItem` bounds (``kind="InvalidInput"``).
    """
    backlog: dict[str, BacklogItem] = dict(state.backlog or {})
    if item_id in backlog:
        raise UserError(f"backlog item {item_id!r} already exists", kind="InvalidInput")

    now = datetime.now(UTC)
    try:
        item = BacklogItem(
            id=item_id,
            scope_id=scope_id,
            title=title,
            description=description,
            priority=priority,
            status=BacklogStatus.OPEN,
            created_at=now,
            closed_at=None,
            resolution=None,
            commit=None,
        )
    except ValidationError as exc:
        raise UserError(
            f"invalid backlog item {item_id!r}: {exc.errors()[0]['msg']}", kind="InvalidInput"
        ) from exc
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
            "has_description": description is not None,
        },
        summary=f"backlog {item_id} added priority={priority.value}",
    )


def edit_backlog(
    state: State,
    *,
    item_id: str,
    title: str | None = None,
    description: str | None = None,
    intent: IntentBrief | None = None,
    clear_intent: bool = False,
) -> Envelope:
    """Edit an open backlog item's title, description, or intent in place.

    At least one editable field must be given. The new values are
    re-validated through :class:`BacklogItem`, so title / description /
    intent bounds stay owned by the model.

    Raises:
        UserError: when ``item_id`` is absent (``kind="NotFound"``); when
            neither field is supplied, when the item is
            :attr:`BacklogStatus.CLOSED` (closed items are frozen), or when
            a new value violates the :class:`BacklogItem` bounds
            (``kind="InvalidInput"``).
    """
    if intent is not None and clear_intent:
        raise UserError("cannot pass intent and clear_intent together", kind="InvalidInput")
    intent_supplied = intent is not None or clear_intent
    if title is None and description is None and not intent_supplied:
        raise UserError(
            "no fields to edit: pass --title, --description, --intent-* or --clear-intent",
            kind="InvalidInput",
        )

    backlog: dict[str, BacklogItem] = dict(state.backlog or {})
    if item_id not in backlog:
        raise UserError(f"backlog item {item_id!r} not found", kind="NotFound")

    prior = backlog[item_id]
    if prior.status == BacklogStatus.CLOSED:
        raise UserError(f"backlog item {item_id!r} is closed; cannot edit", kind="InvalidInput")

    changes: dict[str, object] = {}
    if title is not None:
        changes["title"] = title
    if description is not None:
        changes["description"] = description
    if clear_intent:
        changes["intent"] = None
    elif intent is not None:
        changes["intent"] = intent

    now = datetime.now(UTC)
    try:
        updated = BacklogItem.model_validate({**prior.model_dump(), **changes})
    except ValidationError as exc:
        raise UserError(
            f"invalid backlog edit for {item_id!r}: {exc.errors()[0]['msg']}",
            kind="InvalidInput",
        ) from exc
    backlog[item_id] = updated
    state.backlog = backlog
    state.updated_at = now

    return _io.event_envelope(
        event_id=f"EVT-backlog-edit-{item_id}-{int(now.timestamp() * 1000)}",
        scope_id=updated.scope_id,
        event_type="backlog.edit",
        actor="cli",
        command="backlog edit",
        args={
            "item_id": item_id,
            "fields": sorted(changes),
        },
        summary=f"backlog {item_id} edited fields={','.join(sorted(changes))}",
    )


def backfill_titles(
    state: State,
    *,
    apply: bool = False,
) -> tuple[BacklogTitleReport, Envelope | None]:
    """Sweep every backlog item's title and (optionally) normalize it.

    Walks :attr:`State.backlog` in id-sorted order, normalizing each title to
    the entity-title rule via
    :func:`eawf.surfaces.render.agents_md.normalize_entity_title` (strip a
    trailing period, trim over-cap titles to a word boundary, and derive a
    candidate from the item ``description`` when the title is an empty
    placeholder). The current title's style violations are recorded via
    :func:`eawf.surfaces.render.agents_md.lint_entity_title` so a pure
    ``apply=False`` call doubles as the read-only sweep.

    When ``apply`` is ``True``, each changed item is re-validated through
    :class:`BacklogItem` (the same boundary :func:`edit_backlog` uses, so the
    model stays the single owner of the title bound) and written back in place;
    a single ``backlog.backfill_titles`` event summarising the changed-item
    count is returned. A run that changes nothing (or any ``apply=False`` run)
    returns ``None`` for the envelope so the caller appends no event.

    Closed items are swept for reporting but never mutated: a frozen
    :class:`BacklogStatus.CLOSED` item keeps its historical title even under
    ``apply``. An item whose normalization collapses to an empty string (a
    placeholder title with no usable description) is left unchanged and still
    reported, because the model rejects an empty ``title`` at ingestion.

    Args:
        state: The loaded :class:`State` to sweep / mutate in place.
        apply: When ``True`` persist the normalized titles; when ``False``
            (the default) report only and leave ``state`` untouched.

    Returns:
        A ``(report, envelope)`` pair. ``report`` always describes every item;
        ``envelope`` is the single ``backlog.backfill_titles`` event when
        ``apply`` mutated at least one item, else ``None``.
    """
    backlog: dict[str, BacklogItem] = dict(state.backlog or {})
    rows: list[BacklogTitleRow] = []
    changed_ids: list[str] = []

    for item_id in sorted(backlog):
        item = backlog[item_id]
        before = item.title
        normalized = normalize_entity_title(before, item.description)
        mutable = item.status != BacklogStatus.CLOSED
        # An empty normalization (placeholder title, no usable description)
        # cannot be persisted: the model forbids an empty title. Treat it as a
        # no-op so the sweep still flags the item without proposing an invalid
        # write.
        would_change = mutable and normalized != "" and normalized != before
        rows.append(
            BacklogTitleRow(
                item_id=item_id,
                before=before,
                after=normalized if would_change else before,
                changed=would_change,
                violations=lint_entity_title(before),
            )
        )
        if apply and would_change:
            updated = item.model_copy(update={"title": normalized})
            backlog[item_id] = updated
            changed_ids.append(item_id)

    report = BacklogTitleReport(
        applied=apply and bool(changed_ids),
        total=len(rows),
        changed=sum(1 for row in rows if row.changed),
        violations=sum(len(row.violations) for row in rows),
        rows=rows,
    )

    if not (apply and changed_ids):
        logger.info(
            f"backfill_titles apply={apply} total={report.total} "
            f"changed={report.changed} violations={report.violations}"
        )
        return report, None

    now = datetime.now(UTC)
    state.backlog = backlog
    state.updated_at = now
    scope_id = backlog[changed_ids[0]].scope_id
    logger.info(
        f"backfill_titles apply=True total={report.total} "
        f"changed={len(changed_ids)} violations={report.violations}"
    )
    event = _io.event_envelope(
        event_id=f"EVT-backlog-backfill-titles-{int(now.timestamp() * 1000)}",
        scope_id=scope_id,
        event_type="backlog.backfill_titles",
        actor="cli",
        command="backlog backfill-titles",
        args={
            "item_ids": changed_ids,
            "changed": len(changed_ids),
        },
        summary=f"backlog backfill-titles changed={len(changed_ids)} items",
    )
    return report, event


def set_priority(
    state: State,
    *,
    item_id: str,
    priority: BacklogPriority,
) -> Envelope:
    """Update the priority of an open backlog item in place.

    Raises:
        UserError: when ``item_id`` is absent from :attr:`State.backlog`
            (``kind="NotFound"``); or when the item is
            :attr:`BacklogStatus.CLOSED` (closed items are frozen) or the
            requested ``priority`` already equals the current value (no-op
            rejected so the event log does not silently churn)
            (``kind="InvalidInput"``).
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
