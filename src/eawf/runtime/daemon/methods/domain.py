"""The per-entity lifecycle verbs of a Track, a Milestone, a Batch and a Task.

The entity-agnostic transition verb asks the caller which status to move
to. That is the right shape for a machine replaying a recorded edge and
the wrong one for everything else: a client that spells the target status
itself can ask for an edge the entity has, from a state the entity is not
in, and be told only that the move is illegal. A verb names the move
instead -- ``domain.milestone.accept`` -- so the target status is the
daemon's to decide and the source states the verb admits are declared
beside it.

The second thing a verb owns is the guards a document can answer. The
transition registry is a pure table and its reducer defaults a
non-observation guard to satisfied, which means the entity-agnostic path
admits an edge whose computable predicate is false: a Milestone activates
under a retired Track, a Track retires with open Milestones under it, a
Batch declares itself mergeable with Tasks still running. Those
predicates are computed here, against the document the mutation would
land in, and an unmet one refuses the request with the same
``transition_guard_failed`` code and the same guard name the registry
would have reported had it been able to evaluate it.

A predicate about a sibling record reads the ledgers as well as the
document. A Batch that completes is compacted out of the document into
its append-only ledger, so a Milestone asking whether its required
Batches have closed would find nothing where they used to be and shut the
edge their completion is supposed to open. The lookup therefore falls
through to the collection's standing ledger lines, and a ledger it cannot
read answers nothing at all -- which shuts the edge, exactly as an absent
row does.

A refusal decided here writes nothing at all. It is taken before the
transaction opens its own session, so there is no WAL intent, no document
rewrite and no firehose row to undo -- the same promise the transaction
makes for the denials it decides itself. What the preflight does not
decide it leaves alone: an absent record, a stale revision and an
unreadable row are the transaction's refusals, and a request whose
idempotency key already committed skips the preflight entirely, because a
retry must replay its original receipt rather than be re-judged against a
document its own commit has since moved.

Milestone acceptance carries one requirement no other verb does. Accepting
a Milestone is the moment the work is declared done, so the request must
carry the reference of a sealed PendingAction receipt; without one the
answer is ``protected_approval_required`` and nothing moves. The reference
travels into the committed event's binding refs, so the approval an
acceptance was taken against is readable from the event alone.

The referenced record is then read from the tree, never presented, and
held against the acceptance bundle the request carries. A caller may
present any bundle it likes: the sealed approval records the digest of
the bundle it was actually given to, so a bundle that is not that one
digests differently and the acceptance is refused. Who sealed it needs no
check at all -- the resolver slot of a PendingAction is typed to a human
principal, so an agent-approved acceptance is a record that cannot exist.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from eawf.kernel.delivery.acceptance import MilestoneAcceptanceBundle
from eawf.kernel.identity import EntityKind, IdentityError, QualifiedUrn, parse_qualified_urn
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey, SlugStr, StrictPositiveInt
from eawf.kernel.state.epoch2.batch import BatchStatus, DeliveryBatch
from eawf.kernel.state.epoch2.milestone import Milestone, MilestoneStatus
from eawf.kernel.state.epoch2.pending_action import PendingAction
from eawf.kernel.state.epoch2.task import Task, TaskStatus
from eawf.kernel.state.epoch2.track import Track, TrackStatus
from eawf.kernel.state.epoch2.transitions import (
    DENIAL_REMEDIATION,
    GUARD_DENIALS,
    DenialCode,
    LifecycleStatus,
    ObservedFact,
    TransitionGuard,
    row_for,
)
from eawf.kernel.state.epoch2.urns import AnyEntityUrn
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.ledger import LedgerError, effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import ENTITY_COLLECTIONS, Epoch2Collection
from eawf.runtime.daemon.epoch2_recovery import (
    PROJECTION_DEGRADED,
    publish_projection,
    read_idempotency_receipt,
)
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import (
    LIFECYCLE_ENTITIES,
    RECORD_CLASSES,
    TransactionRefusalCode,
    TransactionRefusedError,
    TransitionRequest,
    run_transaction,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.domain_envelope import (
    ENTITY_REF_WIDTH,
    ENVELOPE_SCHEMA_VERSION,
    UNNAMED_SUBJECT,
    DomainEnvelope,
    DomainError,
    DomainErrorCode,
    DomainStatus,
    accepted_envelope,
    refused_envelope,
)
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_mutator
from eawf.workflow.delivery.acceptance import AcceptanceRefusedError, require_sealed_acceptance
from eawf.workflow.lifecycle.epoch2 import LifecycleRecord

logger = logging.getLogger(__name__)


#: The Milestone statuses that stop a Track from counting it as open. The
#: pair is spelled out rather than read off the terminal set because the
#: remediation an operator is handed says "complete or cancel", and a
#: Milestone that reached neither is still work under the Track.
_CLOSED_MILESTONE_STATUSES: Final[frozenset[str]] = frozenset(
    {MilestoneStatus.COMPLETED.value, MilestoneStatus.CANCELLED.value}
)

#: The Batch statuses that satisfy a Milestone's required-Batch guard. A
#: failed Batch is terminal and is not a completed one, so acceptance
#: review stays shut until it is redriven or cancelled.
_CLOSED_BATCH_STATUSES: Final[frozenset[str]] = frozenset(
    {BatchStatus.COMPLETED.value, BatchStatus.CANCELLED.value}
)

#: The Task statuses that let a Batch claim every Task in it is settled.
#: A failed Task is not settled: the Batch that carries it is not ready to
#: merge until the failure is replanned or the Task is cancelled.
_SETTLED_TASK_STATUSES: Final[frozenset[str]] = frozenset(
    {
        TaskStatus.READY_TO_INTEGRATE.value,
        TaskStatus.COMPLETED.value,
        TaskStatus.CANCELLED.value,
    }
)


class LifecycleParams(BaseModel):
    """The strict parameters every per-entity lifecycle verb takes.

    The target status is absent by construction: the verb names the move,
    so a caller cannot ask one verb to emit another verb's event.

    Attributes:
        urn: The record to move.
        expected_revision: The compare-and-swap token the caller read the
            record at.
        idempotency_key: The client's name for this request.
        actor: Who asked, as an immutable qualified principal key.
        observations: Facts about the outside world the caller presents.
        updates: Field values the target status makes facts.
        reason_code: The stable reason the move happened, which the edges
            behind a recorded-reason guard require.
        binding_refs: The exact proofs the move was taken against.
        correlation_id: The client's thread of related requests.
    """

    model_config = ConfigDict(extra="forbid")

    urn: AnyEntityUrn
    expected_revision: StrictPositiveInt
    idempotency_key: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]
    actor: PrincipalKey
    observations: tuple[ObservedFact, ...] = ()
    updates: dict[str, Any] = Field(default_factory=dict)
    reason_code: SlugStr | None = None
    binding_refs: tuple[str, ...] = ()
    correlation_id: Annotated[str, StringConstraints(strict=True, max_length=128)] | None = None


class MilestoneAcceptParams(LifecycleParams):
    """The parameters of the one verb that declares a Milestone done.

    Attributes:
        approval_receipt_ref: The sealed PendingAction receipt the
            acceptance was approved by. Absent, or addressing anything
            other than a PendingAction, refuses the request.
        acceptance_bundle: The revision the operator read before
            approving. Presented rather than read because nothing seals
            one yet; presenting it buys a caller nothing, because the
            approval read from the tree records the digest it was given
            to and a different bundle does not produce it.
    """

    approval_receipt_ref: AnyEntityUrn | None = None
    acceptance_bundle: MilestoneAcceptanceBundle | None = None


@dataclass(frozen=True, slots=True)
class LifecycleVerb:
    """One registered per-entity verb, and the edge it is allowed to take.

    Attributes:
        method: The dotted JSON-RPC name.
        kind: The entity kind the verb addresses.
        from_statuses: The statuses the verb moves a record out of. A
            record sitting anywhere else is refused, even when the
            registry has an edge from there to the same target, because
            that edge is another verb carrying another event name.
        to_status: The status the verb moves the record to.
        approval_required: Whether the request must carry a sealed
            approval receipt reference.
    """

    method: str
    kind: EntityKind
    from_statuses: tuple[LifecycleStatus, ...]
    to_status: LifecycleStatus
    approval_required: bool = False


@dataclass(frozen=True, slots=True)
class _GuardInputs:
    """Everything a computable predicate is allowed to read.

    Attributes:
        document: The locked document the mutation would land in.
        document_path: The selected generation's document, which the
            ledgers holding its compacted records resolve against.
        record: The subject, validated through its own model.
        updates: The field values the request supplies.
        reason_code: The stable reason the request carries, if any.
        ledger_statuses: What each ledger says about the records it has
            taken out of the document, filled on first read. One request
            may ask about several siblings of one collection, and reading
            the whole ledger once per sibling would make a guard cost a
            file read per reference.
    """

    document: dict[str, Any]
    document_path: Path
    record: LifecycleRecord
    updates: Mapping[str, Any]
    reason_code: str | None
    ledger_statuses: dict[Epoch2Collection, Mapping[str, str]] = field(default_factory=dict)


#: A predicate over the document and the request. ``True`` means the guard
#: holds; a predicate that cannot show its fact answers ``False``, so a
#: document missing the row it needs shuts the edge rather than opening it.
GuardComputer = Callable[[_GuardInputs], bool]


def _row_field(row: Any, name: str) -> Any:
    """Return one field of a stored row, or ``None`` when it has none.

    Sibling rows are read raw rather than validated: a predicate about
    one record must not fail because an unrelated record in the same
    collection is malformed, and every caller here compares the result
    against a known value, so an unreadable row reads as not matching.

    Args:
        row: A stored row, or whatever the document held in its place.
        name: The field to read.

    Returns:
        The field's value, or ``None``.
    """
    return row.get(name) if isinstance(row, dict) else None


def _ledger_statuses(inputs: _GuardInputs, collection: Epoch2Collection) -> Mapping[str, str]:
    """Return the standing status of every record *collection* has compacted.

    Args:
        inputs: The guard inputs, which carry the memo this fills.
        collection: The ledger collection to read.

    Returns:
        Each compacted record's status, keyed by public key. Superseded
        lines are dropped, because a corrected line is the one that
        stands. A ledger that cannot be read answers empty, so the
        predicate that asked it sees the record as absent.
    """
    cached = inputs.ledger_statuses.get(collection)
    if cached is not None:
        return cached
    statuses: Mapping[str, str]
    try:
        path = ledger_path(inputs.document_path, collection)
        statuses = {
            item.record_key: item.status for item in effective_records(read_ledger_records(path))
        }
    except LedgerError, OSError, ValueError:
        logger.warning(f"_ledger_statuses unreadable collection={collection.value}")
        statuses = {}
    inputs.ledger_statuses[collection] = statuses
    return statuses


def _status_of(inputs: _GuardInputs, collection: Epoch2Collection, key: str) -> str | None:
    """Return one record's status, from the document or from its ledger.

    A terminal record leaves the document for its ledger, so a predicate
    reading document rows alone would see a completed Batch as absent and
    shut the very edge its completion opens.

    Args:
        inputs: The guard inputs.
        collection: The collection the record is stored under.
        key: The record's public key.

    Returns:
        The status, or ``None`` when neither tier holds the record or the
        row it holds carries no readable status.
    """
    row = document_rows(inputs.document, collection).get(key)
    if row is None:
        return _ledger_statuses(inputs, collection).get(key)
    status = _row_field(row, "status")
    return status if isinstance(status, str) else None


def _supplied(inputs: _GuardInputs, name: str) -> bool:
    """Return whether the request supplies a usable value for *name*.

    Args:
        inputs: The guard inputs.
        name: The update field to look for.

    Returns:
        ``True`` when the field is present and not empty or null.
    """
    return bool(inputs.updates.get(name))


def _no_open_milestones(inputs: _GuardInputs) -> bool:
    """Return whether no Milestone under this Track is still open."""
    if not isinstance(inputs.record, Track):
        return False
    track_ref = str(inputs.record.urn)
    rows = document_rows(inputs.document, Epoch2Collection.MILESTONE)
    return not any(
        _row_field(row, "primary_track_ref") == track_ref
        and _row_field(row, "status") not in _CLOSED_MILESTONE_STATUSES
        for row in rows.values()
    )


def _track_active(inputs: _GuardInputs) -> bool:
    """Return whether this Milestone's primary Track is still active."""
    if not isinstance(inputs.record, Milestone):
        return False
    rows = document_rows(inputs.document, Epoch2Collection.TRACK)
    row = rows.get(inputs.record.primary_track_ref.entity_key)
    return bool(_row_field(row, "status") == TrackStatus.ACTIVE.value)


def _required_batches_completed(inputs: _GuardInputs) -> bool:
    """Return whether every Batch this Milestone requires has closed.

    A closed Batch is terminal and has been compacted into its ledger, so
    this predicate is the one that reads both tiers most often.
    """
    if not isinstance(inputs.record, Milestone):
        return False
    return all(
        _status_of(inputs, Epoch2Collection.BATCH, ref.entity_key) in _CLOSED_BATCH_STATUSES
        for ref in inputs.record.required_batch_refs
    )


def _tasks_ready_to_integrate(inputs: _GuardInputs) -> bool:
    """Return whether every Task in this Batch has finished or been cancelled.

    A completed or cancelled Task has left the document for its ledger,
    so the settled statuses are read from whichever tier holds each Task.
    """
    if not isinstance(inputs.record, DeliveryBatch):
        return False
    return all(
        _status_of(inputs, Epoch2Collection.TASK, ref.entity_key) in _SETTLED_TASK_STATUSES
        for ref in inputs.record.task_refs
    )


def _target_branch_pinned(inputs: _GuardInputs) -> bool:
    """Return whether the Batch knows which branch it integrates into."""
    if not isinstance(inputs.record, DeliveryBatch):
        return False
    return _supplied(inputs, "target_branch") or inputs.record.target_branch is not None


def _head_binding_pinned(inputs: _GuardInputs) -> bool:
    """Return whether the Batch records the exact head it was checked at."""
    if not isinstance(inputs.record, DeliveryBatch):
        return False
    return (
        _supplied(inputs, "current_head_binding") or inputs.record.current_head_binding is not None
    )


def _promotion_contract_complete(inputs: _GuardInputs) -> bool:
    """Return whether a promoted Task carries a Batch, criteria and a due scope."""
    if not isinstance(inputs.record, Task):
        return False
    return all(_supplied(inputs, name) for name in ("batch_ref", "criteria", "due_scope"))


def _run_bound(inputs: _GuardInputs) -> bool:
    """Return whether the Run the Task would start under is a record that exists."""
    if not isinstance(inputs.record, Task):
        return False
    named = inputs.updates.get("active_run_ref")
    if not isinstance(named, str) or not named:
        return False
    try:
        parsed = parse_qualified_urn(named)
    except IdentityError:
        return False
    if parsed.kind is not EntityKind.RUN:
        return False
    return parsed.entity_key in document_rows(inputs.document, Epoch2Collection.RUN)


def _reason_recorded(inputs: _GuardInputs) -> bool:
    """Return whether the request names why the move happened."""
    return inputs.reason_code is not None


#: Which guards this preflight can answer from the document and the
#: request. A guard absent from this map is left to the reducer, which
#: either requires an observation for it or defaults it to satisfied.
GUARD_COMPUTERS: Final[Mapping[TransitionGuard, GuardComputer]] = {
    TransitionGuard.NO_OPEN_MILESTONES: _no_open_milestones,
    TransitionGuard.TRACK_ACTIVE: _track_active,
    TransitionGuard.REQUIRED_BATCHES_COMPLETED: _required_batches_completed,
    TransitionGuard.TASKS_READY_TO_INTEGRATE: _tasks_ready_to_integrate,
    TransitionGuard.TARGET_BRANCH_PINNED: _target_branch_pinned,
    TransitionGuard.HEAD_BINDING_PINNED: _head_binding_pinned,
    TransitionGuard.PROMOTION_CONTRACT_COMPLETE: _promotion_contract_complete,
    TransitionGuard.RUN_BOUND: _run_bound,
    TransitionGuard.REASON_RECORDED: _reason_recorded,
}


#: Every registered per-entity verb, in registration order.
DOMAIN_LIFECYCLE_VERBS: Final[tuple[LifecycleVerb, ...]] = (
    LifecycleVerb(
        method="domain.track.retire",
        kind=EntityKind.TRACK,
        from_statuses=(TrackStatus.ACTIVE,),
        to_status=TrackStatus.RETIRED,
    ),
    LifecycleVerb(
        method="domain.milestone.activate",
        kind=EntityKind.MILESTONE,
        from_statuses=(MilestoneStatus.PLANNED,),
        to_status=MilestoneStatus.ACTIVE,
    ),
    LifecycleVerb(
        method="domain.milestone.open_review",
        kind=EntityKind.MILESTONE,
        from_statuses=(MilestoneStatus.ACTIVE,),
        to_status=MilestoneStatus.ACCEPTANCE_REVIEW,
    ),
    LifecycleVerb(
        method="domain.milestone.accept",
        kind=EntityKind.MILESTONE,
        from_statuses=(MilestoneStatus.ACCEPTANCE_REVIEW,),
        to_status=MilestoneStatus.COMPLETED,
        approval_required=True,
    ),
    LifecycleVerb(
        method="domain.milestone.cancel",
        kind=EntityKind.MILESTONE,
        from_statuses=(
            MilestoneStatus.PLANNED,
            MilestoneStatus.ACTIVE,
            MilestoneStatus.ACCEPTANCE_REVIEW,
        ),
        to_status=MilestoneStatus.CANCELLED,
    ),
    LifecycleVerb(
        method="domain.batch.activate",
        kind=EntityKind.BATCH,
        from_statuses=(BatchStatus.PLANNED,),
        to_status=BatchStatus.ACTIVE,
    ),
    LifecycleVerb(
        method="domain.batch.ready",
        kind=EntityKind.BATCH,
        from_statuses=(BatchStatus.ACTIVE,),
        to_status=BatchStatus.READY_TO_MERGE,
    ),
    LifecycleVerb(
        method="domain.task.promote",
        kind=EntityKind.TASK,
        from_statuses=(TaskStatus.DRAFT,),
        to_status=TaskStatus.PLANNED,
    ),
    LifecycleVerb(
        method="domain.task.start",
        kind=EntityKind.TASK,
        from_statuses=(TaskStatus.CLAIMED,),
        to_status=TaskStatus.RUNNING,
    ),
)


#: The dotted names of the registered per-entity verbs.
DOMAIN_LIFECYCLE_METHODS: Final[tuple[str, ...]] = tuple(
    verb.method for verb in DOMAIN_LIFECYCLE_VERBS
)


#: Which strict parameter model each verb parses its request through. The
#: handler and the contract test read the same table, so a verb cannot be
#: registered against one model and asserted against another.
DOMAIN_LIFECYCLE_PARAMS: Final[Mapping[str, type[LifecycleParams]]] = {
    verb.method: MilestoneAcceptParams if verb.approval_required else LifecycleParams
    for verb in DOMAIN_LIFECYCLE_VERBS
}


def _schema_refusal(
    error: ValidationError, *, params: dict[str, Any], operation: str
) -> DomainEnvelope:
    """Return the envelope of a request that does not parse.

    The pydantic detail is reduced to the offending field paths, because
    the full error text repeats the submitted values and a refusal that
    travels to terminals and daemon logs must not carry them.

    Args:
        error: What the parameter model refused.
        params: The raw request parameters, read only for the subject.
        operation: The verb the client asked for.

    Returns:
        An ``error`` envelope naming the fields to correct.
    """
    fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
    named = params.get("urn")
    subject = named[:ENTITY_REF_WIDTH] if isinstance(named, str) and named else UNNAMED_SUBJECT
    return DomainEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        status=DomainStatus.ERROR,
        operation=operation,
        errors=(
            DomainError(
                code=DomainErrorCode.SCHEMA_VALIDATION_FAILED,
                message=f"the request is not a valid {operation}; check {', '.join(fields)}",
                entity_ref=subject,
                remediation="Correct the named parameters and retry.",
            ),
        ),
    )


def _sealed_approval(params: LifecycleParams) -> QualifiedUrn | None:
    """Return the sealed approval reference the request supplies.

    Args:
        params: The already-validated request parameters.

    Returns:
        The PendingAction the acceptance was approved by, or ``None``
        when the request names none or names something that is not a
        PendingAction.
    """
    if not isinstance(params, MilestoneAcceptParams):
        return None
    ref = params.approval_receipt_ref
    if ref is None or ref.kind is not EntityKind.PENDING_ACTION:
        return None
    return ref


#: What an acceptance request is told when it names no approval at all.
_APPROVAL_ABSENT: Final = (
    f"needs the reference of a sealed {EntityKind.PENDING_ACTION.value} receipt and the "
    "request carries none"
)


def _approval_refusal(
    verb: LifecycleVerb, *, params: LifecycleParams, record: LifecycleRecord, detail: str
) -> DomainEnvelope:
    """Return the refusal of an acceptance no sealed approval covers.

    The code is outside the transaction's own refusal vocabulary, so the
    envelope is built here rather than through the transaction's refusal
    renderer. Both revisions read the same, because nothing moved.

    Args:
        verb: The verb the client asked for.
        params: The already-validated request parameters.
        record: The subject as the document holds it.
        detail: Which acceptance rule was not met.

    Returns:
        An ``error`` envelope carrying ``protected_approval_required``.
    """
    return DomainEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        status=DomainStatus.ERROR,
        operation=verb.method,
        revision_before=record.revision,
        revision_after=record.revision,
        errors=(
            DomainError(
                code=DomainErrorCode.PROTECTED_APPROVAL_REQUIRED,
                message=f"{verb.method} {detail}",
                entity_ref=str(params.urn),
                guard=TransitionGuard.ACCEPTANCE_JOURNEY_PASSED.value,
                remediation=(
                    "Seal the acceptance approval as a PendingAction against the exact bundle "
                    "revision, and retry with its receipt reference."
                ),
            ),
        ),
    )


def _unsealed_acceptance(
    document: dict[str, Any], *, params: LifecycleParams, record: LifecycleRecord
) -> str | None:
    """Return why this acceptance is not covered, or ``None`` when it is.

    The PendingAction is read from the document rather than accepted from
    the request, so a caller cannot supply the approval it needs. The
    bundle is presented, and is pinned by the digest the sealed approval
    itself recorded, so presenting a different one refuses rather than
    passes.

    Args:
        document: The locked document the mutation would land in.
        params: The already-validated request parameters.
        record: The subject as the document holds it.

    Returns:
        The sentence naming the unmet rule, or ``None``.
    """
    ref = _sealed_approval(params)
    if ref is None:
        return _APPROVAL_ABSENT
    if not isinstance(params, MilestoneAcceptParams) or not isinstance(record, Milestone):
        return "reads an acceptance bundle only for a Milestone"
    bundle = params.acceptance_bundle
    if bundle is None:
        return (
            f"holds {ref.entity_key} against the acceptance bundle it approved, and the "
            "request presents none"
        )
    row = document_rows(document, Epoch2Collection.PENDING_ACTION).get(ref.entity_key)
    if row is None:
        return f"finds no {ref.entity_key} in the tree, so nothing sealed the approval"
    try:
        action = PendingAction.model_validate(row)
    except ValidationError:
        return f"cannot read {ref.entity_key} as a pending action"
    try:
        require_sealed_acceptance(
            action, bundle=bundle, milestone_ref=record.urn, status=record.status
        )
    except AcceptanceRefusedError as error:
        return str(error)
    return None


def _kind_mismatch(verb: LifecycleVerb, *, params: LifecycleParams) -> DomainEnvelope:
    """Return the refusal of a verb pointed at another entity's record.

    The transaction derives the machine it evaluates from the URN rather
    than from the verb, so a verb handed a foreign URN whose machine
    happens to share the target status would commit that machine's edge
    and emit that machine's event name. Refusing the mismatch here is what
    keeps a verb's name and its event in agreement.

    Args:
        verb: The verb the client asked for.
        params: The already-validated request parameters.

    Returns:
        An ``error`` envelope carrying ``identity_kind_mismatch``. Neither
        revision is filled, because no record was read.
    """
    refusal = TransactionRefusedError(
        code=TransactionRefusalCode.IDENTITY_KIND_MISMATCH,
        detail=(
            f"{verb.method} addresses a {verb.kind.value} but the request names a "
            f"{params.urn.kind.value}"
        ),
        entity_ref=str(params.urn),
        remediation=f"Name a {verb.kind.value} URN, or call that entity's own verb.",
    )
    return refused_envelope(refusal, operation=verb.method)


def _illegal_edge(
    verb: LifecycleVerb, *, params: LifecycleParams, record: LifecycleRecord
) -> DomainEnvelope:
    """Return the refusal of a verb asked to move a record it does not move.

    Args:
        verb: The verb the client asked for.
        params: The already-validated request parameters.
        record: The subject as the document holds it.

    Returns:
        An ``error`` envelope carrying ``illegal_transition``.
    """
    admitted = ", ".join(sorted(str(status) for status in verb.from_statuses))
    refusal = TransactionRefusedError(
        code=TransactionRefusalCode.ILLEGAL_TRANSITION,
        detail=(
            f"{verb.method} moves a record out of {admitted}, but the record is {record.status!s}"
        ),
        entity_ref=str(params.urn),
        remediation=DENIAL_REMEDIATION[DenialCode.ILLEGAL_TRANSITION],
        revision=record.revision,
    )
    return refused_envelope(refusal, operation=verb.method)


def _guard_refusal(
    verb: LifecycleVerb,
    *,
    params: LifecycleParams,
    record: LifecycleRecord,
    guard: TransitionGuard,
) -> DomainEnvelope:
    """Return the refusal one unmet computable predicate decides.

    The shape matches what the registry would have reported had it been
    able to evaluate the predicate: the wire code says a guard failed and
    the guard's own name travels beside it.

    Args:
        verb: The verb the client asked for.
        params: The already-validated request parameters.
        record: The subject as the document holds it.
        guard: The first guard of the edge that does not hold.

    Returns:
        An ``error`` envelope carrying ``transition_guard_failed``.
    """
    entity = LIFECYCLE_ENTITIES[verb.kind]
    refusal = TransactionRefusedError(
        code=TransactionRefusalCode.TRANSITION_GUARD_FAILED,
        detail=(
            f"{entity.value} {record.status!s} -> {verb.to_status!s} blocked by guard "
            f"{guard.value!r}"
        ),
        entity_ref=str(params.urn),
        guard=guard.value,
        remediation=DENIAL_REMEDIATION[GUARD_DENIALS[guard]],
        revision=record.revision,
    )
    return refused_envelope(refusal, operation=verb.method)


def _receipt_filed(context: Epoch2RootContext, *, key: str) -> bool:
    """Return whether this root already holds a receipt for *key*.

    An unreadable receipt reads as filed. Whether the request already ran
    is then unknown, and the transaction owns the typed refusal for that,
    so the preflight steps aside rather than judging a request that may
    already have moved the record.

    Args:
        context: The native context of the addressed root.
        key: The client's idempotency key.

    Returns:
        ``True`` when a receipt exists or cannot be read.
    """
    try:
        stored = read_idempotency_receipt(context, namespaced_key=context.idempotency_key(key))
    except ValueError:
        return True
    return stored is not None


def _subject(
    document: dict[str, Any], *, verb: LifecycleVerb, params: LifecycleParams
) -> LifecycleRecord | None:
    """Return the record the preflight may judge, or ``None``.

    An absent row, a row that does not validate and a row at another
    revision are all the transaction's refusals: it re-reads under its own
    locks and reports them against what it found there, so reporting them
    twice from a read taken earlier would only add a way for the two
    answers to differ.

    Args:
        document: The locked document.
        verb: The verb the client asked for.
        params: The already-validated request parameters.

    Returns:
        The validated subject, or ``None`` when the transaction owns the
        answer.
    """
    row = document_rows(document, ENTITY_COLLECTIONS[verb.kind]).get(params.urn.entity_key)
    if row is None:
        return None
    try:
        record = RECORD_CLASSES[LIFECYCLE_ENTITIES[verb.kind]].model_validate(row)
    except ValueError:
        return None
    return record if record.revision == params.expected_revision else None


def _unmet_guard(
    verb: LifecycleVerb,
    *,
    document: dict[str, Any],
    document_path: Path,
    record: LifecycleRecord,
    params: LifecycleParams,
) -> TransitionGuard | None:
    """Return the first computable predicate of this edge that is false.

    Guards are evaluated in the registry's declaration order, so the
    surfaced denial is the first thing an operator has to fix.

    Args:
        verb: The verb the client asked for.
        document: The locked document.
        document_path: The document's own path, which the ledgers of its
            compacted records resolve against.
        record: The validated subject.
        params: The already-validated request parameters.

    Returns:
        The unmet guard, or ``None`` when every predicate this preflight
        can answer holds.
    """
    row = row_for(LIFECYCLE_ENTITIES[verb.kind], record.status, verb.to_status)
    if row is None:
        return None
    inputs = _GuardInputs(
        document=document,
        document_path=document_path,
        record=record,
        updates=params.updates,
        reason_code=params.reason_code,
    )
    for guard in row.guards:
        computer = GUARD_COMPUTERS.get(guard)
        if computer is not None and not computer(inputs):
            return guard
    return None


def _preflight(
    context: Epoch2RootContext, *, verb: LifecycleVerb, params: LifecycleParams
) -> DomainEnvelope | None:
    """Return the refusal this request earns before the transaction runs.

    Nothing is written on any path through this function: the document is
    read under the root's own session and released again, and every answer
    is either a refusal envelope or ``None``.

    Args:
        context: The native context of the addressed root.
        verb: The verb the client asked for.
        params: The already-validated request parameters.

    Returns:
        The refusal envelope, or ``None`` when the transaction should run.

    Raises:
        NativeAuthorityRequiredError: The tree left epoch 2.
        MigrationDualAuthorityError: The tree's select is not whole.
        LockTimeout: A lock stayed held past the lock timeout.
    """
    if params.urn.kind is not verb.kind:
        return _kind_mismatch(verb, params=params)
    if _receipt_filed(context, key=params.idempotency_key):
        return None
    with context.session([params.urn]) as session:
        document = session.read_document()
        document_path = session.document_path
    record = _subject(document, verb=verb, params=params)
    if record is None:
        return None
    if record.status not in verb.from_statuses:
        return _illegal_edge(verb, params=params, record=record)
    if verb.approval_required:
        unsealed = _unsealed_acceptance(document, params=params, record=record)
        if unsealed is not None:
            return _approval_refusal(verb, params=params, record=record, detail=unsealed)
    guard = _unmet_guard(
        verb, document=document, document_path=document_path, record=record, params=params
    )
    if guard is None:
        return None
    return _guard_refusal(verb, params=params, record=record, guard=guard)


def _transition_request(verb: LifecycleVerb, params: LifecycleParams) -> TransitionRequest:
    """Return the transaction request one verb's parameters stand for.

    A sealed approval reference joins the binding refs rather than being
    dropped, so the approval an acceptance was taken against is readable
    from the committed event and is part of the digest a retry is matched
    against.

    Args:
        verb: The verb the client asked for.
        params: The already-validated request parameters.

    Returns:
        The strict transition request.
    """
    approval = _sealed_approval(params)
    binding_refs = (
        params.binding_refs if approval is None else (*params.binding_refs, str(approval))
    )
    return TransitionRequest(
        urn=params.urn,
        to_status=str(verb.to_status),
        expected_revision=params.expected_revision,
        idempotency_key=params.idempotency_key,
        actor=params.actor,
        observations=params.observations,
        updates=params.updates,
        reason_code=params.reason_code,
        binding_refs=binding_refs,
        correlation_id=params.correlation_id,
    )


async def _run_verb(
    ctx: MethodContext,
    params: dict[str, Any],
    authority: RootAuthority,
    *,
    verb: LifecycleVerb,
) -> dict[str, Any]:
    """Commit one per-entity lifecycle verb on the addressed epoch-2 tree.

    Args:
        ctx: Server context, which owns the per-root native contexts and
            the subscription bus.
        params: The request parameters, less the routing key the fence
            already consumed.
        authority: The epoch-2 answer the fence resolved for the tree.
        verb: The verb this handler was registered for.

    Returns:
        The machine envelope, as a JSON-mode mapping. A commit whose
        post-commit publish did not reach the projection still answers
        ``ok`` with its receipt and carries the ``projection_degraded``
        warning. A retry the transaction answered from its receipt store
        publishes nothing, because the original commit already did.
    """
    model = DOMAIN_LIFECYCLE_PARAMS[verb.method]
    try:
        request_params = model.model_validate(
            {key: value for key, value in params.items() if key != REPO_ROOT_PARAM}
        )
    except ValidationError as error:
        return _schema_refusal(error, params=params, operation=verb.method).model_dump(mode="json")
    context = ctx.native_root_context(authority.root)
    refused = await asyncio.to_thread(_preflight, context, verb=verb, params=request_params)
    if refused is not None:
        logger.info(f"_run_verb refused method={verb.method} code={refused.errors[0].code.value}")
        return refused.model_dump(mode="json")
    try:
        committed = await asyncio.to_thread(
            run_transaction,
            context=context,
            request=_transition_request(verb, request_params),
            now=datetime.now(UTC),
        )
    except TransactionRefusedError as refusal:
        logger.info(f"_run_verb refused method={verb.method} code={refusal.code.value}")
        return refused_envelope(refusal, operation=verb.method).model_dump(mode="json")
    warnings: tuple[str, ...] = ()
    # A replayed answer carries no envelope, so a retry never republishes
    # the event the original commit already fanned out.
    if committed.envelope is not None and not publish_projection(ctx.bus, committed.envelope):
        warnings = (PROJECTION_DEGRADED,)
    return accepted_envelope(
        committed.receipt, operation=verb.method, warnings=warnings
    ).model_dump(mode="json")


def _register_verb(verb: LifecycleVerb) -> None:
    """Register one verb's fenced handler under its dotted name.

    Args:
        verb: The verb to register.

    Raises:
        ValueError: The name is already registered.
    """

    async def handler(
        ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
    ) -> dict[str, Any]:
        return await _run_verb(ctx, params, authority, verb=verb)

    handler.__name__ = verb.method.replace(".", "_")
    handler.__doc__ = f"Commit the {verb.method} edge on the addressed epoch-2 tree."
    native_mutator(verb.method)(handler)


for _verb in DOMAIN_LIFECYCLE_VERBS:
    _register_verb(_verb)


__all__ = [
    "DOMAIN_LIFECYCLE_METHODS",
    "DOMAIN_LIFECYCLE_PARAMS",
    "DOMAIN_LIFECYCLE_VERBS",
    "GUARD_COMPUTERS",
    "GuardComputer",
    "LifecycleParams",
    "LifecycleVerb",
    "MilestoneAcceptParams",
]
