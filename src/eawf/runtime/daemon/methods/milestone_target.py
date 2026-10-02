"""The verb that sets or clears the calendar day a Milestone is aimed at.

A target date is not a lifecycle move: the Milestone stays in the status it
holds, so the verb has no edge in the transition registry and its event is
not a ``domain.milestone.<verb>`` name, whose vocabulary is closed to
registered edges. It still commits through the transaction's own write
step, so a retry under the same key replays its receipt, a stale revision
is a conflict rather than an overwrite, and the firehose row carries the
address, status and revision a keyed patch is built from.

A COMPLETED or CANCELLED Milestone is refused: the date it closed with is
part of what it records, and moving it afterwards would rewrite history.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from eawf.kernel.identity import EntityKind
from eawf.kernel.state.canonical_sequence import CanonicalSequenceAllocator
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey, StrictPositiveInt
from eawf.kernel.state.epoch2.milestone import Milestone, MilestoneStatus
from eawf.kernel.state.epoch2.urns import AnyEntityUrn
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_recovery import PROJECTION_DEGRADED, publish_projection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import (
    CANONICAL_SEQUENCE_KEY,
    CommittedTransaction,
    TransactionRefusalCode,
    TransactionRefusedError,
    _CommitPlan,
    _high_water_mark,
    _persist,
    _replayed_receipt,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.domain_envelope import (
    accepted_envelope,
    refused_envelope,
    schema_refusal,
)
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_mutator

logger = logging.getLogger(__name__)

#: The dotted JSON-RPC name of the verb.
MILESTONE_SET_TARGET: Final = "domain.milestone.set_target"

#: The firehose name of a committed change, outside the closed ``domain`` vocabulary.
TARGET_SET_EVENT: Final = "milestone.target_set"

#: Version of the payload a target-date firehose row carries.
TARGET_EVENT_SCHEMA_VERSION: Final = "1"

#: The statuses whose date is history rather than a plan.
_CLOSED: Final = frozenset({MilestoneStatus.COMPLETED, MilestoneStatus.CANCELLED})


class MilestoneTargetRequest(BaseModel):
    """The strict parameters of :data:`MILESTONE_SET_TARGET`.

    Attributes:
        urn: The Milestone whose date is set.
        expected_revision: The compare-and-swap token the caller read it at.
        idempotency_key: The client's name for this request.
        actor: Who asked, as an immutable qualified principal key.
        target_date: The day the Milestone is aimed at; ``None`` clears it.
            Required, so a request that forgot the date is refused rather
            than read as a clear.
        correlation_id: The client's thread of related requests.
    """

    model_config = ConfigDict(extra="forbid")

    urn: AnyEntityUrn
    expected_revision: StrictPositiveInt
    idempotency_key: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]
    actor: PrincipalKey
    target_date: date | None
    correlation_id: Annotated[str, StringConstraints(strict=True, max_length=128)] | None = None


def _held_milestone(document: dict[str, Any], request: MilestoneTargetRequest) -> Milestone:
    """Return the open Milestone the request addresses, at the revision it names.

    Raises:
        TransactionRefusedError: The URN is not a Milestone's, the document holds
            no such row or holds one that does not validate, the revision moved
            on, or the Milestone is closed.
    """
    urn = request.urn
    if urn.kind is not EntityKind.MILESTONE:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.IDENTITY_KIND_MISMATCH,
            detail=f"{MILESTONE_SET_TARGET} addresses a milestone but the request names a "
            f"{urn.kind.value}",
            entity_ref=str(urn),
            remediation="Name a milestone URN.",
        )
    row = document_rows(document, Epoch2Collection.MILESTONE).get(urn.entity_key)
    if row is None:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.IDENTITY_NOT_FOUND,
            detail=f"the document holds no open milestone keyed {urn.entity_key!r}",
            entity_ref=str(urn),
            remediation="Create the Milestone first; a closed one is read from its ledger.",
        )
    try:
        milestone = Milestone.model_validate(row)
    except ValidationError as error:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
            detail="the stored milestone row does not validate through its model",
            entity_ref=str(urn),
            remediation="Repair the stored row before dating it.",
        ) from error
    if milestone.revision != request.expected_revision:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.REVISION_CONFLICT,
            detail=f"the record is at revision {milestone.revision} but the request expects "
            f"{request.expected_revision}",
            entity_ref=str(urn),
            remediation="Re-read the record and retry against its current revision.",
            revision=milestone.revision,
        )
    if milestone.status in _CLOSED:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.ILLEGAL_TRANSITION,
            detail=f"a {milestone.status.value} milestone keeps the date it closed with",
            entity_ref=str(urn),
            remediation="Date a Milestone that is still PLANNED, ACTIVE or in review.",
            revision=milestone.revision,
        )
    return milestone


def _envelope(
    request: MilestoneTargetRequest,
    *,
    before: Milestone,
    after: Milestone,
    sequence: int,
    now: datetime,
) -> Envelope:
    """Return the one firehose row the change appends.

    The status is stated as both from and to, so the keyed patch a console
    builds from the row moves the revision and leaves the status where it is.
    """
    urn = request.urn
    event_id = f"evt-{uuid.uuid4().hex}"
    target = None if after.target_date is None else after.target_date.isoformat()
    return Envelope(
        id=event_id,
        kind=StoreKind.EVENT,
        scope_id=str(urn),
        created_at=now,
        summary=f"{TARGET_SET_EVENT} {urn.entity_key} {target or 'cleared'}",
        payload={
            "schema_version": TARGET_EVENT_SCHEMA_VERSION,
            "name": TARGET_SET_EVENT,
            "event_id": event_id,
            "occurred_at": now.isoformat(),
            "workspace_ref": urn.workspace_key,
            "project_ref": urn.project_key,
            "entity_ref": str(urn),
            "from_status": before.status.value,
            "to_status": after.status.value,
            "revision_before": before.revision,
            "revision_after": after.revision,
            "target_date": target,
            "actor_ref": request.actor,
            "idempotency_key": request.idempotency_key,
            "correlation_id": request.correlation_id,
            "canonical_sequence": sequence,
        },
    )


def set_target_date(
    context: Epoch2RootContext, request: MilestoneTargetRequest, *, now: datetime
) -> CommittedTransaction:
    """Commit one Milestone's new target date, or refuse having written nothing.

    Args:
        context: The native context of the tree the request addresses.
        request: The validated request.
        now: When the change happened, stamped as the record's ``updated_at``.

    Returns:
        The receipt beside the firehose row to publish; a retry of a committed
        request returns that commit's receipt and no row.

    Raises:
        TransactionRefusedError: The key committed other parameters, or
            :func:`_held_milestone` refused the subject. Nothing was written.
    """
    with context.session([request.urn]) as session:
        replayed = _replayed_receipt(context, request=request)
        if replayed is not None:
            return CommittedTransaction(receipt=replayed, envelope=None, replayed=True)
        document = session.read_document()
        before = _held_milestone(document, request)
        after = Milestone.model_validate(
            {
                **before.model_dump(mode="json"),
                "target_date": request.target_date,
                "revision": before.revision + 1,
                "updated_at": now,
            }
        )
        allocator = CanonicalSequenceAllocator.recover(
            workspace_key=request.urn.workspace_key, high_water_mark=_high_water_mark(document)
        )
        with allocator.transaction() as sequences:
            sequence = sequences.allocate()
            new_document = copy.deepcopy(document)
            new_document[Epoch2Collection.MILESTONE.value][before.key] = after.model_dump(
                mode="json"
            )
            new_document[CANONICAL_SEQUENCE_KEY] = sequence
            plan = _CommitPlan(
                record=before,
                successor=after,
                event_name=TARGET_SET_EVENT,
                sequence=sequence,
                document=document,
                new_document=new_document,
                envelope=_envelope(request, before=before, after=after, sequence=sequence, now=now),
                compaction=None,
            )
            receipt = _persist(
                context=context, session=session, plan=plan, request=request, now=now
            )
    logger.info(
        f"set_target_date key={before.key!r} target={request.target_date} "
        f"sequence={receipt.canonical_sequence}"
    )
    return CommittedTransaction(receipt=receipt, envelope=plan.envelope)


@native_mutator(MILESTONE_SET_TARGET)
async def _milestone_set_target(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Set or clear the target date of one open Milestone."""
    try:
        request = MilestoneTargetRequest.model_validate(
            {key: value for key, value in params.items() if key != REPO_ROOT_PARAM}
        )
    except ValidationError as error:
        return schema_refusal(error, params=params, operation=MILESTONE_SET_TARGET).model_dump(
            mode="json"
        )
    context = ctx.native_root_context(authority.root)
    try:
        committed = await asyncio.to_thread(
            set_target_date, context, request, now=datetime.now(UTC)
        )
    except TransactionRefusedError as refusal:
        logger.info(f"_milestone_set_target refused code={refusal.code.value}")
        return refused_envelope(refusal, operation=MILESTONE_SET_TARGET).model_dump(mode="json")
    degraded = committed.envelope is not None and not publish_projection(
        ctx.bus, committed.envelope
    )
    return accepted_envelope(
        committed.receipt,
        operation=MILESTONE_SET_TARGET,
        warnings=(PROJECTION_DEGRADED,) if degraded else (),
    ).model_dump(mode="json")


__all__ = [
    "MILESTONE_SET_TARGET",
    "TARGET_SET_EVENT",
    "MilestoneTargetRequest",
    "set_target_date",
]
