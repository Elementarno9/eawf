"""The one path an accepted epoch-2 mutation takes from request to commit.

Every native lifecycle mutation walks the same seven steps, in the same
order, whichever entity it moves:

1. Parse strict parameters and resolve the entity URN, the expected
   revision and the idempotency key.
2. Take the root's locks in sorted canonical-URN order, document lock
   last, so two sessions over disjoint entities still cannot interleave
   their read and rewrite of the one document.
3. Re-read the guard inputs from the locked document rather than trusting
   what the request said about them.
4. Evaluate the guarded transition registry through
   :func:`~eawf.workflow.lifecycle.epoch2.apply_transition`, which returns
   the successor record or a typed denial.
5. Allocate the workspace-global ``canonical_sequence`` inside the
   committing transaction, so a sequence only becomes durable together
   with the mutation that carries it.
6. Write one WAL intent, mutate the document once, append one firehose
   row carrying one ``domain.<entity>.<verb>`` event, and mark the WAL
   record durable.
7. Publish after the commit, outside the locks, because a projection
   publish inside them would stall every other writer on the root.

A denied edge writes nothing at all: the denial is decided at step 4,
before the WAL record of step 6 exists. A mutation whose new free text
carries a leak shape is refused in the same place, by the scrub every
canonical state writer already runs, so the refusal also predates the
first byte.

The high-water mark of the sequence lives in the document itself. It is
the only figure that survives a restart, and keeping it beside the rows
it orders means one locked read answers both "what does this record look
like" and "what number comes next".
"""

from __future__ import annotations

import copy
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final, cast

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from eawf.kernel.identity import EntityKind, QualifiedUrn
from eawf.kernel.state.canonical_sequence import CanonicalSequenceAllocator
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.epoch2.base import PrincipalKey, SlugStr, StrictPositiveInt
from eawf.kernel.state.epoch2.transitions import (
    ENTITY_STATUS_ENUM,
    DenialCode,
    LifecycleEntity,
    LifecycleStatus,
    ObservedFact,
)
from eawf.kernel.state.epoch2.urns import AnyEntityUrn
from eawf.kernel.state.io import state_version
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.append import append_json_line
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path
from eawf.kernel.store.tiers import ENTITY_COLLECTIONS, Epoch2Collection
from eawf.observability.logging.state_leak import state_leak_refusal
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.wal import WalRecord, mark_applied, mark_fsynced, write_pending
from eawf.workflow.lifecycle.epoch2 import (
    ENTITY_OF_RECORD,
    GuardContext,
    LifecycleRecord,
    TransitionDenied,
    apply_transition,
)

logger = logging.getLogger(__name__)


#: The document key holding one workspace's committed ``canonical_sequence``
#: high-water mark. It sits beside the collections rather than in a file of
#: its own so the locked read that fetches a record also fetches the next
#: number, and a restart recovers the counter from the same bytes.
CANONICAL_SEQUENCE_KEY: Final = "canonical_sequence"

#: Version of the transition-event payload a firehose row carries.
TRANSITION_EVENT_SCHEMA_VERSION: Final = "1"

#: The file name the JSONL path resolver is anchored on. An epoch-2 tree
#: keeps its firehose at the epoch-1 location, because the firehose is not
#: a ledger and never compacts.
_TREE_ANCHOR_FILENAME: Final = "state.json"


class TransactionRefusalCode(StrEnum):
    """The stable codes this transaction refuses a mutation with.

    Every member is also a member of the daemon's closed domain error
    vocabulary, which is checked where the two meet, so a refusal that
    reaches the wire always carries a code a client can branch on.
    """

    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    IDENTITY_NOT_FOUND = "identity_not_found"
    IDENTITY_KIND_MISMATCH = "identity_kind_mismatch"
    LEGACY_IDENTITY_READ_ONLY = "legacy_identity_read_only"
    REVISION_CONFLICT = "revision_conflict"
    ILLEGAL_TRANSITION = "illegal_transition"
    TRANSITION_GUARD_FAILED = "transition_guard_failed"


#: Which stable code each registry denial is reported as. The registry's
#: own code is finer than the wire vocabulary -- it names the guard -- so
#: it travels in the refusal's ``guard`` field rather than being widened
#: into a second spelling of the wire code.
_STRUCTURAL_DENIALS: Final[Mapping[DenialCode, TransactionRefusalCode]] = {
    DenialCode.ILLEGAL_TRANSITION: TransactionRefusalCode.ILLEGAL_TRANSITION,
    DenialCode.TERMINAL_STATE: TransactionRefusalCode.ILLEGAL_TRANSITION,
    DenialCode.LEGACY_ORIGIN_IMMUTABLE: TransactionRefusalCode.LEGACY_IDENTITY_READ_ONLY,
    DenialCode.MISSING_TRANSITION_FIELDS: TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
}

#: Which lifecycle machine governs each addressable entity kind. Only the
#: five driven records appear: a Release is registered in the transition
#: table but is not an epoch-2 record and has no URN to lock.
LIFECYCLE_ENTITIES: Final[Mapping[EntityKind, LifecycleEntity]] = {
    EntityKind.TRACK: LifecycleEntity.TRACK,
    EntityKind.MILESTONE: LifecycleEntity.MILESTONE,
    EntityKind.BATCH: LifecycleEntity.DELIVERY_BATCH,
    EntityKind.TASK: LifecycleEntity.TASK,
    EntityKind.RUN: LifecycleEntity.RUN,
}

#: Which model each entity's stored rows validate through, derived from the
#: reducer's own table so the two cannot name different classes.
RECORD_CLASSES: Final[Mapping[LifecycleEntity, type[LifecycleRecord]]] = {
    entity: record_class for record_class, entity in ENTITY_OF_RECORD.items()
}


class TransactionRefusedError(DaemonValidationError):
    """One native mutation was refused, with nothing written.

    A :class:`~eawf.runtime.daemon.methods.DaemonValidationError`, so an
    unhandled refusal still reaches the wire as a validation failure
    rather than an internal error.

    Attributes:
        code: The stable code a client branches on.
        detail: The operator-facing explanation, without the code prefix.
        entity_ref: The record the refusal is about.
        guard: The registry's finer denial or guard name, or ``None`` for
            a refusal no predicate was reached for.
        remediation: One sentence saying what to do about it.
        revision: The revision the record is still at, or ``None`` when
            the refusal happened before any record was read. It is the
            record's own revision rather than the one the request
            expected, so a stale caller reads the truth back.
    """

    def __init__(
        self,
        *,
        code: TransactionRefusalCode,
        detail: str,
        entity_ref: str,
        guard: str | None = None,
        remediation: str,
        revision: int | None = None,
    ) -> None:
        """Build the refusal and its ``validation_failed`` wire message."""
        super().__init__(f"validation_failed: {code.value}: {detail}")
        self.code = code
        self.detail = detail
        self.entity_ref = entity_ref
        self.guard = guard
        self.remediation = remediation
        self.revision = revision


class TransitionRequest(BaseModel):
    """The strict parameters one native transition is asked for.

    Attributes:
        urn: The record to move. Parsed and re-rendered, so two spellings
            of one address resolve to one lock and one row.
        to_status: The target status, spelled as the entity's own status
            enum member.
        expected_revision: The compare-and-swap token the caller read the
            record at. A record that moved on since is a conflict, not a
            silent overwrite.
        idempotency_key: The client's name for this request.
        actor: Who asked, as an immutable qualified principal key.
        observations: Facts about the outside world the caller presents.
            An edge behind an observation stays shut until its fact is in
            this tuple.
        updates: Field values the target status makes facts.
        reason_code: The stable reason the move happened, when the edge
            records one.
        binding_refs: The exact proofs the move was taken against.
        correlation_id: The client's thread of related requests.
    """

    model_config = ConfigDict(extra="forbid")

    urn: AnyEntityUrn
    to_status: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]
    expected_revision: StrictPositiveInt
    idempotency_key: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]
    actor: PrincipalKey
    observations: tuple[ObservedFact, ...] = ()
    updates: dict[str, Any] = Field(default_factory=dict)
    reason_code: SlugStr | None = None
    binding_refs: tuple[str, ...] = ()
    correlation_id: Annotated[str, StringConstraints(strict=True, max_length=128)] | None = None


class MutationReceipt(BaseModel):
    """What one committed native mutation left behind.

    Attributes:
        event_name: The ``domain.<entity>.<verb>`` name of what happened.
        entity_ref: The record that moved.
        revision_before: The record's compare-and-swap token before.
        revision_after: The token after; always one more than before.
        canonical_sequence: The workspace-global position of this
            mutation, allocated inside the committing transaction.
        event_id: The firehose row's envelope id.
        idempotency_key: The client's name for the request that caused it.
        occurred_at: When the transition happened.
        wal_record_id: The WAL record the commit was journalled under.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_name: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=120)]
    entity_ref: AnyEntityUrn
    revision_before: StrictPositiveInt
    revision_after: StrictPositiveInt
    canonical_sequence: StrictPositiveInt
    event_id: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]
    idempotency_key: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]
    occurred_at: UtcDatetime
    wal_record_id: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]


@dataclass(frozen=True, slots=True)
class CommittedTransaction:
    """A commit's receipt beside the envelope waiting to be published.

    Attributes:
        receipt: What the caller answers with.
        envelope: The firehose row, held so the publish can happen after
            the locks are released.
    """

    receipt: MutationReceipt
    envelope: Envelope


@dataclass(frozen=True, slots=True)
class _CommitPlan:
    """Everything decided before the first byte of a commit is written.

    Attributes:
        record: The record as the locked document held it.
        successor: The record the transition produced.
        event_name: The ``domain.<entity>.<verb>`` name of the move.
        sequence: The ordinal drawn from the committing transaction.
        document: The document as read, kept so the leak scrub has
            something to diff the proposed write against.
        new_document: The whole document the commit leaves behind.
        envelope: The one firehose row the commit appends.
    """

    record: LifecycleRecord
    successor: LifecycleRecord
    event_name: str
    sequence: int
    document: dict[str, Any]
    new_document: dict[str, Any]
    envelope: Envelope


def run_transaction(
    *,
    context: Epoch2RootContext,
    request: TransitionRequest,
    now: datetime,
) -> CommittedTransaction:
    """Commit one native transition, or refuse it having written nothing.

    The locks are released by the time this returns, which is what makes
    the returned envelope publishable: a publish taken inside them would
    hold every other writer on the root for the length of a fan-out.

    Args:
        context: The native context of the tree the mutation addresses.
        request: The already-validated request parameters.
        now: When the transition happened. Supplied by the caller so the
            stored record, the event and the WAL record all agree.

    Returns:
        The receipt of the committed mutation beside the firehose
        envelope the caller publishes.

    Raises:
        TransactionRefusedError: The request names an unknown entity kind or
            status, addresses a record the document does not hold, builds
            on a stale revision, is denied by the transition registry, or
            adds free text carrying a leak shape. Nothing was written.
        NativeAuthorityRequiredError: The tree left epoch 2.
        MigrationDualAuthorityError: The tree's select is not whole.
        LockTimeout: A lock stayed held past the lock timeout.
    """
    entity = _entity_for(request.urn)
    target = _target_status(entity, request)
    collection = ENTITY_COLLECTIONS[request.urn.kind]
    with context.session([request.urn]) as session:
        document = session.read_document()
        record = _reread_record(
            document=document,
            collection=collection,
            entity=entity,
            request=request,
        )
        outcome = apply_transition(
            record,
            to=target,
            at=now,
            ctx=GuardContext(observations=frozenset(request.observations)),
            updates=request.updates,
        )
        if isinstance(outcome, TransitionDenied):
            raise _denied(outcome, request=request, revision=record.revision)
        allocator = CanonicalSequenceAllocator.recover(
            workspace_key=request.urn.workspace_key,
            high_water_mark=_high_water_mark(document),
        )
        with allocator.transaction() as sequences:
            plan = _plan_commit(
                document=document,
                collection=collection,
                record=record,
                successor=outcome.record,
                event_name=outcome.event.name,
                sequence=sequences.allocate(),
                request=request,
                now=now,
            )
            receipt = _persist(
                context=context,
                document_path=session.document_path,
                plan=plan,
                request=request,
                now=now,
                write_document=session.write_document,
            )
            logger.info(
                f"epoch2 transaction committed root={context.identity.root_id} "
                f"event={receipt.event_name} sequence={receipt.canonical_sequence}"
            )
            return CommittedTransaction(receipt=receipt, envelope=plan.envelope)


def _persist(
    *,
    context: Epoch2RootContext,
    document_path: Path,
    plan: _CommitPlan,
    request: TransitionRequest,
    now: datetime,
    write_document: Callable[[dict[str, Any]], None],
) -> MutationReceipt:
    """Write the intent, the document, the firehose row and the durability mark.

    Raises:
        TransactionRefusedError: The write would add free text carrying a leak
            shape, refused before the WAL record exists.
    """
    _refuse_leaks(plan, request=request)
    wal_record = WalRecord(
        record_id=uuid.uuid4().hex,
        envelope=plan.envelope,
        idempotency_key=request.idempotency_key,
        written_at=now,
        before_state_version=state_version(plan.document),
        after_state_version=state_version(plan.new_document),
        state_path=str(document_path),
    )
    write_pending(context.wal_dir, wal_record)
    write_document(plan.new_document)
    mark_applied(context.wal_dir, wal_record.record_id)
    append_json_line(_firehose_path(context), plan.envelope.model_dump_json())
    mark_fsynced(context.wal_dir, wal_record.record_id)
    return MutationReceipt(
        event_name=plan.event_name,
        entity_ref=request.urn,
        revision_before=plan.record.revision,
        revision_after=plan.successor.revision,
        canonical_sequence=plan.sequence,
        event_id=plan.envelope.id,
        idempotency_key=request.idempotency_key,
        occurred_at=now,
        wal_record_id=wal_record.record_id,
    )


def _entity_for(urn: QualifiedUrn) -> LifecycleEntity:
    """Return the lifecycle machine that governs the record *urn* addresses.

    Raises:
        TransactionRefusedError: The kind has no lifecycle machine, so no
            transition of it exists to evaluate.
    """
    entity = LIFECYCLE_ENTITIES.get(urn.kind)
    if entity is None:
        admitted = ", ".join(sorted(kind.value for kind in LIFECYCLE_ENTITIES))
        raise TransactionRefusedError(
            code=TransactionRefusalCode.IDENTITY_KIND_MISMATCH,
            detail=f"{urn.kind.value} has no lifecycle machine; expected one of {admitted}",
            entity_ref=str(urn),
            remediation="Address a track, milestone, batch, task or run.",
        )
    return entity


def _target_status(entity: LifecycleEntity, request: TransitionRequest) -> LifecycleStatus:
    """Return the target status *request* names, in the entity's own enum.

    Raises:
        TransactionRefusedError: The token is not a status of that entity.
    """
    status_enum = ENTITY_STATUS_ENUM[entity]
    try:
        return cast("LifecycleStatus", status_enum(request.to_status))
    except ValueError as error:
        legal = ", ".join(sorted(str(member) for member in status_enum))
        raise TransactionRefusedError(
            code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
            detail=f"{request.to_status!r} is not a {entity.value} status; legal: {legal}",
            entity_ref=str(request.urn),
            remediation="Name a status the entity's machine declares.",
        ) from error


def _reread_record(
    *,
    document: dict[str, Any],
    collection: Epoch2Collection,
    entity: LifecycleEntity,
    request: TransitionRequest,
) -> LifecycleRecord:
    """Return the record the locked document holds, checked against the request.

    Raises:
        TransactionRefusedError: The document holds no such row, the stored row
            does not validate, or its revision is not the one the caller
            built on.
    """
    row = document_rows(document, collection).get(request.urn.entity_key)
    if row is None:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.IDENTITY_NOT_FOUND,
            detail=f"the document holds no {collection.value} row keyed {request.urn.entity_key!r}",
            entity_ref=str(request.urn),
            remediation="Create the record before transitioning it.",
        )
    try:
        record = RECORD_CLASSES[entity].model_validate(row)
    except ValueError as error:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
            detail=f"the stored {entity.value} row does not validate through its model",
            entity_ref=str(request.urn),
            remediation="Repair the stored row before transitioning it.",
        ) from error
    if record.revision != request.expected_revision:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.REVISION_CONFLICT,
            detail=f"the record is at revision {record.revision} but the request expects "
            f"{request.expected_revision}",
            entity_ref=str(request.urn),
            remediation="Re-read the record and retry against its current revision.",
            revision=record.revision,
        )
    return record


def _denied(
    denial: TransitionDenied,
    *,
    request: TransitionRequest,
    revision: int,
) -> TransactionRefusedError:
    """Return the refusal one registry denial is reported as."""
    code = _STRUCTURAL_DENIALS.get(denial.code, TransactionRefusalCode.TRANSITION_GUARD_FAILED)
    guard = denial.guard.value if denial.guard is not None else denial.code.value
    return TransactionRefusedError(
        code=code,
        revision=revision,
        detail=denial.message,
        entity_ref=str(request.urn),
        guard=guard,
        remediation=denial.remediation,
    )


def _high_water_mark(document: dict[str, Any]) -> int:
    """Return the committed sequence the document records.

    Raises:
        ValueError: The document holds a non-integer where the high-water
            mark belongs, so the next sequence cannot be derived.
    """
    recorded = document.get(CANONICAL_SEQUENCE_KEY, 0)
    if not isinstance(recorded, int) or isinstance(recorded, bool):
        raise ValueError(
            f"document {CANONICAL_SEQUENCE_KEY} holds a {type(recorded).__name__}, not an integer"
        )
    return recorded


def _plan_commit(
    *,
    document: dict[str, Any],
    collection: Epoch2Collection,
    record: LifecycleRecord,
    successor: LifecycleRecord,
    event_name: str,
    sequence: int,
    request: TransitionRequest,
    now: datetime,
) -> _CommitPlan:
    """Return the whole commit, decided but not yet written anywhere.

    The document is deep-copied rather than edited, because the leak scrub
    diffs the two payloads and an in-place edit would leave it comparing
    one payload with itself.
    """
    new_document = copy.deepcopy(document)
    rows = new_document.setdefault(collection.value, {})
    rows[record.key] = successor.model_dump(mode="json")
    new_document[CANONICAL_SEQUENCE_KEY] = sequence
    return _CommitPlan(
        record=record,
        successor=successor,
        event_name=event_name,
        sequence=sequence,
        document=document,
        new_document=new_document,
        envelope=_transition_envelope(
            request=request,
            record=record,
            successor=successor,
            event_name=event_name,
            sequence=sequence,
            now=now,
        ),
    )


def _transition_envelope(
    *,
    request: TransitionRequest,
    record: LifecycleRecord,
    successor: LifecycleRecord,
    event_name: str,
    sequence: int,
    now: datetime,
) -> Envelope:
    """Return the one firehose row this transition appends."""
    urn = request.urn
    event_id = f"evt-{uuid.uuid4().hex}"
    return Envelope(
        id=event_id,
        kind=StoreKind.EVENT,
        scope_id=str(urn),
        created_at=now,
        summary=f"{event_name} {urn.entity_key} {record.status!s} -> {successor.status!s}",
        payload={
            "schema_version": TRANSITION_EVENT_SCHEMA_VERSION,
            "name": event_name,
            "event_id": event_id,
            "occurred_at": now.isoformat(),
            "workspace_ref": urn.workspace_key,
            "project_ref": urn.project_key,
            "entity_ref": str(urn),
            "from_status": str(record.status),
            "to_status": str(successor.status),
            "revision_before": record.revision,
            "revision_after": successor.revision,
            "actor_ref": request.actor,
            "reason_code": request.reason_code,
            "binding_refs": list(request.binding_refs),
            "idempotency_key": request.idempotency_key,
            "correlation_id": request.correlation_id,
            "canonical_sequence": sequence,
        },
    )


def _refuse_leaks(plan: _CommitPlan, *, request: TransitionRequest) -> None:
    """Refuse a commit whose added or changed text carries a leak shape.

    Raises:
        TransactionRefusedError: The write adds a home path, an address or a
            credential-shaped token. The detail names the field paths and
            never repeats the matched text.
    """
    refusal = state_leak_refusal(plan.document, plan.new_document)
    if refusal is None:
        return
    logger.warning(f"epoch2 transaction refused for leaks entity={request.urn.entity_key!r}")
    raise TransactionRefusedError(
        revision=plan.record.revision,
        code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
        detail=refusal,
        entity_ref=str(request.urn),
        remediation="Remove the flagged text from the mutation and retry.",
    )


def _firehose_path(context: Epoch2RootContext) -> Path:
    """Return the declared firehose of one root.

    Raises:
        UndeclaredPathError: The commit policy declares no row for it.
    """
    anchor = context.identity.tree_root / _TREE_ANCHOR_FILENAME
    return context.declared_path(store_path(anchor, StoreKind.EVENT))


__all__ = [
    "CANONICAL_SEQUENCE_KEY",
    "LIFECYCLE_ENTITIES",
    "RECORD_CLASSES",
    "TRANSITION_EVENT_SCHEMA_VERSION",
    "CommittedTransaction",
    "MutationReceipt",
    "TransactionRefusalCode",
    "TransactionRefusedError",
    "TransitionRequest",
    "run_transaction",
]
