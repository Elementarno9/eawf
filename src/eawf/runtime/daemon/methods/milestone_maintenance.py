"""The verbs that keep a Milestone's record right without hand-editing state.

Three things an operator needs to do to a Milestone are not lifecycle moves
of its own, or are more than one move:

- ``domain.milestone.edit`` rewrites the contract of a PLANNED Milestone:
  its title, description, outcome, appetite, exclusions and acceptance
  journey. Once a Milestone is ACTIVE its contract is what the work in
  flight was planned against, so an edit is refused rather than moving
  the goalposts under it. The status does not move, so the event is a
  ``milestone.edited`` row outside the closed ``domain`` vocabulary.
- ``domain.milestone.cancel`` is the registered lifecycle edge, and
  :func:`cancel_with_cascade` is the commit it takes. A cancelled
  Milestone's open work has nothing left to land in, so the PLANNED and
  ACTIVE Batches it owns are cancelled with it, their open Tasks are
  cancelled, and the backlog Tasks due on it are dropped -- all in the one
  document write, so no reader ever sees a cancelled Milestone still owning
  live work. A Task the registry gives no cancel edge (one ready to
  integrate) refuses the whole cancel, having written nothing. A cancelled
  Task's live Run is then asked to cancel through ``runtime.run.control.request``,
  the verb ``eawf run cancel`` sends, because a Run ends only on the effect
  its worker reports.
- ``domain.milestone.purge`` removes a CANCELLED Milestone outright. A
  ledger is append-only because a line someone else may hold must never
  change under them; a line no pushed commit carries is held by nobody
  else, so a placeholder created and cancelled by mistake can be taken out
  instead of living in history for ever. The CANCELLED Batches filed
  under it and their CANCELLED or DROPPED Tasks go with it; any other
  status among them refuses the purge. So does any row that stays, in any
  collection's document rows or ledger, and names one of the records the
  purge would remove -- a Run that worked one of its Tasks, a PlanRevision
  that planned it, another Task depending on one -- because such a row
  records work or a decision that happened, and removing what it names
  would leave it dangling. A Milestone any remote-tracking ref of the
  checkout mentions is refused, and so is one a ledger snapshot already
  seals.

The field edit commits through the transaction's own write step, so a retry
under the same key replays its receipt and a stale revision is a conflict.
:func:`commit_record_edit` is shared with the Track maintenance verbs.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import subprocess
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final, Self

import orjson
from pydantic import (
    BaseModel,
    ConfigDict,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from eawf.kernel.identity import EntityKind, QualifiedUrn
from eawf.kernel.runtime.control import TERMINAL_RUN_STATUSES
from eawf.kernel.state.canonical_sequence import CanonicalSequenceAllocator
from eawf.kernel.state.enums import EffortBucket, StoreKind
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import (
    NonEmptyStr,
    OutcomeStr,
    PrincipalKey,
    StrictPositiveInt,
    TitleStr,
)
from eawf.kernel.state.epoch2.batch import BatchStatus, DeliveryBatch
from eawf.kernel.state.epoch2.lifecycle import (
    GuardContext,
    LifecycleRecord,
    TransitionDenied,
    apply_transition,
)
from eawf.kernel.state.epoch2.milestone import (
    AcceptanceStep,
    DurationBudget,
    Milestone,
    MilestoneStatus,
    validate_acceptance_journey,
    validate_exclusions,
)
from eawf.kernel.state.epoch2.task import Task, TaskStatus
from eawf.kernel.state.epoch2.track import Track
from eawf.kernel.state.epoch2.transitions import (
    LifecycleEntity,
    LifecycleStatus,
    TransitionGuard,
)
from eawf.kernel.state.epoch2.urns import AnyEntityUrn
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import (
    LedgerRecord,
    _atomic_write,
    latest_rows,
    read_ledger_records,
    split_ledger_lines,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import ENTITY_COLLECTIONS, LEDGER_COLLECTIONS, Epoch2Collection
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.runtime.daemon.epoch2_edges import edge_updates, locked_unmet
from eawf.runtime.daemon.epoch2_recovery import (
    PROJECTION_DEGRADED,
    publish_projection,
)
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import (
    _STRUCTURAL_DENIALS,
    CANONICAL_SEQUENCE_KEY,
    CommittedTransaction,
    TransactionRefusalCode,
    TransactionRefusedError,
    TransitionRequest,
    _CommitPlan,
    _compact,
    _denied,
    _high_water_mark,
    _persist,
    _plan_commit,
    _replayed_receipt,
    _reread_record,
    _terminal_ledger_record,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.domain_envelope import (
    accepted_envelope,
    refused_envelope,
    schema_refusal,
)
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_mutator

logger = logging.getLogger(__name__)

#: The dotted JSON-RPC name of the contract edit.
MILESTONE_EDIT: Final = "domain.milestone.edit"

#: The dotted JSON-RPC name of the purge.
MILESTONE_PURGE: Final = "domain.milestone.purge"

#: The firehose name of a committed edit, outside the closed ``domain`` vocabulary.
EDITED_EVENT: Final = "milestone.edited"

#: The firehose name of a committed purge.
PURGED_EVENT: Final = "milestone.purged"

#: Version of the payload a maintenance firehose row carries.
MAINTENANCE_EVENT_SCHEMA_VERSION: Final = "1"

#: The collections a purge removes records from: the Milestone and the work filed under it.
_PURGED: Final = (Epoch2Collection.MILESTONE, Epoch2Collection.BATCH, Epoch2Collection.TASK)

#: The guard a purge of a Milestone some remote already holds is refused under.
UNPUSHED_GUARD: Final = "record_unpushed"

#: The contract fields an edit may rewrite, in the order the request declares them.
MILESTONE_EDIT_FIELDS: Final = (
    "title",
    "description",
    "outcome",
    "appetite",
    "exclusions",
    "acceptance_journey",
)

#: The Batch statuses a cancel takes down with its Milestone. A Batch past
#: ACTIVE is integrating work the registry gives no cancel edge to.
_CASCADED_BATCHES: Final = frozenset({BatchStatus.PLANNED, BatchStatus.ACTIVE})

#: The Task statuses of delivery work still open inside a Batch.
_OPEN_TASKS: Final = frozenset(
    {
        TaskStatus.PLANNED,
        TaskStatus.CLAIMED,
        TaskStatus.RUNNING,
        TaskStatus.READY_TO_INTEGRATE,
    }
)

#: The stored statuses of a Run that has stopped, which a cancel asks nothing of.
_ENDED_RUNS: Final = frozenset(status.value for status in TERMINAL_RUN_STATUSES)

#: The backlog statuses a Task due on a cancelled Milestone is dropped from.
_BACKLOG_TASKS: Final = frozenset({TaskStatus.DRAFT, TaskStatus.DEFERRED})

_Key = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]
_Correlation = Annotated[str, StringConstraints(strict=True, max_length=128)]


class RecordEditRequest(BaseModel):
    """The addressing every maintenance request carries.

    Attributes:
        urn: The record to edit.
        expected_revision: The compare-and-swap token the caller read it at.
        idempotency_key: The client's name for this request.
        actor: Who asked, as an immutable qualified principal key.
        correlation_id: The client's thread of related requests.
    """

    model_config = ConfigDict(extra="forbid")

    urn: AnyEntityUrn
    expected_revision: StrictPositiveInt
    idempotency_key: _Key
    actor: PrincipalKey
    correlation_id: _Correlation | None = None


class MilestoneEditRequest(RecordEditRequest):
    """The strict parameters of :data:`MILESTONE_EDIT`.

    A field left out keeps its stored value, so a caller names only what
    changes; a request naming nothing is refused rather than committed as
    a revision that changes nothing.

    Attributes:
        title: The new title.
        description: The new long-form description.
        outcome: The new outcome statement.
        appetite: The new effort bucket or duration budget.
        exclusions: The new exclusion list, whole.
        acceptance_journey: The new acceptance journey, whole.
    """

    title: TitleStr | None = None
    description: NonEmptyStr | None = None
    outcome: OutcomeStr | None = None
    appetite: EffortBucket | DurationBudget | None = None
    exclusions: tuple[NonEmptyStr, ...] | None = None
    acceptance_journey: tuple[AcceptanceStep, ...] | None = None

    @field_validator("exclusions")
    @classmethod
    def _exclusions_are_legal(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        """Hold a new exclusion list to the rule the stored one is held to.

        Raises:
            ValueError: The list breaks an exclusion rule.
        """
        return None if value is None else validate_exclusions(value)

    @field_validator("acceptance_journey")
    @classmethod
    def _journey_is_ordered(
        cls, value: tuple[AcceptanceStep, ...] | None
    ) -> tuple[AcceptanceStep, ...] | None:
        """Hold a new journey to the rule the stored one is held to.

        Raises:
            ValueError: The journey breaks a step-ordering rule.
        """
        return None if value is None else validate_acceptance_journey(value)

    @model_validator(mode="after")
    def _names_a_change(self) -> Self:
        """Require at least one contract field.

        Raises:
            ValueError: The request names no field to change.
        """
        if not self.edits():
            raise ValueError(f"name at least one of {', '.join(MILESTONE_EDIT_FIELDS)}")
        return self

    def edits(self) -> dict[str, Any]:
        """Return the fields this request rewrites, as their stored JSON form."""
        return self.model_dump(mode="json", include=set(MILESTONE_EDIT_FIELDS), exclude_none=True)


class MilestonePurgeRequest(RecordEditRequest):
    """The strict parameters of :data:`MILESTONE_PURGE`: the CANCELLED Milestone's address.

    A purge carries nothing beyond the addressing every maintenance request
    does, so it commits through the same transaction step as an edit.
    """


def _kind_refusal(urn: QualifiedUrn, *, method: str, kind: EntityKind) -> TransactionRefusedError:
    """Return the refusal of a verb pointed at another entity's record."""
    return TransactionRefusedError(
        code=TransactionRefusalCode.IDENTITY_KIND_MISMATCH,
        detail=f"{method} addresses a {kind.value} but the request names a {urn.kind.value}",
        entity_ref=str(urn),
        remediation=f"Name a {kind.value} URN.",
    )


def _stale(urn: QualifiedUrn, *, stored: int, expected: int) -> TransactionRefusedError:
    """Return the refusal of a request built on a revision the record moved past."""
    return TransactionRefusedError(
        code=TransactionRefusalCode.REVISION_CONFLICT,
        detail=f"the record is at revision {stored} but the request expects {expected}",
        entity_ref=str(urn),
        remediation="Re-read the record and retry against its current revision.",
        revision=stored,
    )


def _held[RecordT: (Milestone, Track)](
    document: dict[str, Any], request: RecordEditRequest, *, model: type[RecordT]
) -> RecordT:
    """Return the record the request addresses, at the revision it names.

    Raises:
        TransactionRefusedError: The document holds no such row, holds one
            that does not validate, or holds it at another revision.
    """
    urn = request.urn
    row = document_rows(document, ENTITY_COLLECTIONS[urn.kind]).get(urn.entity_key)
    if row is None:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.IDENTITY_NOT_FOUND,
            detail=f"the document holds no open {urn.kind.value} keyed {urn.entity_key!r}",
            entity_ref=str(urn),
            remediation="Name a record this tree holds open; a closed one is read from its ledger.",
        )
    try:
        record = model.model_validate(row)
    except ValidationError as error:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
            detail=f"the stored {urn.kind.value} row does not validate through its model",
            entity_ref=str(urn),
            remediation="Repair the stored row before editing it.",
        ) from error
    if record.revision != request.expected_revision:
        raise _stale(urn, stored=record.revision, expected=request.expected_revision)
    return record


def _edit_envelope(
    request: RecordEditRequest,
    *,
    event_name: str,
    before: Milestone | Track,
    after: Milestone | Track,
    changed: tuple[str, ...],
    sequence: int,
    now: datetime,
) -> Envelope:
    """Return the one firehose row an edit appends.

    The status is stated as both from and to, so the keyed patch a console
    builds from the row moves the revision and leaves the status where it is.
    """
    urn = request.urn
    event_id = f"evt-{uuid.uuid4().hex}"
    return Envelope(
        id=event_id,
        kind=StoreKind.EVENT,
        scope_id=str(urn),
        created_at=now,
        summary=f"{event_name} {urn.entity_key} {', '.join(changed)}",
        payload={
            "schema_version": MAINTENANCE_EVENT_SCHEMA_VERSION,
            "name": event_name,
            "event_id": event_id,
            "occurred_at": now.isoformat(),
            "workspace_ref": urn.workspace_key,
            "project_ref": urn.project_key,
            "entity_ref": str(urn),
            "from_status": before.status.value,
            "to_status": after.status.value,
            "revision_before": before.revision,
            "revision_after": after.revision,
            "changed_fields": list(changed),
            "actor_ref": request.actor,
            "idempotency_key": request.idempotency_key,
            "correlation_id": request.correlation_id,
            "canonical_sequence": sequence,
        },
    )


def commit_record_edit[RecordT: (Milestone, Track)](
    context: Epoch2RootContext,
    request: RecordEditRequest,
    *,
    method: str,
    model: type[RecordT],
    editable: frozenset[StrEnum],
    fields: Callable[[RecordT], dict[str, Any]],
    event_name: str,
    now: datetime,
) -> CommittedTransaction:
    """Commit new field values on one record whose status does not move.

    Args:
        context: The native context of the tree the request addresses.
        request: The validated request.
        method: The verb the client sent, named in a refusal.
        model: The record class the stored row validates through.
        editable: The statuses the record may be edited in.
        fields: The new field values, as stored JSON, given the record as held.
        event_name: The firehose name of the edit.
        now: When the edit happened, stamped as the record's ``updated_at``.

    Returns:
        The receipt beside the firehose row to publish; a retry of a committed
        request returns that commit's receipt and no row.

    Raises:
        TransactionRefusedError: The URN names another kind, the key committed
            other parameters, the record is absent, stale, unreadable or in a
            status the edit is closed in, or the edited record does not
            validate. Nothing was written.
    """
    urn = request.urn
    kind = EntityKind.MILESTONE if model is Milestone else EntityKind.TRACK
    if urn.kind is not kind:
        raise _kind_refusal(urn, method=method, kind=kind)
    with context.session([urn]) as session:
        replayed = _replayed_receipt(context, request=request)
        if replayed is not None:
            return CommittedTransaction(receipt=replayed, envelope=None, replayed=True)
        document = session.read_document()
        before = _held(document, request, model=model)
        if before.status not in editable:
            open_names = ", ".join(sorted(status.value for status in editable))
            raise TransactionRefusedError(
                code=TransactionRefusalCode.ILLEGAL_TRANSITION,
                detail=f"{method} edits a {kind.value} only while it is {open_names}, but "
                f"{urn.entity_key} is {before.status.value}",
                entity_ref=str(urn),
                remediation=f"Edit a {kind.value} before work is planned against it.",
                revision=before.revision,
            )
        changes = fields(before)
        try:
            after = model.model_validate(
                {
                    **before.model_dump(mode="json"),
                    **changes,
                    "revision": before.revision + 1,
                    "updated_at": now,
                }
            )
        except ValidationError as error:
            raise TransactionRefusedError(
                code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
                detail=f"the edited {kind.value} does not validate: {error.errors()[0]['msg']}",
                entity_ref=str(urn),
                remediation="Correct the edited fields and retry.",
                revision=before.revision,
            ) from error
        allocator = CanonicalSequenceAllocator.recover(
            workspace_key=urn.workspace_key, high_water_mark=_high_water_mark(document)
        )
        with allocator.transaction() as sequences:
            sequence = sequences.allocate()
            new_document = copy.deepcopy(document)
            new_document[ENTITY_COLLECTIONS[kind].value][before.key] = after.model_dump(mode="json")
            new_document[CANONICAL_SEQUENCE_KEY] = sequence
            plan = _CommitPlan(
                record=before,
                successor=after,
                event_name=event_name,
                sequence=sequence,
                document=document,
                new_document=new_document,
                envelope=_edit_envelope(
                    request,
                    event_name=event_name,
                    before=before,
                    after=after,
                    changed=tuple(changes),
                    sequence=sequence,
                    now=now,
                ),
                compaction=None,
            )
            receipt = _persist(
                context=context, session=session, plan=plan, request=request, now=now
            )
    logger.info(
        f"commit_record_edit event={event_name} key={urn.entity_key!r} "
        f"revision={receipt.revision_after} sequence={receipt.canonical_sequence}"
    )
    return CommittedTransaction(receipt=receipt, envelope=plan.envelope)


async def run_record_edit(
    ctx: MethodContext,
    params: dict[str, Any],
    authority: RootAuthority,
    *,
    method: str,
    request_model: type[RecordEditRequest],
    commit: Callable[[Epoch2RootContext, Any, datetime], CommittedTransaction],
) -> dict[str, Any]:
    """Parse, commit and answer one same-status field edit.

    Args:
        ctx: Server context, which owns the per-root native contexts and the bus.
        params: The request parameters, less the routing key the fence consumed.
        authority: The epoch-2 answer the fence resolved for the tree.
        method: The verb the client sent.
        request_model: The strict parameter model of the verb.
        commit: The verb's commit, given the context, the parsed request and the time.

    Returns:
        The machine envelope, as a JSON-mode mapping.
    """
    try:
        request = request_model.model_validate(
            {key: value for key, value in params.items() if key != REPO_ROOT_PARAM}
        )
    except ValidationError as error:
        return schema_refusal(error, params=params, operation=method).model_dump(mode="json")
    context = ctx.native_root_context(authority.root)
    try:
        committed = await asyncio.to_thread(commit, context, request, datetime.now(UTC))
    except TransactionRefusedError as refusal:
        logger.info(f"run_record_edit refused method={method} code={refusal.code.value}")
        return refused_envelope(refusal, operation=method).model_dump(mode="json")
    degraded = committed.envelope is not None and not publish_projection(
        ctx.bus, committed.envelope
    )
    return accepted_envelope(
        committed.receipt, operation=method, warnings=(PROJECTION_DEGRADED,) if degraded else ()
    ).model_dump(mode="json")


def edit_milestone(
    context: Epoch2RootContext, request: MilestoneEditRequest, now: datetime
) -> CommittedTransaction:
    """Commit a PLANNED Milestone's new contract fields, or refuse having written nothing.

    Raises:
        TransactionRefusedError: As :func:`commit_record_edit` raises it.
    """
    return commit_record_edit(
        context,
        request,
        method=MILESTONE_EDIT,
        model=Milestone,
        editable=frozenset({MilestoneStatus.PLANNED}),
        fields=lambda _milestone: request.edits(),
        event_name=EDITED_EVENT,
        now=now,
    )


@native_mutator(MILESTONE_EDIT)
async def _milestone_edit(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Rewrite the contract of one PLANNED Milestone; its status does not move."""
    return await run_record_edit(
        ctx,
        params,
        authority,
        method=MILESTONE_EDIT,
        request_model=MilestoneEditRequest,
        commit=edit_milestone,
    )


# ---- Cancel with its work ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Moved:
    """One record a cancel moves beside the Milestone.

    Attributes:
        entity: The lifecycle machine the record is governed by.
        collection: The collection it is stored under.
        record: The record the move produced.
    """

    entity: LifecycleEntity
    collection: Epoch2Collection
    record: LifecycleRecord


def _native[RecordT: (DeliveryBatch, Task)](
    document: dict[str, Any], collection: Epoch2Collection, model: type[RecordT]
) -> list[RecordT]:
    """Return the native records of *collection*, passing over imported epoch-1 rows.

    An imported backlog row is kept as a wrapper around its epoch-1 record and
    does not validate as a native record; it names no native Milestone, so
    no cancel owns it.
    """
    records: list[RecordT] = []
    for row in document_rows(document, collection).values():
        try:
            records.append(model.model_validate(row))
        except ValidationError:
            continue
    return records


def _aimed_branch(context: Epoch2RootContext, batch: DeliveryBatch) -> str:
    """Return the branch the repository stands on, which a never-activated Batch records.

    A Batch may lack a target branch only while PLANNED, and its cancel edge
    requires one, so the branch the tree's checkout stands on -- the branch
    its work would have been aimed at -- is recorded.

    Raises:
        TransactionRefusedError: The checkout stands on no branch git can name.
    """
    done = subprocess.run(
        [
            "git",
            "-C",
            str(context.identity.tree_root.parent),
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
        ],
        capture_output=True,
        text=True,
        check=False,
        **no_window_kwargs(),
    )
    branch = done.stdout.strip()
    if done.returncode != 0 or not branch:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.TRANSITION_GUARD_FAILED,
            detail=f"{batch.key} was never aimed at a branch and the checkout stands on none",
            entity_ref=str(batch.urn),
            guard=TransitionGuard.TARGET_BRANCH_PINNED.value,
            remediation="Check out the branch the Batch's work was aimed at and retry.",
        )
    return branch


def _move(
    document: dict[str, Any],
    *,
    document_path: Path,
    milestone: Milestone,
    record: DeliveryBatch | Task,
    target: LifecycleStatus,
    updates: dict[str, Any],
    now: datetime,
) -> LifecycleRecord:
    """Return *record* moved to *target* by the registry's own edge.

    Raises:
        TransactionRefusedError: The registry denies the edge; the refusal
            names the record that blocks the cancel.
    """
    outcome = apply_transition(
        record,
        to=target,
        at=now,
        ctx=GuardContext(
            unmet=locked_unmet(document, document_path=document_path, record=record, target=target)
        ),
        updates=updates,
    )
    if isinstance(outcome, TransitionDenied):
        raise TransactionRefusedError(
            code=_STRUCTURAL_DENIALS.get(
                outcome.code, TransactionRefusalCode.TRANSITION_GUARD_FAILED
            ),
            detail=f"cancelling {milestone.key} moves {record.key} to {target!s}, but "
            f"{outcome.message}",
            entity_ref=str(record.urn),
            guard=outcome.guard.value if outcome.guard is not None else outcome.code.value,
            remediation=f"Settle {record.key} first, then cancel {milestone.key} again.",
            revision=milestone.revision,
        )
    return outcome.record


def _names(ref: QualifiedUrn | None, milestone: Milestone) -> bool:
    """Return whether *ref* addresses *milestone*."""
    return ref is not None and ref.kind is EntityKind.MILESTONE and ref.entity_key == milestone.key


def _cascade(
    context: Epoch2RootContext,
    document: dict[str, Any],
    *,
    document_path: Path,
    milestone: Milestone,
    now: datetime,
) -> tuple[tuple[_Moved, ...], tuple[QualifiedUrn, ...]]:
    """Return every Batch and Task a cancel of *milestone* moves with it, and the live Runs.

    A live Run is the active Run of a cancelled Task that the document still
    holds in a status it leaves; a Run that ended is compacted out of it.

    Raises:
        TransactionRefusedError: An owned record has no edge to where the
            cancel takes it, or a PLANNED Batch has no branch to record.
    """
    owned = [
        batch
        for batch in _native(document, Epoch2Collection.BATCH, DeliveryBatch)
        if _names(batch.milestone_ref, milestone) and batch.status in _CASCADED_BATCHES
    ]
    moved: list[_Moved] = []
    branch: str | None = None
    for batch in owned:
        updates: dict[str, Any] = {}
        if batch.target_branch is None:
            branch = branch or _aimed_branch(context, batch)
            updates["target_branch"] = branch
        successor = _move(
            document,
            document_path=document_path,
            milestone=milestone,
            record=batch,
            target=BatchStatus.CANCELLED,
            updates=updates,
            now=now,
        )
        moved.append(_Moved(LifecycleEntity.DELIVERY_BATCH, Epoch2Collection.BATCH, successor))
    cancelled_batches = {batch.key for batch in owned}
    runs = document_rows(document, Epoch2Collection.RUN)
    live: list[QualifiedUrn] = []
    for task in _native(document, Epoch2Collection.TASK, Task):
        in_batch = task.batch_ref is not None and task.batch_ref.entity_key in cancelled_batches
        if in_batch and task.status in _OPEN_TASKS:
            target = TaskStatus.CANCELLED
        elif task.status in _BACKLOG_TASKS and _names(task.due_scope, milestone):
            target = TaskStatus.DROPPED
        else:
            continue
        successor = _move(
            document,
            document_path=document_path,
            milestone=milestone,
            record=task,
            target=target,
            updates={},
            now=now,
        )
        moved.append(_Moved(LifecycleEntity.TASK, Epoch2Collection.TASK, successor))
        run = task.active_run_ref
        row = None if run is None else runs.get(run.entity_key)
        if run is not None and row is not None and row.get("status") not in _ENDED_RUNS:
            live.append(run)
    return tuple(moved), tuple(live)


def cancel_with_cascade(
    *, context: Epoch2RootContext, request: TransitionRequest, now: datetime
) -> CommittedTransaction:
    """Cancel one Milestone and its open work in one commit, or refuse having written nothing.

    The Milestone's own edge is the registry's, judged exactly as
    :func:`~eawf.runtime.daemon.epoch2_transaction.run_transaction` judges
    it; every cascaded record is moved by its own registered edge too, and
    written into the same document, so the one WAL record and the one
    firehose row of the cancel cover all of them. Each record the cancel
    made terminal is compacted into its ledger once the commit is durable.

    Args:
        context: The native context of the tree the request addresses.
        request: The validated transition request, whose target is CANCELLED.
        now: When the cancel happened.

    Returns:
        The receipt of the Milestone's move beside the firehose row to publish
        and the live Runs of the Tasks it cancelled; a retry of a committed
        request returns that receipt, no row and no Run.

    Raises:
        TransactionRefusedError: The key committed other parameters, the
            Milestone is absent or stale, the registry denies its edge, or a
            cascaded record has no edge to where the cancel takes it.
        NativeAuthorityRequiredError: The tree left epoch 2.
        LockTimeout: The document lock stayed held past the lock timeout.
    """
    with context.session([request.urn]) as session:
        replayed = _replayed_receipt(context, request=request)
        if replayed is not None:
            return CommittedTransaction(receipt=replayed, envelope=None, replayed=True)
        document = session.read_document()
        document_path = session.document_path
        record = _reread_record(
            document=document,
            collection=Epoch2Collection.MILESTONE,
            entity=LifecycleEntity.MILESTONE,
            request=request,
        )
        if not isinstance(record, Milestone):
            raise TypeError(f"{request.urn} read back as a {type(record).__name__}")
        target = MilestoneStatus.CANCELLED
        outcome = apply_transition(
            record,
            to=target,
            at=now,
            ctx=GuardContext(
                unmet=locked_unmet(
                    document, document_path=document_path, record=record, target=target
                ),
                observations=frozenset(request.observations),
            ),
            updates=edge_updates(
                record, target=target, updates=request.updates, actor=request.actor, now=now
            ),
        )
        if isinstance(outcome, TransitionDenied):
            raise _denied(outcome, request=request, revision=record.revision)
        moved, live = _cascade(
            context, document, document_path=document_path, milestone=record, now=now
        )
        allocator = CanonicalSequenceAllocator.recover(
            workspace_key=request.urn.workspace_key, high_water_mark=_high_water_mark(document)
        )
        with allocator.transaction() as sequences:
            plan = _plan_commit(
                document=document,
                collection=Epoch2Collection.MILESTONE,
                entity=LifecycleEntity.MILESTONE,
                record=record,
                successor=outcome.record,
                event_name=outcome.event.name,
                sequence=sequences.allocate(),
                request=request,
                now=now,
            )
            # The plan is local until persisted, so the cascaded rows join the one
            # document it writes and the leak scrub sees them beside the Milestone.
            for item in moved:
                rows = plan.new_document.setdefault(item.collection.value, {})
                rows[item.record.key] = item.record.model_dump(mode="json")
            receipt = _persist(
                context=context, session=session, plan=plan, request=request, now=now
            )
        for item in moved:
            line = _terminal_ledger_record(
                entity=item.entity, collection=item.collection, successor=item.record, now=now
            )
            if line is not None:
                _compact(context, document_path=document_path, record=line)
    logger.info(
        f"cancel_with_cascade key={request.urn.entity_key!r} moved={len(moved)} "
        f"sequence={receipt.canonical_sequence}"
    )
    return CommittedTransaction(receipt=receipt, envelope=plan.envelope, stopped_runs=live)


# ---- Purge ------------------------------------------------------------------


def _pushed_ref(repository: Path, generation: Path, key: str) -> str | None:
    """Return a remote-tracking ref whose tree mentions Milestone *key*, or ``None``.

    Every remote-tracking ref is searched rather than the branch's upstream
    alone: a Milestone any remote holds is held by someone else, whichever
    branch carried it there. A directory that is no git checkout has no
    remote, so nothing in it was pushed.

    Raises:
        TransactionRefusedError: git could not search a ref, so whether the
            Milestone was pushed is unknown.
    """
    listed = subprocess.run(
        ["git", "-C", str(repository), "for-each-ref", "--format=%(refname)", "refs/remotes"],
        capture_output=True,
        text=True,
        check=False,
        **no_window_kwargs(),
    )
    if listed.returncode != 0:
        return None
    relative = generation.relative_to(repository).as_posix()
    needle = f'/milestone/{key}"'
    search = ("git", "-C", str(repository), "grep", "--quiet", "-F", "-e", needle)
    for ref in listed.stdout.split():
        found = subprocess.run(
            [*search, ref, "--", relative],
            capture_output=True,
            text=True,
            check=False,
            **no_window_kwargs(),
        )
        if found.returncode == 0:
            return ref
        if found.returncode != 1:
            raise TransactionRefusedError(
                code=TransactionRefusalCode.TRANSITION_GUARD_FAILED,
                detail=f"git could not search {ref} for {key}: {found.stderr.strip()[:200]}",
                entity_ref=key,
                guard=UNPUSHED_GUARD,
                remediation="Repair the checkout's remote refs and retry.",
            )
    return None


def _cancelled_milestone(
    document: dict[str, Any], ledger: Path, request: MilestonePurgeRequest
) -> Milestone:
    """Return the CANCELLED Milestone the purge addresses, from the document or its ledger.

    Raises:
        TransactionRefusedError: The tree holds no such Milestone, its row does
            not validate, it is at another revision, or it is not CANCELLED.
    """
    urn = request.urn
    row = document_rows(document, Epoch2Collection.MILESTONE).get(urn.entity_key)
    if row is None:
        row = latest_rows(ledger).get(urn.entity_key)
    if row is None:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.IDENTITY_NOT_FOUND,
            detail=f"the tree holds no milestone keyed {urn.entity_key!r}",
            entity_ref=str(urn),
            remediation="Name a Milestone this tree holds.",
        )
    try:
        milestone = Milestone.model_validate(row)
    except ValidationError as error:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
            detail="the stored milestone row does not validate through its model",
            entity_ref=str(urn),
            remediation="Repair the stored row before purging it.",
        ) from error
    if milestone.revision != request.expected_revision:
        raise _stale(urn, stored=milestone.revision, expected=request.expected_revision)
    if milestone.status is not MilestoneStatus.CANCELLED:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.ILLEGAL_TRANSITION,
            detail=f"{MILESTONE_PURGE} removes only a CANCELLED milestone, but "
            f"{urn.entity_key} is {milestone.status.value}",
            entity_ref=str(urn),
            remediation="Cancel the Milestone first, then purge it.",
            revision=milestone.revision,
        )
    return milestone


#: The Task statuses a purge takes out with their Milestone: work that never landed.
_PURGEABLE_TASKS: Final = frozenset({TaskStatus.CANCELLED, TaskStatus.DROPPED})


def _standing[RecordT: (DeliveryBatch, Task)](
    document: dict[str, Any], ledger: Path, collection: Epoch2Collection, model: type[RecordT]
) -> list[RecordT]:
    """Return the native records of *collection* the document or its ledger holds.

    A document row wins over a ledger line for the same key, because a
    compaction the daemon deferred leaves the record canonical in the document.
    A row that does not validate is an imported epoch-1 record, which names no
    native Milestone.
    """
    rows = {**latest_rows(ledger), **document_rows(document, collection)}
    records: list[RecordT] = []
    for row in rows.values():
        try:
            records.append(model.model_validate(row))
        except ValidationError:
            continue
    return records


def _owned_keys(
    document: dict[str, Any], *, ledgers: Mapping[Epoch2Collection, Path], milestone: Milestone
) -> dict[Epoch2Collection, frozenset[str]]:
    """Return the keys a purge of *milestone* removes, by collection.

    The Batches filed under the Milestone and the Tasks filed under those
    Batches or due on the Milestone go with it, so no surviving row names a
    record no tier holds.

    Raises:
        TransactionRefusedError: An owned Batch is not CANCELLED or an owned
            Task is neither CANCELLED nor DROPPED, so it carries history a
            purge must not erase.
    """
    batch_ledger, task_ledger = ledgers[Epoch2Collection.BATCH], ledgers[Epoch2Collection.TASK]
    batches = [
        batch
        for batch in _standing(document, batch_ledger, Epoch2Collection.BATCH, DeliveryBatch)
        if _names(batch.milestone_ref, milestone)
    ]
    batch_keys = frozenset(batch.key for batch in batches)
    tasks = [
        task
        for task in _standing(document, task_ledger, Epoch2Collection.TASK, Task)
        if (task.batch_ref is not None and task.batch_ref.entity_key in batch_keys)
        or _names(task.due_scope, milestone)
    ]
    blockers = sorted(
        [
            f"{batch.key} {batch.status.value}"
            for batch in batches
            if batch.status is not BatchStatus.CANCELLED
        ]
        + [
            f"{task.key} {task.status.value}"
            for task in tasks
            if task.status not in _PURGEABLE_TASKS
        ]
    )
    if blockers:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.ILLEGAL_TRANSITION,
            detail=f"{milestone.key} owns records a purge would erase history from: "
            f"{', '.join(blockers)}",
            entity_ref=str(milestone.urn),
            remediation="Purge only a Milestone whose Batches are CANCELLED and whose Tasks "
            "are CANCELLED or DROPPED.",
            revision=milestone.revision,
        )
    return {
        Epoch2Collection.MILESTONE: frozenset({milestone.key}),
        Epoch2Collection.BATCH: batch_keys,
        Epoch2Collection.TASK: frozenset(task.key for task in tasks),
    }


#: The URN kind each purged collection's records are named under.
_PURGED_KINDS: Final = {
    Epoch2Collection.MILESTONE: EntityKind.MILESTONE,
    Epoch2Collection.BATCH: EntityKind.BATCH,
    Epoch2Collection.TASK: EntityKind.TASK,
}


def _refuse_dangling(
    document: dict[str, Any],
    *,
    document_path: Path,
    milestone: Milestone,
    owned: Mapping[Epoch2Collection, frozenset[str]],
) -> None:
    """Refuse the purge while a row it keeps names a record it would remove.

    Every collection's document rows, and the lines of every collection kept
    at the ledger tier, are searched for the purged records' URNs, so a
    reference is found whichever field of whichever record holds it.

    Raises:
        TransactionRefusedError: A kept row names a purged record.
    """
    needles = tuple(
        f'/{_PURGED_KINDS[collection].value}/{key}"'
        for collection in _PURGED
        for key in owned[collection]
    )
    naming: set[str] = set()
    for collection in Epoch2Collection:
        purged = owned.get(collection, frozenset())
        rows = [
            *document_rows(document, collection).items(),
            *(
                (line.record_key, line.payload)
                for line in (
                    read_ledger_records(ledger_path(document_path, collection))
                    if collection in LEDGER_COLLECTIONS
                    else ()
                )
            ),
        ]
        for key, row in rows:
            if key in purged:
                continue
            text = orjson.dumps(row).decode()
            if any(needle in text for needle in needles):
                naming.add(f"{collection.value}/{key}")
    if naming:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.ILLEGAL_TRANSITION,
            detail=f"{milestone.key} is named by records a purge would leave dangling: "
            f"{', '.join(sorted(naming))}",
            entity_ref=str(milestone.urn),
            remediation="Keep the cancelled Milestone; what names its work is history.",
            revision=milestone.revision,
        )


def _ledger_cut(ledger: Path, keys: frozenset[str]) -> tuple[list[str], int, bool]:
    """Return the live lines that stay, how many go, and whether the snapshot seals one.

    Only the live file is ever rewritten; a line the sealed snapshot holds is
    reported so the purge refuses rather than leaving it standing.
    """
    live = split_ledger_lines(ledger.read_bytes() if ledger.exists() else b"")
    kept = [line for line in live if LedgerRecord.model_validate_json(line).record_key not in keys]
    removed = len(live) - len(kept)
    held = sum(1 for line in read_ledger_records(ledger) if line.record_key in keys)
    return kept, removed, held > removed


def _purge_envelope(
    request: MilestonePurgeRequest,
    *,
    milestone: Milestone,
    owned: Mapping[Epoch2Collection, frozenset[str]],
    removed: int,
    sequence: int,
    now: datetime,
) -> Envelope:
    """Return the one firehose row a purge appends.

    The row carries no target status or revision after, because no record
    stands after it for a keyed patch to move.
    """
    urn = request.urn
    event_id = f"evt-{uuid.uuid4().hex}"
    return Envelope(
        id=event_id,
        kind=StoreKind.EVENT,
        scope_id=str(urn),
        created_at=now,
        summary=f"{PURGED_EVENT} {urn.entity_key}",
        payload={
            "schema_version": MAINTENANCE_EVENT_SCHEMA_VERSION,
            "name": PURGED_EVENT,
            "event_id": event_id,
            "occurred_at": now.isoformat(),
            "workspace_ref": urn.workspace_key,
            "project_ref": urn.project_key,
            "entity_ref": str(urn),
            "from_status": milestone.status.value,
            "revision_before": milestone.revision,
            "ledger_lines_removed": removed,
            "purged_batch_keys": sorted(owned[Epoch2Collection.BATCH]),
            "purged_task_keys": sorted(owned[Epoch2Collection.TASK]),
            "actor_ref": request.actor,
            "idempotency_key": request.idempotency_key,
            "correlation_id": request.correlation_id,
            "canonical_sequence": sequence,
        },
    )


def purge_milestone(
    context: Epoch2RootContext, request: MilestonePurgeRequest, *, now: datetime
) -> CommittedTransaction:
    """Remove one CANCELLED Milestone no pushed commit carries, with its cancelled work.

    The document write commits through the transaction's own write step: a
    WAL intent, the document, the receipt, the firehose row. The receipt
    reports the Milestone one revision on, as a tombstone, so a retry under a
    new key at the old revision is told the record is gone rather than stale.
    The ledger lines are taken out of the live files after the commit is
    durable, because no transaction step removes a ledger line; a crash in
    between leaves cancelled lines standing that still name each other, so
    the tree stays valid. The derived offset index is left to the next
    compaction, which rebuilds it whole.

    Args:
        context: The native context of the tree the request addresses.
        request: The validated request.
        now: When the purge happened.

    Returns:
        The receipt beside the firehose row to publish; a retry of a committed
        request returns that receipt and no row.

    Raises:
        TransactionRefusedError: The URN names another kind, the key committed
            other parameters, the Milestone is absent, stale or not CANCELLED,
            an owned Batch or Task is in a status a purge keeps, a kept row
            names a record the purge would remove, a
            remote-tracking ref or a ledger's sealed snapshot already holds
            it, or git could not answer. Nothing was written.
    """
    urn = request.urn
    if urn.kind is not EntityKind.MILESTONE:
        raise _kind_refusal(urn, method=MILESTONE_PURGE, kind=EntityKind.MILESTONE)
    key = urn.entity_key
    with context.session([urn]) as session:
        replayed = _replayed_receipt(context, request=request)
        if replayed is not None:
            return CommittedTransaction(receipt=replayed, envelope=None, replayed=True)
        document = session.read_document()
        document_path = session.document_path
        ledgers = {collection: session.ledger_path(collection) for collection in _PURGED}
        milestone = _cancelled_milestone(document, ledgers[Epoch2Collection.MILESTONE], request)
        owned = _owned_keys(document, ledgers=ledgers, milestone=milestone)
        _refuse_dangling(document, document_path=document_path, milestone=milestone, owned=owned)
        cuts = {
            collection: _ledger_cut(ledgers[collection], owned[collection])
            for collection in _PURGED
        }
        repository = context.identity.tree_root.parent
        pushed = _pushed_ref(repository, document_path.parent, key)
        if pushed is not None or any(sealed for _kept, _removed, sealed in cuts.values()):
            where = f"{pushed} already carries it" if pushed else "a ledger snapshot seals it"
            raise TransactionRefusedError(
                code=TransactionRefusalCode.TRANSITION_GUARD_FAILED,
                detail=f"{key} is history someone else may hold: {where}",
                entity_ref=str(urn),
                guard=UNPUSHED_GUARD,
                remediation="Keep the cancelled Milestone; a pushed record is never removed.",
                revision=milestone.revision,
            )
        removed = sum(lines for _kept, lines, _sealed in cuts.values())
        allocator = CanonicalSequenceAllocator.recover(
            workspace_key=urn.workspace_key, high_water_mark=_high_water_mark(document)
        )
        with allocator.transaction() as sequences:
            sequence = sequences.allocate()
            new_document = copy.deepcopy(document)
            for collection in _PURGED:
                rows = new_document.get(collection.value, {})
                for owned_key in owned[collection]:
                    rows.pop(owned_key, None)
            new_document[CANONICAL_SEQUENCE_KEY] = sequence
            plan = _CommitPlan(
                record=milestone,
                successor=milestone.model_copy(update={"revision": milestone.revision + 1}),
                event_name=PURGED_EVENT,
                sequence=sequence,
                document=document,
                new_document=new_document,
                envelope=_purge_envelope(
                    request,
                    milestone=milestone,
                    owned=owned,
                    removed=removed,
                    sequence=sequence,
                    now=now,
                ),
                compaction=None,
            )
            receipt = _persist(
                context=context, session=session, plan=plan, request=request, now=now
            )
        for collection, (kept, lines, _sealed) in cuts.items():
            if lines:
                content = "".join(f"{line}\n" for line in kept).encode("utf-8")
                _atomic_write(ledgers[collection], content)
    logger.info(f"purge_milestone key={key!r} lines={removed} sequence={sequence}")
    return CommittedTransaction(receipt=receipt, envelope=plan.envelope)


@native_mutator(MILESTONE_PURGE)
async def _milestone_purge(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Remove one CANCELLED Milestone that no pushed commit carries."""
    try:
        request = MilestonePurgeRequest.model_validate(
            {key: value for key, value in params.items() if key != REPO_ROOT_PARAM}
        )
    except ValidationError as error:
        return schema_refusal(error, params=params, operation=MILESTONE_PURGE).model_dump(
            mode="json"
        )
    context = ctx.native_root_context(authority.root)
    try:
        committed = await asyncio.to_thread(
            purge_milestone, context, request, now=datetime.now(UTC)
        )
    except TransactionRefusedError as refusal:
        logger.info(f"_milestone_purge refused code={refusal.code.value}")
        return refused_envelope(refusal, operation=MILESTONE_PURGE).model_dump(mode="json")
    degraded = committed.envelope is not None and not publish_projection(
        ctx.bus, committed.envelope
    )
    return accepted_envelope(
        committed.receipt,
        operation=MILESTONE_PURGE,
        warnings=(PROJECTION_DEGRADED,) if degraded else (),
    ).model_dump(mode="json")


__all__ = [
    "EDITED_EVENT",
    "MILESTONE_EDIT",
    "MILESTONE_EDIT_FIELDS",
    "MILESTONE_PURGE",
    "PURGED_EVENT",
    "MilestoneEditRequest",
    "MilestonePurgeRequest",
    "RecordEditRequest",
    "cancel_with_cascade",
    "commit_record_edit",
    "edit_milestone",
    "purge_milestone",
    "run_record_edit",
]
