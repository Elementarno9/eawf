"""Build the successor document a plan apply writes.

Applying a plan is two concerns: the transaction around the write --
locks, compare-and-swap, drift, the journal -- and the content of the
write itself. This module owns the second: deciding which rows the plan
creates, promotes, rewrites or cancels, and building the one document
that carries all of them. It reads the document and returns a new one,
taking no lock and writing nothing, so the transaction module can decide
everything before its single atomic replace.

That includes the re-plan. A child of an APPLIED revision on the same
Milestone rewrites the PLANNED Milestone its parent created rather than
creating one, since a plan is the only path that writes a Milestone
contract and a planned but unstarted Milestone must stay revisable. Only
work nobody has started is rewritten -- the Milestone, and the parent's
Batches and Tasks, must still be PLANNED -- so a re-plan never pulls a
claimed Task or an active Batch out from under its holder. What the
parent placed and the child drops is cancelled rather than deleted, so
the history of the dropped work survives the re-plan.
"""

from __future__ import annotations

import copy
import logging
import uuid
from typing import Any, Final

from eawf.kernel.state.epoch2.batch import BatchStatus, DeliveryBatch
from eawf.kernel.state.epoch2.lifecycle import TransitionDenied, apply_transition
from eawf.kernel.state.epoch2.milestone import Milestone, MilestoneStatus
from eawf.kernel.state.epoch2.plan_revision import (
    PlanBody,
    PlannedTask,
    PlanRevision,
    PlanRevisionStatus,
)
from eawf.kernel.state.epoch2.task import Task, TaskStatus
from eawf.kernel.state.epoch2.values import EntityOrigin, Epoch2Record
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.workflow.planning.revision import PlanRefusal, PlanRefusalCode

logger = logging.getLogger(__name__)

#: The collection plan revisions are stored under, at the document tier.
PLAN_REVISION_COLLECTION: Final = Epoch2Collection.PLAN_REVISION

#: The origin every natively planned record carries.
_NATIVE_ORIGIN: Final = EntityOrigin(kind="native", mapping_basis="native", confidence="exact")


def _row_field(row: Any, name: str) -> Any:
    """Return one field of a stored row, or ``None`` when it has none.

    Sibling rows are read raw rather than validated: a drift predicate
    about a Track must not fail because an unrelated repository row is
    malformed, and every caller compares the result against a known value,
    so an unreadable row reads as not matching.

    Args:
        row: A stored row, or whatever the document held in its place.
        name: The field to read.

    Returns:
        The field's value, or ``None``.
    """
    return row.get(name) if isinstance(row, dict) else None


def _str_field(row: Any, name: str) -> str | None:
    """Return one string field of a stored row, or ``None``."""
    value = _row_field(row, name)
    return value if isinstance(value, str) else None


def _promotable_draft(document: dict[str, Any], *, key: str) -> Task | None:
    """Return the native backlog Task the plan may promote under *key*, if any.

    A plan names an existing Task to promote it: the backlog row keeps its
    identifier, and the apply takes it through the ordinary promotion edge
    rather than minting a second record. Only a native ``DRAFT`` qualifies.
    A row imported from the previous epoch is immutable to this lifecycle,
    and a row past the backlog is already placed, so either stays a claimed
    key the apply refuses.

    Args:
        document: The locked document.
        key: The Task key the plan names.

    Returns:
        The validated draft, or ``None``.
    """
    row = document_rows(document, Epoch2Collection.TASK).get(key)
    try:
        task = None if row is None else Task.model_validate(row)
    except ValueError:
        return None
    if task is None or task.status is not TaskStatus.DRAFT or task.origin.kind == "legacy":
        return None
    return task


def _claimed_keys(
    document: dict[str, Any], *, body: PlanBody, parent: PlanBody | None
) -> tuple[str, ...]:
    """Return the locators the document already holds that the plan creates.

    A backlog draft the plan promotes is not claimed: the plan places it
    rather than creating it. Neither is a row a re-plan may rewrite (see
    :func:`_replannable`); any other row a re-plan names, including one
    its parent owned that has since moved past PLANNED, stays claimed.

    Args:
        document: The locked document.
        body: The plan content.
        parent: The applied body *body* re-plans, or ``None``.

    Returns:
        The ``<collection>/<key>`` locators of the rows the plan would
        overwrite without owning.
    """
    wanted: dict[Epoch2Collection, tuple[str, ...]] = {
        Epoch2Collection.MILESTONE: (body.milestone_urn.entity_key,),
        Epoch2Collection.BATCH: tuple(batch.urn.entity_key for batch in body.batches),
        Epoch2Collection.TASK: tuple(
            task.urn.entity_key
            for task in body.tasks
            if _promotable_draft(document, key=task.urn.entity_key) is None
        ),
    }
    replannable = _replannable(document, parent=parent)
    return tuple(
        locator
        for collection, keys in wanted.items()
        for key in keys
        if key in document_rows(document, collection)
        and (locator := _locator(collection, key)) not in replannable
    )


def _locator(collection: Epoch2Collection, key: str) -> str:
    """Return the ``<collection>/<key>`` locator one stored row is named by."""
    return f"{collection.value}/{key}"


def _replannable(document: dict[str, Any], *, parent: PlanBody | None) -> frozenset[str]:
    """Return the locators of the rows a re-plan of *parent* may rewrite or cancel.

    The Milestone is always among them; whether it is still PLANNED is
    refused separately by :func:`_replan_parent`, which names the cause.
    A Batch or Task qualifies only while it is PLANNED, because a row
    past PLANNED has a holder or a history the re-plan must not erase.

    Args:
        document: The locked document.
        parent: The applied body being re-planned, or ``None``.

    Returns:
        The locators, empty when *parent* is ``None``.
    """
    if parent is None:
        return frozenset()
    batches = document_rows(document, Epoch2Collection.BATCH)
    tasks = document_rows(document, Epoch2Collection.TASK)
    return frozenset(
        {
            _locator(Epoch2Collection.MILESTONE, parent.milestone_urn.entity_key),
            *(
                _locator(Epoch2Collection.BATCH, batch.urn.entity_key)
                for batch in parent.batches
                if _str_field(batches.get(batch.urn.entity_key), "status")
                == BatchStatus.PLANNED.value
            ),
            *(
                _locator(Epoch2Collection.TASK, task.urn.entity_key)
                for task in parent.tasks
                if _str_field(tasks.get(task.urn.entity_key), "status") == TaskStatus.PLANNED.value
            ),
        }
    )


def _dropped(
    replannable: frozenset[str], *, collection: Epoch2Collection, kept: set[str]
) -> tuple[str, ...]:
    """Return the keys of *collection* a re-plan cancels, in key order.

    Args:
        replannable: The locators :func:`_replannable` answered.
        collection: The collection to read them from.
        kept: The keys of *collection* the child plan still names.

    Returns:
        Every still-PLANNED key the parent owned and the child dropped.
    """
    prefix = _locator(collection, "")
    return tuple(
        sorted(
            key
            for locator in replannable
            if locator.startswith(prefix) and (key := locator.removeprefix(prefix)) not in kept
        )
    )


def _default_branch(document: dict[str, Any]) -> str | None:
    """Return the branch the tree's project delivers into, if its row records one.

    A migrated tree keeps the project row as an import envelope, so the
    branch sits two payloads deep there; a native row carries it at the
    top level.

    Args:
        document: The document to read.

    Returns:
        The default branch, or ``None`` when the tree holds no project
        row or the row names no branch.
    """
    row = next(iter(document_rows(document, Epoch2Collection.PROJECT).values()), None)
    imported = _row_field(_row_field(row, "payload"), "payload")
    return _str_field(row, "default_branch") or _str_field(imported, "default_branch")


def _replan_parent(
    document: dict[str, Any], *, record: PlanRevision
) -> PlanBody | PlanRefusal | None:
    """Return the applied parent body *record* re-plans, or why it may not.

    A child whose parent targets another Milestone, or whose parent never
    created the Milestone it names, plans the ordinary way: its parent
    only informed the lenses. A child of an unapplied parent whose
    Milestone already exists is refused here, because nothing it could
    re-plan was ever placed by its parent.

    Args:
        document: The locked document.
        record: The revision being applied.

    Returns:
        The parent's body when the apply is a re-plan, ``None`` when it is
        not, or the typed refusal.
    """
    if record.parent_key is None:
        return None
    parent = _stored_revision(document, key=record.parent_key)
    if parent is None:
        return PlanRefusal(
            code=PlanRefusalCode.IDENTITY_NOT_FOUND,
            guard="parent_plan_revision_resolved",
            detail=(
                f"the plan names parent revision {record.parent_key!r}, which the document "
                "does not hold as a readable row"
            ),
            remediation="Submit the plan against a parent revision the document holds.",
            revision=record.revision,
        )
    milestone_urn = record.body.milestone_urn
    if parent.body.milestone_urn != milestone_urn:
        return None
    milestone = document_rows(document, Epoch2Collection.MILESTONE).get(milestone_urn.entity_key)
    if parent.status is not PlanRevisionStatus.APPLIED:
        if milestone is None:
            return None
        return PlanRefusal(
            code=PlanRefusalCode.TRANSITION_GUARD_FAILED,
            guard="parent_plan_revision_applied",
            detail=(
                f"Milestone {milestone_urn.entity_key} already exists and parent revision "
                f"{parent.key} is {parent.status.value}, so it placed nothing to re-plan"
            ),
            remediation="Re-plan the Milestone as a child of the revision that applied it.",
            revision=record.revision,
        )
    status = _str_field(milestone, "status")
    if status != MilestoneStatus.PLANNED.value:
        return PlanRefusal(
            code=PlanRefusalCode.TRANSITION_GUARD_FAILED,
            guard="replanned_milestone_planned",
            detail=(
                f"Milestone {milestone_urn.entity_key} is {status or 'absent'}, and only a "
                "PLANNED Milestone is re-planned"
            ),
            remediation="Change a started Milestone through its own lifecycle, not a re-plan.",
            revision=record.revision,
        )
    dropped_batches = _dropped(
        _replannable(document, parent=parent.body),
        collection=Epoch2Collection.BATCH,
        kept={batch.urn.entity_key for batch in record.body.batches},
    )
    if dropped_batches and _default_branch(document) is None:
        return PlanRefusal(
            code=PlanRefusalCode.IDENTITY_NOT_FOUND,
            guard="project_default_branch_resolved",
            detail=(
                f"cancelling {', '.join(dropped_batches)} records the branch it targeted, "
                "and the project row names no default branch"
            ),
            remediation="Record the project's default branch before re-planning.",
            revision=record.revision,
        )
    return parent.body


def _restamped[RecordT: Epoch2Record](
    prior: RecordT, *, updates: dict[str, Any], at: UtcDatetime
) -> RecordT:
    """Return *prior* rewritten by a re-plan, keeping its identity and history.

    Args:
        prior: The stored PLANNED row, validated.
        updates: The fields the child plan rewrites, in JSON mode.
        at: When the apply happened.

    Returns:
        The successor at the next revision, re-validated as a whole.

    Raises:
        ValidationError: The rewritten record breaks its own model.
    """
    return type(prior).model_validate(
        {
            **prior.model_dump(mode="json"),
            **updates,
            "revision": prior.revision + 1,
            "updated_at": at.isoformat(),
        }
    )


def _planned_milestone(
    document: dict[str, Any], *, body: PlanBody, at: UtcDatetime, replannable: frozenset[str]
) -> Milestone:
    """Return the PLANNED Milestone *body* materialises.

    Args:
        document: The locked document.
        body: The plan content.
        at: When the apply happened.
        replannable: The rows a re-plan may rewrite (see :func:`_replannable`).

    Returns:
        The Milestone at revision one, or the stored one rewritten to the
        child's contract when the apply is a re-plan.

    Raises:
        ValidationError: The Milestone does not satisfy its own model.
    """
    spec = body.milestone.model_dump(mode="json")
    key = body.milestone_urn.entity_key
    if _locator(Epoch2Collection.MILESTONE, key) in replannable:
        prior = Milestone.model_validate(document_rows(document, Epoch2Collection.MILESTONE)[key])
        return _restamped(prior, updates=spec, at=at)
    return Milestone.model_validate(
        {
            **spec,
            "uid": str(uuid.uuid5(uuid.NAMESPACE_URL, str(body.milestone_urn))),
            "urn": str(body.milestone_urn),
            "origin": _NATIVE_ORIGIN.model_dump(mode="json"),
            "revision": 1,
            "created_at": at.isoformat(),
            "updated_at": at.isoformat(),
            "status": MilestoneStatus.PLANNED.value,
        }
    )


def _planned_batches(
    document: dict[str, Any], *, body: PlanBody, at: UtcDatetime, replannable: frozenset[str]
) -> tuple[DeliveryBatch, ...]:
    """Return the PLANNED Batches *body* materialises, in declared order.

    Args:
        document: The locked document.
        body: The plan content.
        at: When the apply happened.
        replannable: The rows a re-plan may rewrite (see :func:`_replannable`).

    Returns:
        One Batch per plan entry: created at revision one, or, for a
        PLANNED Batch a re-plan carries, the stored one re-placed.

    Raises:
        ValidationError: A Batch does not satisfy its own model.
    """
    stored = document_rows(document, Epoch2Collection.BATCH)
    batches: list[DeliveryBatch] = []
    for batch in body.batches:
        placement = {
            "milestone_ref": str(body.milestone_urn),
            "repository_ref": str(batch.repository_ref),
            "task_refs": [str(task.urn) for task in body.tasks if task.batch_ref == batch.urn],
            "status": BatchStatus.PLANNED.value,
        }
        key = batch.urn.entity_key
        if _locator(Epoch2Collection.BATCH, key) in replannable:
            prior = DeliveryBatch.model_validate(stored[key])
            batches.append(_restamped(prior, updates=placement, at=at))
            continue
        batches.append(
            DeliveryBatch.model_validate(
                {
                    **placement,
                    "uid": str(uuid.uuid5(uuid.NAMESPACE_URL, str(batch.urn))),
                    "key": key,
                    "urn": str(batch.urn),
                    "origin": _NATIVE_ORIGIN.model_dump(mode="json"),
                    "revision": 1,
                    "created_at": at.isoformat(),
                    "updated_at": at.isoformat(),
                }
            )
        )
    return tuple(batches)


def _planned_task(
    document: dict[str, Any],
    *,
    body: PlanBody,
    task: PlannedTask,
    at: UtcDatetime,
    replannable: frozenset[str] = frozenset(),
) -> Task:
    """Return the PLANNED Task one plan entry materialises.

    An entry naming a backlog draft promotes that draft through the
    registry's own promotion edge, so the Task keeps its identifier, its
    history and any due scope the draft already carried, and takes the
    plan's placement and graph edges like a created one. An entry naming
    a PLANNED Task a re-plan carries re-places it the same way and bumps
    its contract revision, since its criteria may have changed under it.
    Every other entry creates the Task at revision one.

    Args:
        document: The locked document.
        body: The plan content.
        task: The plan entry.
        at: When the apply happened.
        replannable: The rows a re-plan may rewrite (see :func:`_replannable`);
            empty for a plan that creates everything it names.

    Returns:
        The PLANNED Task.

    Raises:
        ValidationError: The Task does not satisfy its own model.
    """
    placement = {
        "batch_ref": str(task.batch_ref),
        "priority": task.priority.value,
        "intent": task.intent,
        "criteria": [item.model_dump(mode="json") for item in task.criteria],
        "depends_on": [str(ref) for ref in task.depends_on],
        "write_claims": list(task.write_claims),
        "exclusive": task.exclusive,
    }
    key = task.urn.entity_key
    if _locator(Epoch2Collection.TASK, key) in replannable:
        prior = Task.model_validate(document_rows(document, Epoch2Collection.TASK)[key])
        return _restamped(
            prior,
            updates={
                **placement,
                "due_scope": str(body.milestone_urn),
                "contract_revision": prior.contract_revision + 1,
            },
            at=at,
        )
    draft = _promotable_draft(document, key=key)
    if draft is None:
        return Task.model_validate(
            {
                **placement,
                "uid": str(uuid.uuid5(uuid.NAMESPACE_URL, str(task.urn))),
                "key": key,
                "urn": str(task.urn),
                "origin": _NATIVE_ORIGIN.model_dump(mode="json"),
                "revision": 1,
                "created_at": at.isoformat(),
                "updated_at": at.isoformat(),
                "due_scope": str(body.milestone_urn),
                "contract_revision": 1,
                "status": TaskStatus.PLANNED.value,
            }
        )
    due_scope = draft.due_scope if draft.due_scope is not None else body.milestone_urn
    promoted = apply_transition(
        draft, to=TaskStatus.PLANNED, at=at, updates={**placement, "due_scope": str(due_scope)}
    )
    # The draft was just read as a native DRAFT and every promotion field
    # is supplied, so the edge cannot be denied here.
    assert not isinstance(promoted, TransitionDenied), promoted
    return promoted.record


def materialise_plan(
    document: dict[str, Any],
    *,
    record: PlanRevision,
    at: UtcDatetime,
    parent: PlanBody | None,
) -> dict[str, Any]:
    """Return the whole document an applied plan leaves behind.

    The successor is built by deep-copying and editing rather than by
    mutating in place, because the leak scrub diffs the two payloads and
    an in-place edit would leave it comparing one payload with itself.

    A re-plan rewrites the PLANNED Milestone and every still-PLANNED Batch
    and Task the child carries at their next revision, keeping each row's
    identity, and cancels each still-PLANNED Batch and Task the parent
    placed and the child dropped. A cancelled Batch records the project's
    default branch as its target, because the cancellation edge requires
    the branch the work was aimed at and a PLANNED Batch has none yet.
    Dropped rows already past PLANNED are left as they are: the apply
    refuses before reaching here if the child still names one.

    Args:
        document: The locked document as read.
        record: The revision, already advanced to ``APPLIED``.
        at: When the apply happened.
        parent: The applied body *record* re-plans, as
            :func:`_replan_parent` resolved it, or ``None`` for a plan
            that creates everything it names.

    Returns:
        The successor document, carrying the Milestone, its Batches, its
        PLANNED Tasks, any cancelled leftovers of a re-plan, and the
        applied revision.

    Raises:
        ValidationError: A materialised record does not satisfy its own
            model, which refuses before anything is written.
    """
    body = record.body
    replannable = _replannable(document, parent=parent)
    new_document = copy.deepcopy(document)
    milestone = _planned_milestone(document, body=body, at=at, replannable=replannable)
    new_document.setdefault(Epoch2Collection.MILESTONE.value, {})[milestone.key] = (
        milestone.model_dump(mode="json")
    )
    batches = new_document.setdefault(Epoch2Collection.BATCH.value, {})
    for batch in _planned_batches(document, body=body, at=at, replannable=replannable):
        batches[batch.key] = batch.model_dump(mode="json")
    tasks = new_document.setdefault(Epoch2Collection.TASK.value, {})
    for entry in body.tasks:
        task = _planned_task(document, body=body, task=entry, at=at, replannable=replannable)
        tasks[task.key] = task.model_dump(mode="json")
    dropped_batches = _dropped(
        replannable,
        collection=Epoch2Collection.BATCH,
        kept={batch.urn.entity_key for batch in body.batches},
    )
    for key in dropped_batches:
        cancelled_batch = apply_transition(
            DeliveryBatch.model_validate(batches[key]),
            to=BatchStatus.CANCELLED,
            at=at,
            updates={"target_branch": _default_branch(document)},
        )
        # A dropped Batch was read as a native PLANNED row and its branch
        # was resolved before the apply was admitted.
        assert not isinstance(cancelled_batch, TransitionDenied), cancelled_batch
        batches[key] = cancelled_batch.record.model_dump(mode="json")
    dropped_tasks = _dropped(
        replannable,
        collection=Epoch2Collection.TASK,
        kept={task.urn.entity_key for task in body.tasks},
    )
    for key in dropped_tasks:
        cancelled_task = apply_transition(
            Task.model_validate(tasks[key]), to=TaskStatus.CANCELLED, at=at
        )
        assert not isinstance(cancelled_task, TransitionDenied), cancelled_task
        tasks[key] = cancelled_task.record.model_dump(mode="json")
    new_document.setdefault(PLAN_REVISION_COLLECTION.value, {})[record.key] = record.model_dump(
        mode="json"
    )
    return new_document


def _stored_revision(document: dict[str, Any], *, key: str) -> PlanRevision | None:
    """Return the stored revision *key* addresses, or ``None``.

    Args:
        document: The document to read.
        key: The plan-revision key.

    Returns:
        The validated revision, or ``None`` when the document holds no
        such row or the row does not validate.
    """
    row = document_rows(document, PLAN_REVISION_COLLECTION).get(key)
    if row is None:
        return None
    try:
        return PlanRevision.model_validate(row)
    except ValueError:
        logger.warning(f"_stored_revision rejected a stored row plan_revision_key={key!r}")
        return None


__all__ = ["PLAN_REVISION_COLLECTION", "materialise_plan"]
