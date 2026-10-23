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

A plan that is not a re-plan may also claim a bare PLANNED Milestone:
one made by ``eawf milestone create`` that no applied plan placed and no
Batch belongs to yet. It rewrites that row in place to its contract, as
a re-plan would, because the operator created the Milestone precisely so
that a plan could fill it (see :func:`_milestone_claim_refusal`).

An ACTIVE Milestone takes a re-plan only when every change is an append:
new Batches and Tasks, and Batch refs added after the required ones it
already has, including a Batch of its own that was created outside a
plan. Its existing rows are left exactly as they are, because work on
them may be underway; anything else is refused like any other re-plan of
a started Milestone.

A Task or Batch that compacted out of the document into its ledger is
still placed: every judgement here reads the document with those rows laid
under it (see :func:`with_compacted_rows`), so a plan never recreates or
rewrites a row in either tier.
"""

from __future__ import annotations

import copy
import logging
import uuid
from collections.abc import Mapping
from typing import Any, Final

from eawf.kernel.state.epoch2.batch import BatchStatus, DeliveryBatch
from eawf.kernel.state.epoch2.lifecycle import TransitionDenied, apply_transition
from eawf.kernel.state.epoch2.milestone import Milestone, MilestoneCreateSpec, MilestoneStatus
from eawf.kernel.state.epoch2.plan_revision import (
    PlanBody,
    PlannedTask,
    PlanRevision,
    PlanRevisionStatus,
)
from eawf.kernel.state.epoch2.task import TERMINAL_TASK_STATUSES, Task, TaskStatus
from eawf.kernel.state.epoch2.urns import TaskUrn
from eawf.kernel.state.epoch2.values import EntityOrigin, Epoch2Record
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.workflow.planning.revision import PlanRefusal, PlanRefusalCode

logger = logging.getLogger(__name__)

#: The collection plan revisions are stored under, at the document tier.
PLAN_REVISION_COLLECTION: Final = Epoch2Collection.PLAN_REVISION

#: The stored states of a Task an append may list in its ``dropped_tasks``.
_DROPPABLE: Final = frozenset({TaskStatus.CANCELLED.value, TaskStatus.DROPPED.value})

#: The backlog states: a plan naming a Task in one places it for the first time.
_BACKLOG: Final = frozenset({TaskStatus.DRAFT.value, TaskStatus.DEFERRED.value})

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


def with_compacted_rows(
    document: dict[str, Any], *, compacted: Mapping[Epoch2Collection, Mapping[str, dict[str, Any]]]
) -> dict[str, Any]:
    """Return *document* with each compacted row laid under its collection.

    The result is for judging a plan only, never for writing: it carries
    rows that live in the ledger tier, which the document must not regain.

    Args:
        document: The locked document.
        compacted: Each collection's latest ledger rows, keyed by record key.

    Returns:
        A shallow copy whose named collections hold their ledger rows and
        then their document rows, so a row in the document wins.
    """
    return {
        **document,
        **{
            collection.value: {**rows, **document_rows(document, collection)}
            for collection, rows in compacted.items()
        },
    }


def appended_placements(document: dict[str, Any], *, body: PlanBody) -> dict[TaskUrn, TaskStatus]:
    """Return the stored status of each plan Task already placed under an ACTIVE Milestone.

    Args:
        document: The document with its compacted rows laid under it (see
            :func:`with_compacted_rows`), so a finished Task counts too.
        body: The plan content, which appends to the Milestone it names.

    Returns:
        Each restated Task's stored status, keyed by its URN; empty when the
        Milestone is not ACTIVE, and without a backlog Task the plan promotes.

    Raises:
        ValueError: A stored Task holds a status no Task has.
    """
    if not _active(document, body=body):
        return {}
    tasks = document_rows(document, Epoch2Collection.TASK)
    return {
        task.urn: TaskStatus(status)
        for task in body.tasks
        if (status := _str_field(tasks.get(task.urn.entity_key), "status")) is not None
        and status not in _BACKLOG
    }


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


def _milestone_claim_refusal(document: dict[str, Any], *, body: PlanBody) -> str | None:
    """Return why a plan that is not a re-plan may not claim its stored Milestone, if it may not.

    A Milestone made outside a plan holds no contract a plan wrote and no
    work, so the first plan to name it fills it in place rather than
    being refused for reusing its key. Anything that suggests the row is
    already planned or under way keeps it claimed: a row imported from
    the previous epoch is immutable to this lifecycle, a row past PLANNED
    has started, and a row with required Batches, a Batch of its own or
    an applied plan naming it was placed already. A plan that was applied
    and later superseded still left its Batches behind, so the Batch
    check covers it too.

    Args:
        document: The locked document, with its compacted rows laid under
            it (see :func:`with_compacted_rows`).
        body: The plan content, whose Milestone the document holds.

    Returns:
        A clause naming why the row stays claimed, or ``None`` when the
        plan may claim it.
    """
    urn = str(body.milestone_urn)
    row = document_rows(document, Epoch2Collection.MILESTONE)[body.milestone_urn.entity_key]
    status = _str_field(row, "status")
    if _str_field(_row_field(row, "origin"), "kind") == "legacy":
        return "it was imported from the previous epoch"
    if status != MilestoneStatus.PLANNED.value:
        return f"it is {status or 'unreadable'}, not PLANNED"
    if _row_field(row, "required_batch_refs"):
        return "it already requires Batches"
    owned = sorted(
        key
        for key, batch in document_rows(document, Epoch2Collection.BATCH).items()
        if _str_field(batch, "milestone_ref") == urn
    )
    if owned:
        return f"Batch {owned[0]} already belongs to it"
    applied = sorted(
        key
        for key, revision in document_rows(document, PLAN_REVISION_COLLECTION).items()
        if _str_field(revision, "status") == PlanRevisionStatus.APPLIED.value
        and _str_field(_row_field(revision, "body"), "milestone_urn") == urn
    )
    if applied:
        return f"applied plan revision {applied[0]} placed it"
    return None


def _claimable(
    document: dict[str, Any], *, body: PlanBody, parent: PlanBody | None
) -> frozenset[str]:
    """Return the locator of the stored Milestone a plan that is not a re-plan may fill.

    Args:
        document: The locked document, with its compacted rows laid under it.
        body: The plan content.
        parent: The applied body *body* re-plans, or ``None``.

    Returns:
        The Milestone's locator, or nothing when *body* re-plans, the
        document holds no such Milestone, or the row stays claimed (see
        :func:`_milestone_claim_refusal`).
    """
    key = body.milestone_urn.entity_key
    if (
        parent is not None
        or key not in document_rows(document, Epoch2Collection.MILESTONE)
        or _milestone_claim_refusal(document, body=body) is not None
    ):
        return frozenset()
    return frozenset({_locator(Epoch2Collection.MILESTONE, key)})


def _claimed_keys(
    document: dict[str, Any], *, body: PlanBody, parent: PlanBody | None
) -> tuple[str, ...]:
    """Return the locators the document already holds that the plan creates.

    A backlog draft the plan promotes is not claimed: the plan places it
    rather than creating it. Neither is a bare PLANNED Milestone a plan
    that is not a re-plan fills (see :func:`_claimable`), a row a re-plan
    may rewrite (see :func:`_replannable`) or an ACTIVE Milestone's own
    row an append leaves in place (see :func:`_kept`); any other row a
    re-plan names, including one its parent owned that has since moved
    past PLANNED, stays claimed.

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
    owned = (
        _replannable(document, parent=parent)
        | _kept(document, body=body, parent=parent)
        | _claimable(document, body=body, parent=parent)
    )
    return tuple(
        locator
        for collection, keys in wanted.items()
        for key in keys
        if key in document_rows(document, collection)
        and (locator := _locator(collection, key)) not in owned
    )


def _locator(collection: Epoch2Collection, key: str) -> str:
    """Return the ``<collection>/<key>`` locator one stored row is named by."""
    return f"{collection.value}/{key}"


def _replannable(document: dict[str, Any], *, parent: PlanBody | None) -> frozenset[str]:
    """Return the locators of the rows a re-plan of *parent* may rewrite or cancel.

    The Milestone is always among them; whether it is still PLANNED, or
    ACTIVE and only appended to, is refused separately by
    :func:`_replan_parent`, which names the cause. A Batch or Task
    qualifies only while it is PLANNED, because a row past PLANNED has a
    holder or a history the re-plan must not erase, and never under an
    ACTIVE Milestone, whose existing rows an append leaves as they are.

    Args:
        document: The locked document.
        parent: The applied body being re-planned, or ``None``.

    Returns:
        The locators, empty when *parent* is ``None``.
    """
    if parent is None:
        return frozenset()
    milestone = _locator(Epoch2Collection.MILESTONE, parent.milestone_urn.entity_key)
    if _active(document, body=parent):
        return frozenset({milestone})
    batches = document_rows(document, Epoch2Collection.BATCH)
    tasks = document_rows(document, Epoch2Collection.TASK)
    return frozenset(
        {
            milestone,
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


def _active(document: dict[str, Any], *, body: PlanBody) -> bool:
    """Return whether the Milestone *body* addresses is stored ACTIVE."""
    row = document_rows(document, Epoch2Collection.MILESTONE).get(body.milestone_urn.entity_key)
    return _str_field(row, "status") == MilestoneStatus.ACTIVE.value


def _kept(document: dict[str, Any], *, body: PlanBody, parent: PlanBody | None) -> frozenset[str]:
    """Return the locators of the ACTIVE Milestone's own rows an append leaves as they are.

    A Batch is the Milestone's own when its stored ``milestone_ref``
    names it, and a Task when its stored Batch is one of those. A row the
    plan names under any other Milestone is not kept, so it stays claimed
    and the apply refuses it as a key the plan does not own.

    Args:
        document: The locked document.
        body: The plan content.
        parent: The applied body *body* re-plans, or ``None``.

    Returns:
        The locators, empty unless *body* re-plans an ACTIVE Milestone.
    """
    if parent is None or not _active(document, body=body):
        return frozenset()
    batches = document_rows(document, Epoch2Collection.BATCH)
    tasks = document_rows(document, Epoch2Collection.TASK)
    own_batches = tuple(
        batch.urn
        for batch in body.batches
        if _str_field(batches.get(batch.urn.entity_key), "milestone_ref") == str(body.milestone_urn)
    )
    own_refs = {str(urn) for urn in own_batches}
    return frozenset(
        {
            *(_locator(Epoch2Collection.BATCH, urn.entity_key) for urn in own_batches),
            *(
                _locator(Epoch2Collection.TASK, task.urn.entity_key)
                for task in body.tasks
                if _str_field(tasks.get(task.urn.entity_key), "batch_ref") in own_refs
            ),
        }
    )


def _non_append(document: dict[str, Any], *, body: PlanBody) -> str | None:
    """Return how *body* edits or removes its ACTIVE Milestone's scope, if it does.

    Only the Milestone's own scope is judged here; a row the plan names
    under another Milestone is refused by the claimed-key check instead.

    Args:
        document: The locked document, whose Milestone *body* addresses
            is stored ACTIVE.
        body: The plan content.

    Returns:
        A clause naming the first change that is not an append, or
        ``None`` when the plan only appends.

    Raises:
        ValidationError: The stored Milestone, or a stored Task the plan
            names, does not satisfy its own model.
    """
    milestone = Milestone.model_validate(
        document_rows(document, Epoch2Collection.MILESTONE)[body.milestone_urn.entity_key]
    )
    contract = set(MilestoneCreateSpec.model_fields) - {"required_batch_refs"}
    if body.milestone.model_dump(mode="json", include=contract) != milestone.model_dump(
        mode="json", include=contract
    ):
        return "rewrites the Milestone's contract"
    required = milestone.required_batch_refs
    if body.milestone.required_batch_refs[: len(required)] != required:
        return "removes or reorders a required Batch"
    batches = document_rows(document, Epoch2Collection.BATCH)
    for batch in body.batches:
        row = batches.get(batch.urn.entity_key)
        if _str_field(row, "milestone_ref") == str(body.milestone_urn) and _str_field(
            row, "repository_ref"
        ) != str(batch.repository_ref):
            return f"moves Batch {batch.urn.entity_key} to another repository"
    tasks = document_rows(document, Epoch2Collection.TASK)
    for task in body.tasks:
        edit = _task_edit(document, task=task)
        if edit is not None:
            return edit
    for dropped in body.dropped_tasks:
        key = dropped.task_ref.entity_key
        if _str_field(tasks.get(key), "status") not in _DROPPABLE:
            return f"drops Task {key}, which is not cancelled or dropped"
    return None


def _task_edit(document: dict[str, Any], *, task: PlannedTask) -> str | None:
    """Return how one plan Task edits an ACTIVE Milestone's scope, if it does.

    A Task the document does not hold, or holds only as a promotable
    draft, is new and may land only in a Batch the plan creates, since an
    existing Batch's Task list is part of the scope an append keeps. A
    Task the document holds must be restated exactly as stored, except
    that a terminal Task need only keep its Batch: the append never
    writes it, and its stored content may carry what a plan cannot
    restate, such as an operator's criterion waiver or a dependency on a
    Task the plan drops.

    Args:
        document: The locked document.
        task: The plan entry.

    Returns:
        A clause naming the edit, or ``None`` when the entry appends or
        restates.

    Raises:
        ValidationError: The stored Task does not satisfy its own model.
    """
    key = task.urn.entity_key
    row = document_rows(document, Epoch2Collection.TASK).get(key)
    if row is None or _promotable_draft(document, key=key) is not None:
        batch_key = task.batch_ref.entity_key
        if batch_key in document_rows(document, Epoch2Collection.BATCH):
            return f"adds Task {key} to Batch {batch_key}, which already exists"
        return None
    stored = Task.model_validate(row)
    if stored.status in TERMINAL_TASK_STATUSES:
        return None if stored.batch_ref == task.batch_ref else f"changes or moves Task {key}"
    placed = (
        stored.batch_ref,
        stored.priority,
        stored.intent,
        stored.criteria,
        stored.depends_on,
        stored.write_claims,
        stored.exclusive,
    )
    planned = (
        task.batch_ref,
        task.priority,
        task.intent,
        task.criteria,
        task.depends_on,
        task.write_claims,
        task.exclusive,
    )
    return None if placed == planned else f"changes or moves Task {key}"


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
    re-plan was ever placed by its parent. So is a child of an ACTIVE
    Milestone that does more than append (see :func:`_non_append`).

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
    if status == MilestoneStatus.ACTIVE.value:
        edit = _non_append(document, body=record.body)
        if edit is None:
            return parent.body
        return PlanRefusal(
            code=PlanRefusalCode.TRANSITION_GUARD_FAILED,
            guard="replanned_milestone_planned",
            detail=(
                f"Milestone {milestone_urn.entity_key} is ACTIVE, which a re-plan may only "
                f"append to, and the plan {edit}"
            ),
            remediation=(
                "Re-plan an ACTIVE Milestone by appending Batches and Tasks only, and change "
                "its existing scope through its own lifecycle."
            ),
            revision=record.revision,
        )
    if status != MilestoneStatus.PLANNED.value:
        return PlanRefusal(
            code=PlanRefusalCode.TRANSITION_GUARD_FAILED,
            guard="replanned_milestone_planned",
            detail=(
                f"Milestone {milestone_urn.entity_key} is {status or 'absent'}, and only a "
                "PLANNED Milestone is re-planned, or an ACTIVE one appended to"
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
    document: dict[str, Any], *, body: PlanBody, at: UtcDatetime, rewritable: frozenset[str]
) -> Milestone:
    """Return the PLANNED Milestone *body* materialises.

    Args:
        document: The locked document.
        body: The plan content.
        at: When the apply happened.
        rewritable: The rows the plan may rewrite in place: those a
            re-plan may rewrite (see :func:`_replannable`) or the bare
            Milestone a first plan fills (see :func:`_claimable`).

    Returns:
        The Milestone at revision one, or the stored one rewritten to the
        plan's contract when the apply is a re-plan or fills a bare one.

    Raises:
        ValidationError: The Milestone does not satisfy its own model.
    """
    spec = body.milestone.model_dump(mode="json")
    key = body.milestone_urn.entity_key
    if _locator(Epoch2Collection.MILESTONE, key) in rewritable:
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
        if batch.title is not None:
            # An untitled plan entry keeps a title the Batch was already given.
            placement["title"] = batch.title
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
        # A label the plan leaves unset keeps whatever the promoted draft
        # or the re-placed Task already carries.
        **task.model_dump(
            mode="json", include={"title", "description", "role", "effort"}, exclude_none=True
        ),
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
    placed: dict[str, Any],
    record: PlanRevision,
    at: UtcDatetime,
    parent: PlanBody | None,
) -> dict[str, Any]:
    """Return the whole document an applied plan leaves behind.

    The successor is built by deep-copying and editing rather than by
    mutating in place, because the leak scrub diffs the two payloads and
    an in-place edit would leave it comparing one payload with itself.

    A plan that is not a re-plan rewrites a bare PLANNED Milestone it
    claims (see :func:`_claimable`) at its next revision, keeping its
    identity, and creates everything else it names.

    A re-plan rewrites the PLANNED Milestone and every still-PLANNED Batch
    and Task the child carries at their next revision, keeping each row's
    identity, and cancels each still-PLANNED Batch and Task the parent
    placed and the child dropped. A cancelled Batch records the project's
    default branch as its target, because the cancellation edge requires
    the branch the work was aimed at and a PLANNED Batch has none yet.
    Dropped rows already past PLANNED are left as they are: the apply
    refuses before reaching here if the child still names one. A re-plan
    of an ACTIVE Milestone rewrites only the Milestone's required Batches
    and adds the new rows, leaving every row it keeps untouched.

    Args:
        document: The locked document as read, which the successor copies.
        placed: *document* with its compacted rows laid under it (see
            :func:`with_compacted_rows`), which every judgement reads.
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
    replannable = _replannable(placed, parent=parent)
    kept = _kept(placed, body=body, parent=parent)
    new_document = copy.deepcopy(document)
    milestone = _planned_milestone(
        placed,
        body=body,
        at=at,
        rewritable=replannable | _claimable(placed, body=body, parent=parent),
    )
    new_document.setdefault(Epoch2Collection.MILESTONE.value, {})[milestone.key] = (
        milestone.model_dump(mode="json")
    )
    batches = new_document.setdefault(Epoch2Collection.BATCH.value, {})
    for batch in _planned_batches(placed, body=body, at=at, replannable=replannable):
        if _locator(Epoch2Collection.BATCH, batch.key) not in kept:
            batches[batch.key] = batch.model_dump(mode="json")
    tasks = new_document.setdefault(Epoch2Collection.TASK.value, {})
    for entry in body.tasks:
        if _locator(Epoch2Collection.TASK, entry.urn.entity_key) in kept:
            continue
        task = _planned_task(placed, body=body, task=entry, at=at, replannable=replannable)
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


__all__ = [
    "PLAN_REVISION_COLLECTION",
    "appended_placements",
    "materialise_plan",
    "with_compacted_rows",
]
