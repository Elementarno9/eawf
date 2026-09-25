"""Admitting a new epoch-2 record through the native transaction.

A lifecycle edge moves a record that exists. Nothing in the transition
registry makes one exist, so until this module a Track, a Milestone, a
Batch, a Task or a Run could reach an epoch-2 tree only by a plan apply or
by a direct write of the generation document -- and the second skips the
WAL, the receipt, the sequence and the event every other mutation carries.

A create walks the transaction's seven steps and commits through its
:func:`~eawf.runtime.daemon.epoch2_transaction._persist`, so the four
things a create writes are the four things a transition writes, in the same
order. What differs is step 4. There is no predecessor for the registry to
judge, so the successor is built from the entity's strict create document,
in the one status its machine admits a new record in, at revision one.

The compare-and-swap token of a create cannot be the record's own revision,
because the record does not exist yet. It is the tree's committed
``canonical_sequence`` the caller last read -- the cursor a projection read
reports -- so a create decided against a tree that has since moved is
refused rather than landed on top of it. Three more things refuse a create
with nothing written: a key the document or the collection's ledger
already holds, since a public key is never reused; a parent that is not a
live document row, since a record placed under nothing cannot be reached;
and free text carrying a leak shape, refused by the scrub the commit shares
with every other writer.

A retry is answered exactly as a transition's is: a key that already
committed these parameters returns its original receipt and no envelope,
having written nothing, and a key that committed different parameters is
refused.
"""

from __future__ import annotations

import copy
import logging
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.state.canonical_sequence import CanonicalSequenceAllocator
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.epoch2.base import Epoch2Model, PrincipalKey, StrictNonNegativeInt
from eawf.kernel.state.epoch2.batch import BatchCreateSpec, BatchStatus
from eawf.kernel.state.epoch2.milestone import MilestoneCreateSpec, MilestoneStatus
from eawf.kernel.state.epoch2.run import RunCreateSpec, RunStatus
from eawf.kernel.state.epoch2.task import TaskCreateSpec, TaskStatus
from eawf.kernel.state.epoch2.track import TrackCreateSpec, TrackStatus
from eawf.kernel.state.epoch2.transitions import LifecycleEntity, LifecycleStatus
from eawf.kernel.state.epoch2.urns import AnyEntityUrn
from eawf.kernel.state.epoch2.values import EntityOrigin
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import LedgerError, read_ledger_records
from eawf.kernel.store.tiers import ENTITY_COLLECTIONS, Epoch2Collection, StorageTier, tier_for
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.epoch2_transaction import (
    CANONICAL_SEQUENCE_KEY,
    LIFECYCLE_ENTITIES,
    RECORD_CLASSES,
    CommittedTransaction,
    TransactionRefusalCode,
    TransactionRefusedError,
    _CommitPlan,
    _entity_for,
    _high_water_mark,
    _persist,
    _replayed_receipt,
)
from eawf.workflow.lifecycle.epoch2 import LifecycleRecord

logger = logging.getLogger(__name__)


#: Version of the create-event payload a firehose row carries.
CREATE_EVENT_SCHEMA_VERSION: Final = "1"

#: The first segment of a create event name. Admission is not a lifecycle
#: edge -- nothing precedes it -- and the ``domain`` vocabulary is closed
#: to registered edges, so a create names its event in a namespace of its
#: own rather than inventing an edge with no source status.
CREATE_EVENT_NAMESPACE: Final = "admission"

#: The status each machine admits a new record in. It is the one status of
#: the machine no edge leads into, except for a Task: a Task is born a
#: ``DRAFT`` backlog row, which a deferral can also return it to.
CREATE_STATUSES: Final[Mapping[LifecycleEntity, LifecycleStatus]] = {
    LifecycleEntity.TRACK: TrackStatus.ACTIVE,
    LifecycleEntity.MILESTONE: MilestoneStatus.PLANNED,
    LifecycleEntity.DELIVERY_BATCH: BatchStatus.PLANNED,
    LifecycleEntity.TASK: TaskStatus.DRAFT,
    LifecycleEntity.RUN: RunStatus.QUEUED,
}

#: The strict create document each machine's records are admitted from.
#: Status, identity, revision and every proof are absent from all five, so
#: a caller cannot create a record already past its first state.
CREATE_SPECS: Final[Mapping[LifecycleEntity, type[Epoch2Model]]] = {
    LifecycleEntity.TRACK: TrackCreateSpec,
    LifecycleEntity.MILESTONE: MilestoneCreateSpec,
    LifecycleEntity.DELIVERY_BATCH: BatchCreateSpec,
    LifecycleEntity.TASK: TaskCreateSpec,
    LifecycleEntity.RUN: RunCreateSpec,
}

#: The origin every record admitted here carries.
_NATIVE_ORIGIN: Final = EntityOrigin(kind="native", mapping_basis="native", confidence="exact")

#: The contract revision a created Task starts at. Only a Task's create
#: document omits a field its record requires, because no contract exists
#: yet for anything to have revised.
_FIRST_CONTRACT_REVISION: Final = 1

#: Every legal create event name: one per collection a machine governs.
CREATE_EVENT_NAMES: Final[frozenset[str]] = frozenset(
    f"{CREATE_EVENT_NAMESPACE}.{ENTITY_COLLECTIONS[kind].value}.created"
    for kind in LIFECYCLE_ENTITIES
)


class CreateRequest(BaseModel):
    """The strict parameters one native create is asked for.

    Attributes:
        urn: The record to admit. Its kind picks the machine and the create
            document, and its entity key must be the key the document names.
        expected_revision: The tree's committed ``canonical_sequence`` the
            caller read, ``0`` for a tree nothing has committed to yet. A
            tree that moved on since is a conflict, because the create was
            decided against a world that is no longer there.
        idempotency_key: The client's name for this request.
        actor: Who asked, as an immutable qualified principal key.
        spec: The entity's create document, validated through the strict
            model :data:`CREATE_SPECS` names for the URN's kind.
        correlation_id: The client's thread of related requests.
    """

    model_config = ConfigDict(extra="forbid")

    urn: AnyEntityUrn
    expected_revision: StrictNonNegativeInt
    idempotency_key: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]
    actor: PrincipalKey
    spec: dict[str, Any]
    correlation_id: Annotated[str, StringConstraints(strict=True, max_length=128)] | None = None


def run_create(
    *,
    context: Epoch2RootContext,
    request: CreateRequest,
    now: datetime,
) -> CommittedTransaction:
    """Admit one new record into the tree, or refuse it having written nothing.

    The record is built and validated before the locks are taken, because
    whether a create document describes a legal record does not depend on
    the tree; everything that does -- the cursor, the free key, the live
    parent -- is decided under them.

    Args:
        context: The native context of the tree the record is admitted to.
        request: The already-validated request parameters.
        now: When the record was created. Supplied by the caller so the
            stored record, the event and the WAL record all agree.

    Returns:
        The receipt of the committed create beside the firehose envelope
        the caller publishes. A retry of a create this root already
        committed returns that commit's receipt and no envelope, having
        written nothing.

    Raises:
        TransactionRefusedError: The request reuses an idempotency key for
            different parameters, names a kind with no lifecycle machine,
            carries a create document that does not validate, was decided
            against a cursor the tree has moved past, names a key already
            taken, names a parent that is not a live row, or carries free
            text with a leak shape. Nothing was written.
        NativeAuthorityRequiredError: The tree left epoch 2.
        MigrationDualAuthorityError: The tree's select is not whole.
        LockTimeout: A lock stayed held past the lock timeout.
    """
    entity = _entity_for(request.urn)
    collection = ENTITY_COLLECTIONS[request.urn.kind]
    spec = _create_spec(entity, request=request)
    record = _admitted_record(entity, spec=spec, request=request, now=now)
    with context.session([request.urn]) as session:
        replayed = _replayed_receipt(context, request=request)
        if replayed is not None:
            logger.info(
                f"epoch2 create replayed root={context.identity.root_id} "
                f"key={request.idempotency_key!r} sequence={replayed.canonical_sequence}"
            )
            return CommittedTransaction(receipt=replayed, envelope=None, replayed=True)
        document = session.read_document()
        _require_cursor(document, request=request)
        _require_key_free(session, document=document, collection=collection, request=request)
        _require_live_parents(document, spec=spec, request=request)
        allocator = CanonicalSequenceAllocator.recover(
            workspace_key=request.urn.workspace_key,
            high_water_mark=_high_water_mark(document),
        )
        with allocator.transaction() as sequences:
            sequence = sequences.allocate()
            new_document = copy.deepcopy(document)
            new_document.setdefault(collection.value, {})[record.key] = record.model_dump(
                mode="json"
            )
            new_document[CANONICAL_SEQUENCE_KEY] = sequence
            event_name = f"{CREATE_EVENT_NAMESPACE}.{collection.value}.created"
            plan = _CommitPlan(
                record=None,
                successor=record,
                event_name=event_name,
                sequence=sequence,
                document=document,
                new_document=new_document,
                envelope=_create_envelope(
                    request=request,
                    record=record,
                    event_name=event_name,
                    sequence=sequence,
                    now=now,
                ),
                compaction=None,
            )
            receipt = _persist(
                context=context, session=session, plan=plan, request=request, now=now
            )
            logger.info(
                f"epoch2 create committed root={context.identity.root_id} "
                f"event={receipt.event_name} sequence={receipt.canonical_sequence}"
            )
            return CommittedTransaction(receipt=receipt, envelope=plan.envelope)


def _create_spec(entity: LifecycleEntity, *, request: CreateRequest) -> Epoch2Model:
    """Return the request's create document, validated for *entity*.

    Raises:
        TransactionRefusedError: The document does not validate, or names a
            key other than the one the URN addresses. The detail names the
            offending fields and never repeats their values.
    """
    model = CREATE_SPECS[entity]
    try:
        spec = model.model_validate(request.spec)
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        raise TransactionRefusedError(
            code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
            detail=f"the {entity.value} create document does not validate; check "
            f"{', '.join(fields) or model.__name__}",
            entity_ref=str(request.urn),
            remediation="Correct the named create-document fields and retry.",
        ) from error
    key = getattr(spec, "key", None)
    if key != request.urn.entity_key:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
            detail=f"the create document is keyed {key!r} but the URN addresses "
            f"{request.urn.entity_key!r}",
            entity_ref=str(request.urn),
            remediation="Key the create document by the URN's own entity key.",
        )
    return spec


def _admitted_record(
    entity: LifecycleEntity, *, spec: Epoch2Model, request: CreateRequest, now: datetime
) -> LifecycleRecord:
    """Return the record *spec* is admitted as, at revision one.

    The identity head is the daemon's to fill: the uid is derived from the
    URN so a replayed create would mint the same one, and the status is
    the machine's own first state.

    Raises:
        TransactionRefusedError: The record model refuses the document the
            create spec admitted, which a spec narrower than its record
            makes possible for a cross-field rule.
    """
    payload: dict[str, Any] = {
        **spec.model_dump(mode="json"),
        "uid": str(uuid.uuid5(uuid.NAMESPACE_URL, str(request.urn))),
        "urn": str(request.urn),
        "origin": _NATIVE_ORIGIN.model_dump(mode="json"),
        "revision": 1,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "status": str(CREATE_STATUSES[entity]),
    }
    if entity is LifecycleEntity.TASK:
        payload["contract_revision"] = _FIRST_CONTRACT_REVISION
    try:
        return RECORD_CLASSES[entity].model_validate(payload)
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        raise TransactionRefusedError(
            code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
            detail=f"the created {entity.value} does not validate through its record model; "
            f"check {', '.join(fields) or entity.value}",
            entity_ref=str(request.urn),
            remediation="Correct the create document so the record it builds is legal.",
        ) from error


def _require_cursor(document: dict[str, Any], *, request: CreateRequest) -> None:
    """Refuse a create decided against a tree that has since moved.

    Raises:
        TransactionRefusedError: The committed sequence is not the one the
            caller read. The detail carries the current cursor, so the
            caller can re-read and retry without a second round trip.
    """
    cursor = _high_water_mark(document)
    if cursor == request.expected_revision:
        return
    raise TransactionRefusedError(
        code=TransactionRefusalCode.REVISION_CONFLICT,
        detail=f"the tree is at canonical sequence {cursor} but the create expects "
        f"{request.expected_revision}",
        entity_ref=str(request.urn),
        guard="tree_cursor_current",
        remediation="Re-read the tree and retry against its current canonical sequence.",
    )


def _require_key_free(
    session: RootSession,
    *,
    document: dict[str, Any],
    collection: Epoch2Collection,
    request: CreateRequest,
) -> None:
    """Refuse a create whose key the document or the ledger already holds.

    A terminal record is compacted out of the document, so the ledger is
    read as well: a key that retired there is still taken.

    Raises:
        TransactionRefusedError: The key is taken, or the ledger cannot be
            read, which leaves whether it is taken unknown.
    """
    key = request.urn.entity_key
    row = document_rows(document, collection).get(key)
    revision = row.get("revision") if isinstance(row, dict) else None
    taken = row is not None
    if not taken and tier_for(collection) is StorageTier.LEDGER:
        try:
            lines = read_ledger_records(session.ledger_path(collection))
        except (LedgerError, OSError, ValueError) as error:
            raise TransactionRefusedError(
                code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
                detail=f"the {collection.value} ledger cannot be read, so whether "
                f"{key!r} is free is unknown",
                entity_ref=str(request.urn),
                remediation="Repair the collection ledger before creating under it.",
            ) from error
        taken = any(line.record_key == key for line in lines)
    if not taken:
        return
    raise TransactionRefusedError(
        code=TransactionRefusalCode.REVISION_CONFLICT,
        detail=f"the tree already holds a {collection.value} keyed {key!r}",
        entity_ref=str(request.urn),
        guard="record_key_free",
        remediation="Create the record under a key the tree has never held.",
        revision=revision if isinstance(revision, int) and revision >= 1 else None,
    )


def _parent_refs(spec: Epoch2Model) -> tuple[QualifiedUrn, ...]:
    """Return the records *spec* is placed under, among the driven machines.

    A Run's scope names its subject through a field spelled after the scope
    kind; only a subject one of the five machines governs is returned,
    because no other kind has a document row this tree admits.
    """
    if isinstance(spec, MilestoneCreateSpec):
        refs: tuple[QualifiedUrn, ...] = (spec.primary_track_ref,)
    elif isinstance(spec, BatchCreateSpec):
        refs = (spec.milestone_ref,)
    elif isinstance(spec, RunCreateSpec):
        refs = (getattr(spec.scope, f"{spec.scope.scope_kind}_ref"),)
    else:
        refs = ()
    return tuple(ref for ref in refs if ref.kind in LIFECYCLE_ENTITIES)


def _require_live_parents(
    document: dict[str, Any], *, spec: Epoch2Model, request: CreateRequest
) -> None:
    """Refuse a create placed under a record the document does not hold.

    Only the document is read. A parent compacted into its ledger reached a
    terminal state, and nothing new is placed under finished work.

    Raises:
        TransactionRefusedError: A parent is not a row of the document.
    """
    for parent in _parent_refs(spec):
        if parent.entity_key in document_rows(document, ENTITY_COLLECTIONS[parent.kind]):
            continue
        raise TransactionRefusedError(
            code=TransactionRefusalCode.IDENTITY_NOT_FOUND,
            detail=f"the document holds no live {parent.kind.value} keyed "
            f"{parent.entity_key!r} to place the record under",
            entity_ref=str(request.urn),
            guard="parent_record_live",
            remediation=f"Create the {parent.kind.value} before placing records under it.",
        )


def _create_envelope(
    *,
    request: CreateRequest,
    record: LifecycleRecord,
    event_name: str,
    sequence: int,
    now: datetime,
) -> Envelope:
    """Return the one firehose row a create appends.

    The row carries the same subject, status and revision fields a
    transition row does, so a projection patches the new record in from
    the event alone.
    """
    urn = request.urn
    event_id = f"evt-{uuid.uuid4().hex}"
    return Envelope(
        id=event_id,
        kind=StoreKind.EVENT,
        scope_id=str(urn),
        created_at=now,
        summary=f"{event_name} {urn.entity_key} {record.status!s}",
        payload={
            "schema_version": CREATE_EVENT_SCHEMA_VERSION,
            "name": event_name,
            "event_id": event_id,
            "occurred_at": now.isoformat(),
            "workspace_ref": urn.workspace_key,
            "project_ref": urn.project_key,
            "entity_ref": str(urn),
            "from_status": None,
            "to_status": str(record.status),
            "revision_before": None,
            "revision_after": record.revision,
            "actor_ref": request.actor,
            "idempotency_key": request.idempotency_key,
            "correlation_id": request.correlation_id,
            "canonical_sequence": sequence,
        },
    )


__all__ = [
    "CREATE_EVENT_NAMES",
    "CREATE_EVENT_NAMESPACE",
    "CREATE_EVENT_SCHEMA_VERSION",
    "CREATE_SPECS",
    "CREATE_STATUSES",
    "CreateRequest",
    "run_create",
]
