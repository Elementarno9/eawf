"""The verb that gives a Batch the title shown beside its key.

A title is not a lifecycle move: the Batch stays in the status it holds, so
the verb has no edge in the transition registry and its event is not a
``domain.batch.<verb>`` name, whose vocabulary is closed to registered
edges. Every status is accepted, a finished one included, because a title
names what the Batch delivered and records no fact about how it went.

A Batch still in the document commits through the transaction's own write
step, as a target date does. A finished Batch has compacted into its
ledger, where a line is never edited, so its title is filed as a line that
supersedes the one standing for it -- the record whole, one revision on --
the way a squash rebind amends finished rows. Either way a stale revision
is a conflict rather than an overwrite, and a retry under the same key
replays its answer.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from eawf.kernel.identity import EntityKind
from eawf.kernel.projection.compute import CANONICAL_SEQUENCE_FIELD
from eawf.kernel.state.canonical_sequence import CanonicalSequenceAllocator
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey, StrictPositiveInt, TitleStr
from eawf.kernel.state.epoch2.batch import DeliveryBatch
from eawf.kernel.state.epoch2.urns import AnyEntityUrn
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import (
    LedgerRecord,
    line_digest,
    read_ledger_content,
    split_ledger_lines,
)
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_recovery import (
    PROJECTION_DEGRADED,
    publish_projection,
    record_idempotency_receipt,
)
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.epoch2_transaction import (
    CANONICAL_SEQUENCE_KEY,
    CommittedTransaction,
    MutationReceipt,
    TransactionRefusalCode,
    TransactionRefusedError,
    _CommitPlan,
    _high_water_mark,
    _persist,
    _replayed_receipt,
    _request_digest,
    commit_ledger_append,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.domain_envelope import (
    accepted_envelope,
    refused_envelope,
    schema_refusal,
)
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_mutator
from eawf.runtime.daemon.wal import list_records, read_record

logger = logging.getLogger(__name__)

#: The dotted JSON-RPC name of the verb.
BATCH_SET_TITLE: Final = "domain.batch.set_title"

#: The firehose name of a committed change, outside the closed ``domain`` vocabulary.
TITLE_SET_EVENT: Final = "batch.title_set"

#: Version of the payload a title firehose row carries.
TITLE_EVENT_SCHEMA_VERSION: Final = "1"


class BatchTitleRequest(BaseModel):
    """The strict parameters of :data:`BATCH_SET_TITLE`.

    Attributes:
        urn: The Batch to title.
        expected_revision: The compare-and-swap token the caller read it at.
        idempotency_key: The client's name for this request.
        actor: Who asked, as an immutable qualified principal key.
        title: The title, an imperative noun phrase of at most 80 characters.
        correlation_id: The client's thread of related requests.
    """

    model_config = ConfigDict(extra="forbid")

    urn: AnyEntityUrn
    expected_revision: StrictPositiveInt
    idempotency_key: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]
    actor: PrincipalKey
    title: TitleStr
    correlation_id: Annotated[str, StringConstraints(strict=True, max_length=128)] | None = None


def _validated(row: Any, request: BatchTitleRequest) -> DeliveryBatch:
    """Return *row* as a Batch at the revision the request names.

    Raises:
        TransactionRefusedError: The row does not validate as a native Batch,
            or its revision moved on.
    """
    try:
        batch = DeliveryBatch.model_validate(row)
    except ValidationError as error:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
            detail="the stored batch row does not validate through its model",
            entity_ref=str(request.urn),
            remediation="Repair the stored row before titling it.",
        ) from error
    if batch.revision != request.expected_revision:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.REVISION_CONFLICT,
            detail=f"the record is at revision {batch.revision} but the request expects "
            f"{request.expected_revision}",
            entity_ref=str(request.urn),
            remediation="Re-read the record and retry against its current revision.",
            revision=batch.revision,
        )
    return batch


def _standing_line(session: RootSession, key: str) -> tuple[str, LedgerRecord] | None:
    """Return the digest and record of the line standing for Batch *key* in its ledger.

    Only a line whose payload names its own key is the record: the Batch
    ledger also files verification cycles and merge reconciliations, and an
    imported epoch-1 Batch line states no ``key`` at all.
    """
    lines = split_ledger_lines(read_ledger_content(session.ledger_path(Epoch2Collection.BATCH)))
    records = [LedgerRecord.model_validate_json(line) for line in lines]
    superseded = {record.supersedes for record in records if record.supersedes is not None}
    for line, record in reversed(list(zip(lines, records, strict=True))):
        if record.record_key != key or record.payload.get("key") != key:
            continue
        digest = line_digest(line)
        if digest not in superseded:
            return digest, record
    return None


def _wal_record_id(context: Epoch2RootContext, event_id: str) -> str:
    """Return the id of the WAL record a ledger append journalled *event_id* under.

    The append returns only the firehose row it wrote, while a receipt names
    the journal record too; the record carries that row, so it is found by it.

    Raises:
        LookupError: No WAL record carries the row, which the append that just
            returned it rules out.
    """
    for path in list_records(context.wal_dir):
        record = read_record(path)
        if record.envelope.id == event_id:
            return record.record_id
    raise LookupError(f"no WAL record carries event {event_id!r}")


def _envelope(
    request: BatchTitleRequest,
    *,
    before: DeliveryBatch,
    after: DeliveryBatch,
    sequence: int,
    now: datetime,
) -> Envelope:
    """Return the one firehose row a document-held Batch's change appends.

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
        summary=f"{TITLE_SET_EVENT} {urn.entity_key}",
        payload={
            "schema_version": TITLE_EVENT_SCHEMA_VERSION,
            "name": TITLE_SET_EVENT,
            "event_id": event_id,
            "occurred_at": now.isoformat(),
            "workspace_ref": urn.workspace_key,
            "project_ref": urn.project_key,
            "entity_ref": str(urn),
            "from_status": before.status.value,
            "to_status": after.status.value,
            "revision_before": before.revision,
            "revision_after": after.revision,
            "actor_ref": request.actor,
            "idempotency_key": request.idempotency_key,
            "correlation_id": request.correlation_id,
            "canonical_sequence": sequence,
        },
    )


def _titled(before: DeliveryBatch, request: BatchTitleRequest, now: datetime) -> DeliveryBatch:
    """Return *before* carrying the requested title, one revision on."""
    return DeliveryBatch.model_validate(
        {
            **before.model_dump(mode="json"),
            "title": request.title,
            "revision": before.revision + 1,
            "updated_at": now,
        }
    )


def _commit_in_document(
    context: Epoch2RootContext,
    session: RootSession,
    document: dict[str, Any],
    request: BatchTitleRequest,
    *,
    now: datetime,
) -> CommittedTransaction:
    """Commit the title of a Batch the document holds through the transaction's write step."""
    before = _validated(
        document_rows(document, Epoch2Collection.BATCH)[request.urn.entity_key], request
    )
    after = _titled(before, request, now)
    allocator = CanonicalSequenceAllocator.recover(
        workspace_key=request.urn.workspace_key, high_water_mark=_high_water_mark(document)
    )
    with allocator.transaction() as sequences:
        sequence = sequences.allocate()
        new_document = copy.deepcopy(document)
        new_document[Epoch2Collection.BATCH.value][before.key] = after.model_dump(mode="json")
        new_document[CANONICAL_SEQUENCE_KEY] = sequence
        plan = _CommitPlan(
            record=before,
            successor=after,
            event_name=TITLE_SET_EVENT,
            sequence=sequence,
            document=document,
            new_document=new_document,
            envelope=_envelope(request, before=before, after=after, sequence=sequence, now=now),
            compaction=None,
        )
        receipt = _persist(context=context, session=session, plan=plan, request=request, now=now)
    return CommittedTransaction(receipt=receipt, envelope=plan.envelope)


def _commit_in_ledger(
    session: RootSession, request: BatchTitleRequest, *, now: datetime
) -> CommittedTransaction:
    """File the title of a compacted Batch as a line superseding the one standing for it.

    Raises:
        TransactionRefusedError: The ledger holds no native line for the Batch,
            or the standing one is stale or does not validate.
    """
    key = request.urn.entity_key
    standing = _standing_line(session, key)
    if standing is None:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.IDENTITY_NOT_FOUND,
            detail=f"neither the document nor the batch ledger holds a native batch keyed {key!r}",
            entity_ref=str(request.urn),
            remediation="Name a Batch this tree created; an imported epoch-1 Batch keeps the "
            "title it was imported with.",
        )
    digest, line = standing
    before = _validated(line.payload, request)
    after = _titled(before, request, now)
    envelope = commit_ledger_append(
        session,
        LedgerRecord(
            collection=Epoch2Collection.BATCH,
            record_key=key,
            status=line.status,
            recorded_at=now,
            supersedes=digest,
            payload=after.model_dump(mode="json"),
        ),
        patch_fields={
            "entity_ref": str(request.urn),
            "to_status": after.status.value,
            "revision_after": after.revision,
        },
    )
    receipt = MutationReceipt(
        event_name=TITLE_SET_EVENT,
        entity_ref=request.urn,
        revision_before=before.revision,
        revision_after=after.revision,
        canonical_sequence=envelope.payload[CANONICAL_SEQUENCE_FIELD],
        event_id=envelope.id,
        idempotency_key=request.idempotency_key,
        occurred_at=now,
        wal_record_id=_wal_record_id(session.context, envelope.id),
    )
    return CommittedTransaction(receipt=receipt, envelope=envelope)


def set_title(
    context: Epoch2RootContext, request: BatchTitleRequest, *, now: datetime
) -> CommittedTransaction:
    """Commit one Batch's title, or refuse having written nothing.

    Args:
        context: The native context of the tree the request addresses.
        request: The validated request.
        now: When the change happened, stamped as the record's ``updated_at``.

    Returns:
        The receipt beside the firehose row to publish; a retry of a committed
        request returns that commit's receipt and no row.

    Raises:
        TransactionRefusedError: The URN is not a Batch's, the key committed
            other parameters, the Batch is absent, stale or does not
            validate. Nothing was written.
    """
    urn = request.urn
    if urn.kind is not EntityKind.BATCH:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.IDENTITY_KIND_MISMATCH,
            detail=f"{BATCH_SET_TITLE} addresses a batch but the request names a {urn.kind.value}",
            entity_ref=str(urn),
            remediation="Name a batch URN.",
        )
    with context.session([urn]) as session:
        replayed = _replayed_receipt(context, request=request)
        if replayed is not None:
            return CommittedTransaction(receipt=replayed, envelope=None, replayed=True)
        document = session.read_document()
        if urn.entity_key in document_rows(document, Epoch2Collection.BATCH):
            committed = _commit_in_document(context, session, document, request, now=now)
        else:
            committed = _commit_in_ledger(session, request, now=now)
            # The document step files its own receipt; a ledger append files none,
            # so it is filed where the document step's is, for one replay rule.
            record_idempotency_receipt(
                context,
                namespaced_key=context.idempotency_key(request.idempotency_key),
                params_digest=_request_digest(request),
                receipt=committed.receipt.model_dump(mode="json"),
                recorded_at=now,
            )
    logger.info(
        f"set_title key={urn.entity_key!r} revision={committed.receipt.revision_after} "
        f"sequence={committed.receipt.canonical_sequence}"
    )
    return committed


@native_mutator(BATCH_SET_TITLE)
async def _batch_set_title(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Give one Batch, open or finished, the title shown beside its key."""
    try:
        request = BatchTitleRequest.model_validate(
            {key: value for key, value in params.items() if key != REPO_ROOT_PARAM}
        )
    except ValidationError as error:
        return schema_refusal(error, params=params, operation=BATCH_SET_TITLE).model_dump(
            mode="json"
        )
    context = ctx.native_root_context(authority.root)
    try:
        committed = await asyncio.to_thread(set_title, context, request, now=datetime.now(UTC))
    except TransactionRefusedError as refusal:
        logger.info(f"_batch_set_title refused code={refusal.code.value}")
        return refused_envelope(refusal, operation=BATCH_SET_TITLE).model_dump(mode="json")
    degraded = committed.envelope is not None and not publish_projection(
        ctx.bus, committed.envelope
    )
    return accepted_envelope(
        committed.receipt,
        operation=BATCH_SET_TITLE,
        warnings=(PROJECTION_DEGRADED,) if degraded else (),
    ).model_dump(mode="json")


__all__ = [
    "BATCH_SET_TITLE",
    "TITLE_SET_EVENT",
    "BatchTitleRequest",
    "set_title",
]
