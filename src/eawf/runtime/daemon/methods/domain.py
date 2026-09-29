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
predicates are computed by :mod:`eawf.runtime.daemon.methods.domain_guards`,
against the document the mutation would land in, and an unmet one refuses
the request with the same ``transition_guard_failed`` code and the same
guard name the registry would have reported had it been able to evaluate
it.

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

Task completion is the other verb with a requirement of its own. The
integrated binding a completed Task records is derived, never presented:
``domain.task.complete`` runs the completion assessment the caller's
proofs are judged by, refuses unless every leg is proved on the Batch
head, and binds exactly that head. A Batch's merge is three moves on
three facts -- ``merge`` records the authorisation, ``observe_merge``
needs a landed read-back filed by the reconciliation verb, and
``complete`` needs that read-back to match the head the Batch pinned.

``domain.task.release`` hands a claimed Task back to ``PLANNED``. Only
the principal holding the claim may release it, never while a Run is
queued or working against the Task, and each release files a
:class:`~eawf.kernel.state.epoch2.task.TaskReleaseRecord` naming its cause
and actor, so an operator abort and a routine replan read differently
afterwards.

Two further verbs carry the bookkeeping the fenced epoch-1 verbs used to:
``domain.legacy.advance`` moves a record the cutover imported along its
closed edge table, and ``domain.record.append`` files an audit, decision or
artifact in the generation's ledger. Both are thin: the decisions are in
:mod:`eawf.kernel.migration.epoch2.continuation` and the locked reads,
gate runs and commits in :mod:`eawf.runtime.daemon.legacy_continuation`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from eawf.kernel.delivery.acceptance import MilestoneAcceptanceBundle
from eawf.kernel.identity import EntityKind, QualifiedUrn
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey, ShaStr, SlugStr, StrictPositiveInt
from eawf.kernel.state.epoch2.batch import BatchStatus
from eawf.kernel.state.epoch2.milestone import Milestone, MilestoneStatus
from eawf.kernel.state.epoch2.pending_action import PendingAction
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.kernel.state.epoch2.task import TaskStatus
from eawf.kernel.state.epoch2.track import TrackStatus
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
    MutationReceipt,
    TransactionRefusalCode,
    TransactionRefusedError,
    TransitionRequest,
    run_transaction,
)
from eawf.runtime.daemon.legacy_continuation import (
    CommittedContinuation,
    LegacyAdvanceRequest,
    RecordAppendRequest,
    advance_legacy,
    append_record,
)
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.delivery_completion import (
    TaskCompletionInputs,
    TaskCompletionParams,
    completion_binding,
)
from eawf.runtime.daemon.methods.domain_envelope import (
    ENVELOPE_SCHEMA_VERSION,
    DomainEnvelope,
    DomainError,
    DomainErrorCode,
    DomainStatus,
    accepted_envelope,
    refused_envelope,
    schema_refusal,
)
from eawf.runtime.daemon.methods.domain_guards import (
    GUARD_COMPUTERS,
    GuardInputs,
    milestone_watchlist,
)
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_mutator
from eawf.runtime.daemon.run_capture_updates import bind_run_capture
from eawf.runtime.daemon.task_release import TASK_RELEASE_METHOD, file_task_release
from eawf.workflow.delivery.acceptance import AcceptanceRefusedError, require_sealed_acceptance
from eawf.workflow.lifecycle.epoch2 import LifecycleRecord

logger = logging.getLogger(__name__)


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


class TaskCompleteParams(LifecycleParams):
    """The parameters of the one verb that declares a Task delivered.

    The integrated binding is absent by construction: the daemon derives
    it from the completion assessment, so a caller names the commit it
    believes carries the Task and the judgment that commit must pass,
    never the proof itself.

    Attributes:
        integrated_commit: The commit the caller says the Batch head
            delivers. Refused unless it is the head the Batch's selected
            generation stands on.
        assessment: The half of the completion judgment no native record
            holds -- the base, the report verdict, the gates, the proof
            receipts and the runtime facts they ran under.
    """

    integrated_commit: ShaStr
    assessment: TaskCompletionInputs


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
        binds_integration: Whether the daemon derives the integrated
            binding from a passing completion assessment before the move.
    """

    method: str
    kind: EntityKind
    from_statuses: tuple[LifecycleStatus, ...]
    to_status: LifecycleStatus
    approval_required: bool = False
    binds_integration: bool = False


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
        method="domain.batch.merge",
        kind=EntityKind.BATCH,
        from_statuses=(BatchStatus.READY_TO_MERGE,),
        to_status=BatchStatus.MERGING,
    ),
    LifecycleVerb(
        method="domain.batch.observe_merge",
        kind=EntityKind.BATCH,
        from_statuses=(BatchStatus.MERGING,),
        to_status=BatchStatus.MERGED_PENDING_RECONCILIATION,
    ),
    LifecycleVerb(
        method="domain.batch.complete",
        kind=EntityKind.BATCH,
        from_statuses=(BatchStatus.MERGED_PENDING_RECONCILIATION,),
        to_status=BatchStatus.COMPLETED,
    ),
    LifecycleVerb(
        method="domain.task.promote",
        kind=EntityKind.TASK,
        from_statuses=(TaskStatus.DRAFT,),
        to_status=TaskStatus.PLANNED,
    ),
    LifecycleVerb(
        method="domain.task.demote",
        kind=EntityKind.TASK,
        from_statuses=(TaskStatus.PLANNED,),
        to_status=TaskStatus.DRAFT,
    ),
    LifecycleVerb(
        method="domain.task.claim",
        kind=EntityKind.TASK,
        from_statuses=(TaskStatus.PLANNED,),
        to_status=TaskStatus.CLAIMED,
    ),
    LifecycleVerb(
        method=TASK_RELEASE_METHOD,
        kind=EntityKind.TASK,
        from_statuses=(TaskStatus.CLAIMED,),
        to_status=TaskStatus.PLANNED,
    ),
    LifecycleVerb(
        method="domain.task.start",
        kind=EntityKind.TASK,
        from_statuses=(TaskStatus.CLAIMED,),
        to_status=TaskStatus.RUNNING,
    ),
    LifecycleVerb(
        method="domain.task.ready",
        kind=EntityKind.TASK,
        from_statuses=(TaskStatus.RUNNING,),
        to_status=TaskStatus.READY_TO_INTEGRATE,
    ),
    LifecycleVerb(
        method="domain.task.complete",
        kind=EntityKind.TASK,
        from_statuses=(TaskStatus.READY_TO_INTEGRATE,),
        to_status=TaskStatus.COMPLETED,
        binds_integration=True,
    ),
    LifecycleVerb(
        method="domain.run.start",
        kind=EntityKind.RUN,
        from_statuses=(RunStatus.QUEUED,),
        to_status=RunStatus.RUNNING,
    ),
    LifecycleVerb(
        method="domain.run.finish",
        kind=EntityKind.RUN,
        from_statuses=(RunStatus.RUNNING,),
        to_status=RunStatus.COMPLETED,
    ),
    LifecycleVerb(
        method="domain.run.fail",
        kind=EntityKind.RUN,
        from_statuses=(RunStatus.RUNNING,),
        to_status=RunStatus.FAILED,
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
    verb.method: (
        MilestoneAcceptParams
        if verb.approval_required
        else TaskCompleteParams
        if verb.binds_integration
        else LifecycleParams
    )
    for verb in DOMAIN_LIFECYCLE_VERBS
}


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
    inputs = GuardInputs(
        document=document,
        document_path=document_path,
        record=record,
        updates=params.updates,
        reason_code=params.reason_code,
        actor=params.actor,
        binding_refs=params.binding_refs,
    )
    for guard in row.guards:
        computer = GUARD_COMPUTERS.get(guard)
        if computer is not None and not computer(inputs):
            return guard
    return None


def _preflight(
    context: Epoch2RootContext, *, verb: LifecycleVerb, params: LifecycleParams
) -> tuple[DomainEnvelope | None, int | None]:
    """Return the refusal this request earns before the transaction runs.

    Nothing is written on any path through this function: the document is
    read under the root's own session and released again, and every answer
    is either a refusal envelope or ``None``.

    Args:
        context: The native context of the addressed root.
        verb: The verb the client asked for.
        params: The already-validated request parameters.

    Returns:
        The refusal envelope, or ``None`` when the transaction should run,
        beside the revision the subject was read at, or ``None`` when the
        preflight left the subject to the transaction unread.

    Raises:
        NativeAuthorityRequiredError: The tree left epoch 2.
        MigrationDualAuthorityError: The tree's select is not whole.
        LockTimeout: A lock stayed held past the lock timeout.
    """
    if params.urn.kind is not verb.kind:
        return _kind_mismatch(verb, params=params), None
    if _receipt_filed(context, key=params.idempotency_key):
        return None, None
    with context.session([params.urn]) as session:
        document = session.read_document()
        document_path = session.document_path
    record = _subject(document, verb=verb, params=params)
    if record is None:
        return None, None
    if record.status not in verb.from_statuses:
        return _illegal_edge(verb, params=params, record=record), record.revision
    if verb.approval_required:
        unsealed = _unsealed_acceptance(document, params=params, record=record)
        if unsealed is not None:
            refusal = _approval_refusal(verb, params=params, record=record, detail=unsealed)
            return refusal, record.revision
    guard = _unmet_guard(
        verb, document=document, document_path=document_path, record=record, params=params
    )
    if guard is None:
        return None, record.revision
    return _guard_refusal(verb, params=params, record=record, guard=guard), record.revision


def _integration_refusal(
    verb: LifecycleVerb,
    *,
    params: LifecycleParams,
    code: DomainErrorCode,
    detail: str,
    revision: int | None,
) -> DomainEnvelope:
    """Return the refusal of a completion the assessment does not prove.

    Args:
        verb: The verb the client asked for.
        params: The already-validated request parameters.
        code: ``proof_stale`` when a leg must run again at the head, and
            ``transition_guard_failed`` when the assessment refused.
        detail: What the assessment found.
        revision: The revision the subject was read at, when it was read.

    Returns:
        An ``error`` envelope naming the integrated-binding guard.
    """
    return DomainEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        status=DomainStatus.ERROR,
        operation=verb.method,
        revision_before=revision,
        revision_after=revision,
        errors=(
            DomainError(
                code=code,
                message=f"{verb.method} {detail}"[:1000],
                entity_ref=str(params.urn),
                guard=TransitionGuard.INTEGRATED_BINDING_PINNED.value,
                remediation=DENIAL_REMEDIATION[DenialCode.TASK_INTEGRATION_UNPROVEN],
            ),
        ),
    )


def _bind_integration(
    context: Epoch2RootContext,
    *,
    verb: LifecycleVerb,
    params: LifecycleParams,
    revision: int | None,
) -> LifecycleParams | DomainEnvelope:
    """Return *params* carrying the binding a passing assessment derives.

    A binding the caller supplied is replaced rather than trusted: the
    edge's proof is whatever the assessment finds on the Batch head.

    Args:
        context: The native context of the addressed root.
        verb: The verb the client asked for.
        params: The already-validated request parameters.
        revision: The revision the subject was read at, when it was read.

    Returns:
        The request with ``integrated_binding`` set, or the refusal of an
        assessment that refused or left a leg unproven.
    """
    if not isinstance(params, TaskCompleteParams):
        raise TypeError(f"{verb.method} parses through TaskCompleteParams")
    inputs = params.assessment
    args = TaskCompletionParams.model_validate(
        {**inputs.model_dump(mode="json"), "urn": str(params.urn), "actor": params.actor}
    )
    try:
        answer, binding = completion_binding(
            context, args, integrated_commit=params.integrated_commit
        )
    except DaemonValidationError as error:
        detail = str(error).removeprefix("validation_failed: ")
        return _integration_refusal(
            verb,
            params=params,
            code=DomainErrorCode.TRANSITION_GUARD_FAILED,
            detail=detail,
            revision=revision,
        )
    if binding is None:
        return _integration_refusal(
            verb,
            params=params,
            code=DomainErrorCode.PROOF_STALE,
            detail=f"{answer.reason}; rerun {', '.join(answer.rerun_gate_ids) or 'none'}, "
            f"unavailable {', '.join(answer.unavailable_gate_ids) or 'none'}",
            revision=revision,
        )
    updates = {**params.updates, "integrated_binding": binding.model_dump(mode="json")}
    return params.model_copy(update={"updates": updates})


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


def _bundle_binding(params: LifecycleParams) -> LifecycleParams:
    """Return *params* carrying the accepted binding its acceptance bundle names.

    An acceptance is taken on exactly the tree the sealed bundle records,
    so a request presenting the bundle and no binding means that one; a
    binding the request does name is left for the seal check to hold
    against the bundle rather than overwritten.
    """
    if not isinstance(params, MilestoneAcceptParams) or params.acceptance_bundle is None:
        return params
    if "accepted_binding" in params.updates:
        return params
    binding = params.acceptance_bundle.accepted_binding.model_dump(mode="json")
    return params.model_copy(update={"updates": {**params.updates, "accepted_binding": binding}})


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
        return schema_refusal(error, params=params, operation=verb.method).model_dump(mode="json")
    request_params = _bundle_binding(request_params)
    context = ctx.native_root_context(authority.root)
    refused, revision = await asyncio.to_thread(
        _preflight, context, verb=verb, params=request_params
    )
    if refused is None and verb.binds_integration:
        bound = await asyncio.to_thread(
            _bind_integration, context, verb=verb, params=request_params, revision=revision
        )
        if isinstance(bound, DomainEnvelope):
            refused = bound
        else:
            request_params = bound
    if refused is None and isinstance(verb.to_status, RunStatus):
        try:
            captured = await asyncio.to_thread(
                bind_run_capture,
                context,
                urn=request_params.urn,
                to_status=verb.to_status,
                updates=request_params.updates,
            )
        except ValidationError as error:
            return schema_refusal(error, params=params, operation=verb.method).model_dump(
                mode="json"
            )
        request_params = request_params.model_copy(update={"updates": captured})
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
    warnings += await _after_commit(ctx, context, verb, request_params, committed.receipt)
    return accepted_envelope(
        committed.receipt, operation=verb.method, warnings=warnings
    ).model_dump(mode="json")


async def _after_commit(
    ctx: MethodContext,
    context: Epoch2RootContext,
    verb: LifecycleVerb,
    params: LifecycleParams,
    receipt: MutationReceipt,
) -> tuple[str, ...]:
    """Do what one committed verb owes after its commit, and return its warnings.

    An activated Milestone reads its advisory WIP signals. A released Task
    files its release record -- on a replay too, because a daemon that died
    between the commit and the append left the release without its record.

    Returns:
        The watchlist lines, or ``projection_degraded`` when a filed release
        record's row did not reach the projection; empty otherwise.
    """
    if verb.to_status is MilestoneStatus.ACTIVE:
        return await asyncio.to_thread(_activation_watchlist, context, params.urn)
    if verb.method != TASK_RELEASE_METHOD:
        return ()
    filed = await asyncio.to_thread(
        file_task_release,
        context,
        task_ref=params.urn,
        cause=str(params.reason_code),
        actor=params.actor,
        receipt=receipt,
    )
    return () if filed is None or publish_projection(ctx.bus, filed) else (PROJECTION_DEGRADED,)


def _activation_watchlist(context: Epoch2RootContext, urn: QualifiedUrn) -> tuple[str, ...]:
    """Return the advisory WIP signals a just-activated Milestone raises.

    Read after the commit, so the count includes the activation itself.

    Args:
        context: The native context of the addressed root.
        urn: The activated Milestone.

    Returns:
        The watchlist lines, or nothing when the Milestone row is not
        readable or its Track is within its advisory ceiling.
    """
    with context.session([urn]) as session:
        document = session.read_document()
    row = document_rows(document, Epoch2Collection.MILESTONE).get(urn.entity_key)
    try:
        milestone = Milestone.model_validate(row)
    except ValidationError:
        return ()
    return milestone_watchlist(document, milestone)


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


#: The verb that moves an imported Milestone, Batch or Task after the
#: cutover. An imported row fails every native model, so it cannot take a
#: per-entity verb; this one takes the closed legacy edge table instead.
DOMAIN_LEGACY_ADVANCE: Final = "domain.legacy.advance"

#: The verb that files an audit, decision or artifact in the generation's
#: ledger once the epoch-1 verbs that used to write them are fenced.
DOMAIN_RECORD_APPEND: Final = "domain.record.append"


async def _run_continuation[RequestT: BaseModel, ReceiptT: BaseModel](
    ctx: MethodContext,
    params: dict[str, Any],
    authority: RootAuthority,
    *,
    operation: str,
    model: type[RequestT],
    runner: Callable[..., CommittedContinuation[ReceiptT]],
) -> dict[str, Any]:
    """Parse, run and answer one post-cutover bookkeeping verb.

    Args:
        ctx: Server context, which owns the per-root native contexts and
            the subscription bus.
        params: The request parameters, less the routing key the fence
            already consumed.
        authority: The epoch-2 answer the fence resolved for the tree.
        operation: The verb's dotted name.
        model: The strict parameter model.
        runner: The library call that commits the verb.

    Returns:
        The machine envelope, as a JSON-mode mapping, carrying the
        runner's receipt on success. An imported row has no revision, so
        neither revision field is filled.
    """
    try:
        request = model.model_validate(
            {key: value for key, value in params.items() if key != REPO_ROOT_PARAM}
        )
    except ValidationError as error:
        return schema_refusal(error, params=params, operation=operation).model_dump(mode="json")
    context = ctx.native_root_context(authority.root)
    try:
        committed = await asyncio.to_thread(runner, context, request, now=datetime.now(UTC))
    except TransactionRefusedError as refusal:
        logger.info(f"_run_continuation refused method={operation} code={refusal.code.value}")
        return refused_envelope(refusal, operation=operation).model_dump(mode="json")
    degraded = [
        envelope for envelope in committed.envelopes if not publish_projection(ctx.bus, envelope)
    ]
    return DomainEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        status=DomainStatus.OK,
        operation=operation,
        result=committed.receipt.model_dump(mode="json"),
        warnings=(PROJECTION_DEGRADED,) if degraded else (),
    ).model_dump(mode="json")


@native_mutator(DOMAIN_LEGACY_ADVANCE)
async def _domain_legacy_advance(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Move one imported row along the closed legacy edge table."""
    return await _run_continuation(
        ctx,
        params,
        authority,
        operation=DOMAIN_LEGACY_ADVANCE,
        model=LegacyAdvanceRequest,
        runner=advance_legacy,
    )


@native_mutator(DOMAIN_RECORD_APPEND)
async def _domain_record_append(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Append one audit, decision or artifact to the generation's ledger."""
    return await _run_continuation(
        ctx,
        params,
        authority,
        operation=DOMAIN_RECORD_APPEND,
        model=RecordAppendRequest,
        runner=append_record,
    )


__all__ = [
    "DOMAIN_LEGACY_ADVANCE",
    "DOMAIN_LIFECYCLE_METHODS",
    "DOMAIN_LIFECYCLE_PARAMS",
    "DOMAIN_LIFECYCLE_VERBS",
    "DOMAIN_RECORD_APPEND",
    "LifecycleParams",
    "LifecycleVerb",
    "MilestoneAcceptParams",
    "TaskCompleteParams",
]
