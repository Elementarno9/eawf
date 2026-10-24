"""Dropping a backlog row the epoch-2 cutover imported.

The cutover keeps an epoch-1 backlog row under its epoch-1 key, such as
``B012``, as a wrapper around the imported record rather than as a native
Task, so it has no URN and the native drop edge cannot address it. This
module retires one such row: it records the drop as a continuation event
beside the untouched imported record and files the row in the Task ledger
with status ``DROPPED``. The document then no longer holds the key, which
is what takes it out of the backlog; the imported record and the sealed
ledger snapshot are never rewritten.

The move is decided here under the root's lock and committed through
:func:`~eawf.runtime.daemon.epoch2_transaction.commit_row_write`, which
stays the only writer of the document and the ledger line.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from eawf.kernel.identity import EntityKind, format_qualified_urn
from eawf.kernel.identity.urn import URN_SCHEME
from eawf.kernel.migration.epoch2.continuation import (
    ROW_CONTINUATION_FIELD,
    ContinuationEvent,
    LegacyContinuationRefusedError,
    entity_ref,
    read_legacy_row,
)
from eawf.kernel.migration.epoch2.cutover import ROW_RECORDED_AT_FIELD, ROW_STATUS_FIELD
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.migration.epoch2.urn_rekey import spells_single_key
from eawf.kernel.state.epoch2.base import PrincipalKey, StrictPositiveInt
from eawf.kernel.state.epoch2.task import TaskStatus
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import LedgerRecord, effective_records, read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_recovery import (
    read_idempotency_receipt,
    record_idempotency_receipt,
)
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import (
    TransactionRefusalCode,
    TransactionRefusedError,
    _request_digest,
    commit_row_write,
)

#: The statuses the native drop edge leaves, which an imported row is held to too.
_DROPPABLE: Final = frozenset({TaskStatus.DRAFT.value, TaskStatus.DEFERRED.value})

#: The prefix of the name the backlog projection gives an imported Task row.
_LEGACY_TASK_PREFIX: Final = entity_ref(Epoch2Collection.TASK, "")

_Ref = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]


class LegacyTaskDropParams(BaseModel):
    """The strict parameters of a drop addressed to an imported backlog row.

    The wire shape is the native drop's, so ``task drop`` sends one request
    whichever kind of row it names; what differs is what each field may hold.

    Attributes:
        urn: The row's epoch-1 key, or the ``legacy:task/<key>`` name the
            backlog shows for it.
        expected_revision: The revision the caller read: one for the import
            plus one per continuation move since.
        idempotency_key: The client's name for this request.
        actor: Who asked, as an immutable qualified principal key.
        observations: Always empty; an imported row has no guard to observe.
        updates: Always empty; the imported record is never edited.
        reason_code: Why the row is dropped, in the operator's words.
        binding_refs: What the caller cites for the drop.
        correlation_id: The client's thread of related requests.
    """

    model_config = ConfigDict(extra="forbid")

    urn: _Ref
    expected_revision: StrictPositiveInt
    idempotency_key: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]
    actor: PrincipalKey
    observations: tuple[()] = ()
    updates: Annotated[dict[str, Any], Field(max_length=0)] = Field(default_factory=dict)
    reason_code: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
    binding_refs: tuple[_Ref, ...] = ()
    correlation_id: Annotated[str, StringConstraints(strict=True, max_length=128)] | None = None

    @property
    def key(self) -> str:
        """Return the row's key, whichever of its two names the caller used."""
        return self.urn.removeprefix(_LEGACY_TASK_PREFIX)


class LegacyDropReceipt(BaseModel):
    """What one committed legacy drop left behind.

    Attributes:
        entity_ref: The ``legacy:task/<key>`` name of the dropped row.
        revision_before: The row's revision before the drop.
        revision_after: Its revision after, one more.
        event_id: The firehose row the drop wrote.
        idempotency_key: The client's name for the request.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_ref: str
    revision_before: StrictPositiveInt
    revision_after: StrictPositiveInt
    event_id: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CommittedLegacyDrop:
    """A committed drop beside the firehose row to publish after the lock.

    Attributes:
        receipt: What the caller answers with.
        envelope: The firehose row the commit appended, or ``None`` on a
            retry answered from the receipt store, which publishes nothing.
    """

    receipt: LegacyDropReceipt
    envelope: Envelope | None


def names_legacy_row(urn: object) -> bool:
    """Return whether a drop's ``urn`` names an imported row rather than a native Task.

    A native Task is always addressed by a qualified URN; any
    other string is an epoch-1 key or the projection's name for one.
    """
    return isinstance(urn, str) and not urn.startswith(URN_SCHEME)


def _project_urn(context: Epoch2RootContext) -> str:
    """Return the URN of the one project the tree imported, which the drop locks.

    Raises:
        TransactionRefusedError: The selected document does not hold
            exactly one project row, so no scope can be named.
    """
    authority = context.require_selected_generation()
    assert authority.target is not None and authority.generation_id is not None
    document = read_document(
        authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    )
    projects = sorted(document_rows(document, Epoch2Collection.PROJECT))
    if len(projects) != 1:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
            detail=f"the selected document holds {len(projects)} project rows, not one",
            entity_ref=context.identity.tree_root.name,
            remediation="Drop imported rows only on a tree that imported one project.",
        )
    code = projects[0]
    return format_qualified_urn(
        workspace_key=code,
        project_key=code,
        repository_key=None,
        kind=EntityKind.PROJECT,
        entity_key=code,
        single_key=spells_single_key(document),
    )


def _replayed_drop(
    context: Epoch2RootContext, request: LegacyTaskDropParams, *, ref: str
) -> LegacyDropReceipt | None:
    """Return the receipt a drop under this idempotency key already earned, if any.

    Raises:
        TransactionRefusedError: The key committed other parameters
            (``idempotency_conflict``), or its stored receipt cannot be read
            (``schema_validation_failed``).
    """
    try:
        stored = read_idempotency_receipt(
            context, namespaced_key=context.idempotency_key(request.idempotency_key)
        )
        receipt = None if stored is None else LegacyDropReceipt.model_validate(stored.receipt)
    except ValueError as error:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
            detail=f"the receipt stored for idempotency key {request.idempotency_key!r} "
            "cannot be read, so whether this drop already ran is unknown",
            entity_ref=ref,
            remediation="Remove the unreadable receipt from the root local store and retry.",
        ) from error
    if stored is None or receipt is None:
        return None
    if stored.params_digest != _request_digest(request):
        raise TransactionRefusedError(
            code=TransactionRefusalCode.IDEMPOTENCY_CONFLICT,
            detail=f"idempotency key {request.idempotency_key!r} already committed a request "
            "with different parameters, so this one is not a retry of it",
            entity_ref=ref,
            remediation="Retry with the original parameters, or choose a new idempotency key.",
            revision=receipt.revision_after,
        )
    return receipt


def retire_legacy_task(
    context: Epoch2RootContext, request: LegacyTaskDropParams, *, method: str, now: datetime
) -> CommittedLegacyDrop:
    """Drop one imported backlog row and file it in the Task ledger.

    Args:
        context: The native context of the tree.
        request: The already-validated request.
        method: The verb the client sent, which the firehose row is named by.
        now: When the drop happens.

    Returns:
        The receipt beside the firehose row the drop wrote. A retry of a drop
        this root already committed returns that receipt and no row.

    Raises:
        TransactionRefusedError: Nothing was written, because the
            idempotency key committed other parameters
            (``idempotency_conflict``), the key names
            no row (``identity_not_found``), a row the ledger already holds
            or one not in a droppable status (``legacy_edge_refused``), a row
            at another revision (``revision_conflict``), or a row that is not
            an imported Task or whose reason carries a leak shape
            (``schema_validation_failed``).
        NativeAuthorityRequiredError: The tree left epoch 2.
        LockTimeout: The document lock stayed held past the lock timeout.
    """
    key, ref = request.key, entity_ref(Epoch2Collection.TASK, request.key)
    with context.session([_project_urn(context)]) as session:
        replayed = _replayed_drop(context, request, ref=ref)
        if replayed is not None:
            return CommittedLegacyDrop(receipt=replayed, envelope=None)
        row = document_rows(session.read_document(), Epoch2Collection.TASK).get(key)
        if row is None:
            lines = effective_records(
                read_ledger_records(session.ledger_path(Epoch2Collection.TASK))
            )
            filed = any(line.record_key == key for line in lines)
            raise TransactionRefusedError(
                code=TransactionRefusalCode.LEGACY_EDGE_REFUSED
                if filed
                else TransactionRefusalCode.IDENTITY_NOT_FOUND,
                detail=f"{ref} has already left the backlog"
                if filed
                else f"the tree holds no task row {key!r}",
                entity_ref=ref,
                remediation="Name an imported row the backlog still lists.",
            )
        try:
            legacy = read_legacy_row(Epoch2Collection.TASK, key, row)
        except LegacyContinuationRefusedError as error:
            raise TransactionRefusedError(
                code=TransactionRefusalCode(error.code.value),
                detail=error.detail,
                entity_ref=ref,
                guard=error.guard,
                remediation=error.remediation,
            ) from error
        revision = 1 + len(legacy.continuation)
        if legacy.status not in _DROPPABLE:
            raise TransactionRefusedError(
                code=TransactionRefusalCode.LEGACY_EDGE_REFUSED,
                detail=f"{ref} is {legacy.status}; only a DRAFT or DEFERRED row is dropped",
                entity_ref=ref,
                remediation="Leave a row that is already settled as it stands.",
                revision=revision,
            )
        if revision != request.expected_revision:
            raise TransactionRefusedError(
                code=TransactionRefusalCode.REVISION_CONFLICT,
                detail=f"{ref} is at revision {revision}, not {request.expected_revision}",
                entity_ref=ref,
                remediation="Read the row again and retry at its current revision.",
                revision=revision,
            )
        event = ContinuationEvent(
            from_status=legacy.status,
            to_status=TaskStatus.DROPPED.value,
            at=now,
            actor=request.actor,
            reason=request.reason_code,
            evidence_refs=request.binding_refs,
        )
        dropped = {
            **legacy.raw,
            ROW_STATUS_FIELD: TaskStatus.DROPPED.value,
            ROW_RECORDED_AT_FIELD: now.isoformat(),
            ROW_CONTINUATION_FIELD: [
                *(item.model_dump(mode="json") for item in legacy.continuation),
                event.model_dump(mode="json"),
            ],
        }
        envelope = commit_row_write(
            session,
            collection=Epoch2Collection.TASK,
            record_key=key,
            row=dropped,
            event_name=method,
            event_fields={
                "entity_ref": ref,
                "from_status": legacy.status,
                "to_status": TaskStatus.DROPPED.value,
                "revision_after": revision + 1,
                "actor": request.actor,
                "reason": request.reason_code,
                "idempotency_key": request.idempotency_key,
                "correlation_id": request.correlation_id,
            },
            compaction=LedgerRecord(
                collection=Epoch2Collection.TASK,
                record_key=key,
                status=TaskStatus.DROPPED.value,
                recorded_at=now,
                payload=dropped,
            ),
            now=now,
        )
        receipt = LegacyDropReceipt(
            entity_ref=ref,
            revision_before=revision,
            revision_after=revision + 1,
            event_id=envelope.id,
            idempotency_key=request.idempotency_key,
        )
        record_idempotency_receipt(
            context,
            namespaced_key=context.idempotency_key(request.idempotency_key),
            params_digest=_request_digest(request),
            receipt=receipt.model_dump(mode="json"),
            recorded_at=now,
        )
    return CommittedLegacyDrop(receipt=receipt, envelope=envelope)


__all__ = [
    "CommittedLegacyDrop",
    "LegacyDropReceipt",
    "LegacyTaskDropParams",
    "names_legacy_row",
    "retire_legacy_task",
]
