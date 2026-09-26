"""Opening and sealing the approval a Milestone acceptance is taken on.

``domain.milestone.accept`` refuses unless the tree holds a sealed
PendingAction bound to the digest of the bundle it is presented. These two
verbs are what put that row in the tree.

``runtime.delivery.open_acceptance_approval``: verify asks the question.

Once every Batch of a Milestone in acceptance review stands on a verified
head, the verify stage presents what the acceptance journey showed. The
verb files that journey as revision one of the Milestone's acceptance
bundle when the ledger holds none, resolves every ``EVD-####`` it cites,
and opens one protected approval bound to the bundle's digest. Asking
again about the same bytes finds the standing question and writes
nothing, so re-verifying the same Batches never files a duplicate.

``runtime.delivery.seal_acceptance_approval``: the operator answers it.

The first answer seals the waiting question at the revision it was given
against, and its own disposition rides the same commit as the seal. The
resolver is typed a person, and has to be the actor asking, so a caller
cannot seal on somebody else's behalf. The receipt the answer cites is
resolved against the evidence ledger. Every answer once one already has
-- a different principal's, or the same principal choosing differently
-- records that principal's own disposition beside the standing seal,
without moving it, and reports the ``superseded`` outcome and the
winning choice instead of a refusal. The winning answer retried under a
fresh idempotency key writes nothing and returns the original receipt
in the reply; the same answer, winning or losing, retried under its own
idempotency key returns the original receipt through the ordinary
replay path.

Every commit -- opening, the winning seal, and a losing principal's own
disposition -- goes through the transaction's
:func:`~eawf.runtime.daemon.epoch2_transaction._persist` step and the
bundle line through
:func:`~eawf.runtime.daemon.epoch2_transaction.commit_ledger_append`, so
each writes one WAL intent, one document rewrite, one receipt and one
firehose row, and a refusal writes nothing at all.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from eawf.kernel.delivery.acceptance import (
    AcceptanceBundleLedger,
    AcceptanceStepOutcome,
    EvidenceView,
    MilestoneAcceptanceBundle,
)
from eawf.kernel.delivery.integration import IdempotencyKey
from eawf.kernel.identity import EntityKind, QualifiedUrn, parse_qualified_urn
from eawf.kernel.state.canonical_sequence import CanonicalSequenceAllocator
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey, StrictPositiveInt
from eawf.kernel.state.epoch2.batch import DeliveryBatch
from eawf.kernel.state.epoch2.milestone import Milestone
from eawf.kernel.state.epoch2.pending_action import (
    ActionPrincipal,
    AnswerOutcome,
    AnswerResult,
    HumanPrincipal,
    OptionId,
    PendingAction,
    PendingActionStatus,
    PrincipalDispositionRow,
)
from eawf.kernel.state.epoch2.urns import AnyEntityUrn, EvidenceUrn, MilestoneUrn
from eawf.kernel.state.epoch2.values import ExactRevisionBinding
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import LedgerRecord, effective_records, read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_create import CREATE_EVENT_NAMESPACE
from eawf.runtime.daemon.epoch2_recovery import publish_projection
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
    commit_ledger_append,
)
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.delivery_acceptance import BUNDLE_KEY_PREFIX
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_mutator
from eawf.workflow.delivery.acceptance import (
    AcceptanceRefusal,
    AcceptanceRefusedError,
    acceptance_evidence,
    evidence_view,
)
from eawf.workflow.delivery.acceptance_approval import (
    ApprovalRefusal,
    ApprovalRefusedError,
    acceptance_question,
    first_bundle,
    next_action_key,
    require_same_bundle,
    require_verified_batches,
    seal_question,
    standing_question,
)

logger = logging.getLogger(__name__)


#: The verb verify calls once a Milestone's Batches are verified.
DELIVERY_OPEN_APPROVAL_METHOD: Final = "runtime.delivery.open_acceptance_approval"

#: The verb an operator's answer seals the question through.
DELIVERY_SEAL_APPROVAL_METHOD: Final = "runtime.delivery.seal_acceptance_approval"

#: The event a new question is admitted under, in the namespace every
#: native create names its event in.
APPROVAL_OPENED_EVENT: Final = (
    f"{CREATE_EVENT_NAMESPACE}.{Epoch2Collection.PENDING_ACTION.value}.created"
)

#: The event an answer is sealed under. Sealing is not a lifecycle edge of
#: a driven machine, so it names its event in a namespace of its own.
APPROVAL_SEALED_EVENT: Final = f"resolution.{Epoch2Collection.PENDING_ACTION.value}.sealed"

#: The event a losing answer's own disposition is recorded under. The
#: seal itself does not move -- ``from_status`` and ``to_status`` both
#: read ``SEALED`` -- so this names its own event rather than reusing
#: :data:`APPROVAL_SEALED_EVENT`, which would claim a second seal.
APPROVAL_ANSWER_RECORDED_EVENT: Final = (
    f"resolution.{Epoch2Collection.PENDING_ACTION.value}.answer_recorded"
)

#: Version of the payload both events carry.
APPROVAL_EVENT_SCHEMA_VERSION: Final = "1"

#: Which transaction code each seal refusal reaches the wire as. The finer
#: approval code travels in the refusal's ``guard``.
_SEAL_REFUSAL_CODES: Final[Mapping[ApprovalRefusal, TransactionRefusalCode]] = {
    ApprovalRefusal.ACTION_NOT_WAITING: TransactionRefusalCode.ILLEGAL_TRANSITION,
    ApprovalRefusal.ACTION_STALE: TransactionRefusalCode.REVISION_CONFLICT,
}


class ActionCommitRequest(BaseModel):
    """What one pending-action commit is filed and replayed under.

    Attributes:
        urn: The pending action the commit writes.
        idempotency_key: The name the commit's receipt is filed under.
        actor: Who asked.
        operation: Whether the commit opens or seals the question.
        detail: The operation's own parameters, which the replay digest
            covers so one key cannot name two different answers.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    urn: AnyEntityUrn
    idempotency_key: IdempotencyKey
    actor: PrincipalKey
    operation: Literal["open", "seal"]
    detail: dict[str, Any]


class ApprovalOpenParams(BaseModel):
    """Params of :data:`DELIVERY_OPEN_APPROVAL_METHOD`.

    Attributes:
        urn: The Milestone whose acceptance is asked about.
        actor: Who asked.
        requested_by: The principal the question is recorded as asked by.
        steps: What the acceptance journey showed.
        accepted_binding: The exact tree the acceptance is taken on.
    """

    model_config = ConfigDict(extra="forbid")

    urn: MilestoneUrn
    actor: PrincipalKey
    requested_by: ActionPrincipal
    steps: tuple[AcceptanceStepOutcome, ...] = Field(min_length=1)
    accepted_binding: ExactRevisionBinding


class ApprovalSealParams(BaseModel):
    """Params of :data:`DELIVERY_SEAL_APPROVAL_METHOD`.

    Attributes:
        urn: The pending action being answered.
        expected_revision: The revision the answer was given against.
        idempotency_key: The client's name for this request.
        actor: Who answered.
        resolver: The person who answered, typed so an agent cannot be
            named; it must be the actor.
        option_id: The answer they chose.
        receipt_ref: The evidence row recording the answer.
        correlation_id: The client's thread of related requests.
    """

    model_config = ConfigDict(extra="forbid")

    urn: AnyEntityUrn
    expected_revision: StrictPositiveInt
    idempotency_key: IdempotencyKey
    actor: PrincipalKey
    resolver: HumanPrincipal
    option_id: OptionId
    receipt_ref: EvidenceUrn
    correlation_id: Annotated[str, StringConstraints(strict=True, max_length=128)] | None = None

    @model_validator(mode="after")
    def _answers_one_action_as_oneself(self) -> Self:
        """Require a pending-action URN and a resolver who is the actor.

        Raises:
            ValueError: The URN addresses another kind, or the actor seals
                in somebody else's name.
        """
        if self.urn.kind is not EntityKind.PENDING_ACTION:
            raise ValueError(f"urn addresses a {self.urn.kind.value}, not a pending action")
        if self.resolver.principal_id != self.actor:
            raise ValueError(
                f"actor {self.actor} cannot seal an answer {self.resolver.principal_id} gave"
            )
        return self


class ApprovalAnswer(BaseModel):
    """What opening or sealing a question answers with.

    Attributes:
        action_ref: The pending action the answer is about.
        status: The status it stands in.
        revision: The revision it stands at.
        bundle_revision: The bundle revision the question is bound to.
        bundle_digest: The digest the question is bound to.
        acceptance_bundle: The bundle itself, which is what
            ``domain.milestone.accept`` must be presented.
        created: Whether this call wrote the row, rather than finding it.
        canonical_sequence: The sequence of the commit, or ``None`` when
            nothing was committed.
        outcome: What a seal answer achieved --
            :attr:`~eawf.kernel.state.epoch2.pending_action.AnswerOutcome.SEALED`
            or ``SUPERSEDED``. ``None`` for an open, which answers no
            question yet.
        reason: One sentence an operator reads.
        receipt_ref: The evidence row the action stands sealed on, or
            ``None`` before any seal. Set from the winner's own seal even
            when this particular answer lost the race, so a duplicate or
            superseded answer still learns which receipt the question is
            resolved on.
        dispositions: Every principal's own outcome of the action so far,
            the loser's row beside the winner's.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_ref: str
    status: str
    revision: int
    bundle_revision: int | None = None
    bundle_digest: str | None = None
    acceptance_bundle: dict[str, Any] | None = None
    created: bool
    canonical_sequence: int | None = None
    outcome: str | None = None
    reason: str
    receipt_ref: str | None = None
    dispositions: tuple[PrincipalDispositionRow, ...] = ()


@dataclass(frozen=True, slots=True)
class ApprovalCommit:
    """An answer beside the firehose rows waiting to be published.

    Attributes:
        answer: What the caller answers with.
        envelopes: The rows the commits appended, published once the
            locks are released; empty when nothing was written.
    """

    answer: ApprovalAnswer
    envelopes: tuple[Envelope, ...] = ()


def _refused(code: ApprovalRefusal | AcceptanceRefusal, detail: str) -> DaemonValidationError:
    """Return the wire form of one open refusal."""
    return DaemonValidationError(f"validation_failed: {code.value}: {detail}")


def _action_urn(container: QualifiedUrn, key: str) -> QualifiedUrn:
    """Return the URN of pending action *key* in the repository *container* names."""
    prefix = str(container).rsplit("/", 2)[0]
    return parse_qualified_urn(f"{prefix}/{EntityKind.PENDING_ACTION.value}/{key}")


def _milestone_of(document: dict[str, Any], urn: MilestoneUrn) -> Milestone:
    """Return the live Milestone the document holds under *urn*.

    Raises:
        TransactionRefusedError: The document holds no readable Milestone.
    """
    row = document_rows(document, Epoch2Collection.MILESTONE).get(urn.entity_key)
    if row is not None:
        try:
            return Milestone.model_validate(row)
        except ValidationError:
            logger.warning(f"milestone unreadable key={urn.entity_key!r}")
    raise TransactionRefusedError(
        code=TransactionRefusalCode.IDENTITY_NOT_FOUND,
        detail=f"the document holds no readable live milestone keyed {urn.entity_key!r}",
        entity_ref=str(urn),
        remediation="Name a Milestone the tree holds in acceptance review.",
    )


def _batches(session: RootSession, document: dict[str, Any]) -> tuple[DeliveryBatch, ...]:
    """Return every Batch the tree holds, from the document and the ledger.

    A finished Batch is compacted out of the document into its ledger, so
    both tiers are read; a Batch missing from the answer would be a Batch
    whose verification nobody checked.

    Raises:
        DaemonValidationError: A stored Batch row does not read back, so
            whether the Milestone's work is verified cannot be decided.
    """
    live = document_rows(document, Epoch2Collection.BATCH)
    payloads = list(live.values())
    lines = effective_records(read_ledger_records(session.ledger_path(Epoch2Collection.BATCH)))
    payloads += [
        item.payload
        for item in lines
        if item.record_key.startswith("BAT-") and item.record_key not in live
    ]
    try:
        return tuple(DeliveryBatch.model_validate(item) for item in payloads)
    except ValidationError as error:
        raise _refused(
            ApprovalRefusal.BATCHES_UNVERIFIED,
            f"a stored batch does not read back ({error.error_count()} error(s)), so whether "
            "the milestone's work is verified is unknown",
        ) from error


def _bundle_ledger(session: RootSession, urn: MilestoneUrn) -> AcceptanceBundleLedger:
    """Return the Milestone's filed bundle revisions, oldest first.

    Raises:
        DaemonValidationError: A filed revision does not read, or the
            chain of digests is broken.
    """
    wanted = str(urn)
    payloads = [
        item.payload
        for item in read_ledger_records(session.ledger_path(Epoch2Collection.MILESTONE))
        if item.record_key.startswith(BUNDLE_KEY_PREFIX)
        and item.payload.get("milestone_ref") == wanted
    ]
    try:
        return AcceptanceBundleLedger.model_validate({"milestone_ref": wanted, "bundles": payloads})
    except ValidationError as error:
        raise _refused(
            ApprovalRefusal.BUNDLE_DIVERGED,
            f"the bundle revisions of {urn.entity_key} do not read back as one chain",
        ) from error


def _evidence(session: RootSession) -> EvidenceView:
    """Return the view over every ``EVD-####`` the evidence ledger holds.

    Raises:
        DaemonValidationError: An evidence line does not read as a row.
    """
    payloads = [
        item.payload
        for item in read_ledger_records(session.ledger_path(Epoch2Collection.EVIDENCE))
        if item.record_key.startswith("EVD-")
    ]
    try:
        return evidence_view(payloads)
    except ValidationError as error:
        raise _refused(
            AcceptanceRefusal.EVIDENCE_UNHELD, "the evidence ledger does not read back"
        ) from error


def _actions(document: dict[str, Any]) -> dict[str, PendingAction]:
    """Return every pending action the document holds that reads back.

    A row that does not validate is not a question anybody can answer, so
    it is left out of the standing-question search; its key still counts
    as taken when the next key is allocated.
    """
    held: dict[str, PendingAction] = {}
    for key, row in document_rows(document, Epoch2Collection.PENDING_ACTION).items():
        try:
            held[key] = PendingAction.model_validate(row)
        except ValidationError:
            logger.warning(f"pending action unreadable key={key!r}")
    return held


def _envelope(
    *,
    request: ActionCommitRequest,
    before: PendingAction | None,
    after: PendingAction,
    event_name: str,
    sequence: int,
    now: datetime,
) -> Envelope:
    """Return the one firehose row a pending-action commit appends."""
    urn = request.urn
    event_id = f"evt-{uuid.uuid4().hex}"
    return Envelope(
        id=event_id,
        kind=StoreKind.EVENT,
        scope_id=str(urn),
        created_at=now,
        summary=f"{event_name} {urn.entity_key} {after.status.value}",
        payload={
            "schema_version": APPROVAL_EVENT_SCHEMA_VERSION,
            "name": event_name,
            "event_id": event_id,
            "occurred_at": now.isoformat(),
            "workspace_ref": urn.workspace_key,
            "project_ref": urn.project_key,
            "entity_ref": str(urn),
            "subject_ref": str(after.subject_ref),
            "from_status": None if before is None else before.status.value,
            "to_status": after.status.value,
            "revision_before": None if before is None else before.revision,
            "revision_after": after.revision,
            "actor_ref": request.actor,
            "idempotency_key": request.idempotency_key,
            "canonical_sequence": sequence,
        },
    )


def _commit_action(
    *,
    context: Epoch2RootContext,
    session: RootSession,
    request: ActionCommitRequest,
    before: PendingAction | None,
    after: PendingAction,
    event_name: str,
    now: datetime,
) -> CommittedTransaction:
    """Write *after* into the document through the transaction's commit step.

    Raises:
        TransactionRefusedError: The row carries free text with a leak
            shape. Nothing was written.
    """
    document = session.read_document()
    allocator = CanonicalSequenceAllocator.recover(
        workspace_key=request.urn.workspace_key, high_water_mark=_high_water_mark(document)
    )
    with allocator.transaction() as sequences:
        sequence = sequences.allocate()
        new_document = copy.deepcopy(document)
        new_document.setdefault(Epoch2Collection.PENDING_ACTION.value, {})[after.id] = (
            after.model_dump(mode="json")
        )
        new_document[CANONICAL_SEQUENCE_KEY] = sequence
        plan = _CommitPlan(
            record=before,
            successor=after,
            event_name=event_name,
            sequence=sequence,
            document=document,
            new_document=new_document,
            envelope=_envelope(
                request=request,
                before=before,
                after=after,
                event_name=event_name,
                sequence=sequence,
                now=now,
            ),
            compaction=None,
        )
        receipt = _persist(context=context, session=session, plan=plan, request=request, now=now)
    return CommittedTransaction(receipt=receipt, envelope=plan.envelope)


def _answer(
    action: PendingAction,
    *,
    urn: QualifiedUrn,
    bundle: MilestoneAcceptanceBundle | None,
    receipt: MutationReceipt | None,
    outcome: AnswerOutcome | None = None,
    reason: str,
) -> ApprovalAnswer:
    """Build the answer one open or seal returns."""
    return ApprovalAnswer(
        action_ref=str(urn),
        status=action.status.value,
        revision=action.revision,
        bundle_revision=None if bundle is None else bundle.revision,
        bundle_digest=action.bundle_digest,
        acceptance_bundle=None if bundle is None else bundle.model_dump(mode="json"),
        created=receipt is not None,
        canonical_sequence=None if receipt is None else receipt.canonical_sequence,
        outcome=None if outcome is None else outcome.value,
        reason=reason,
        receipt_ref=None if action.receipt_ref is None else str(action.receipt_ref),
        dispositions=action.dispositions,
    )


def _bundle_to_ask_about(
    session: RootSession, *, milestone: Milestone, args: ApprovalOpenParams, now: datetime
) -> tuple[MilestoneAcceptanceBundle, bool]:
    """Return the bundle revision the question binds to, and whether it is new.

    Raises:
        DaemonValidationError: A Batch is not verified, the presentation
            differs from the filed head, the journey breaks a bundle rule,
            or the bundle cites evidence the tree does not hold.
    """
    document = session.read_document()
    try:
        batch_refs = require_verified_batches(milestone, _batches(session, document))
        head = _bundle_ledger(session, args.urn).head
        if head is None:
            bundle = first_bundle(
                milestone,
                steps=args.steps,
                accepted_binding=args.accepted_binding,
                batch_refs=batch_refs,
                at=now,
            )
        else:
            bundle = require_same_bundle(
                head,
                steps=args.steps,
                accepted_binding=args.accepted_binding,
                batch_refs=batch_refs,
            )
        acceptance_evidence(_evidence(session), bundle)
    except (ApprovalRefusedError, AcceptanceRefusedError) as error:
        raise DaemonValidationError(f"validation_failed: {error}") from error
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        raise DaemonValidationError(
            f"validation_failed: schema_validation_failed: check {', '.join(fields)}"
        ) from error
    return bundle, head is None


def open_acceptance_approval(
    context: Epoch2RootContext, args: ApprovalOpenParams, *, now: datetime
) -> ApprovalCommit:
    """Open the protected approval a verified Milestone's acceptance needs.

    Args:
        context: The native context of the tree the Milestone lives in.
        args: The validated request.
        now: When the question was asked, which is also when a first
            bundle revision is sealed.

    Returns:
        The question, the bundle it is bound to, and the rows to publish.
        A question already standing on the same bytes is returned with
        nothing written.

    Raises:
        TransactionRefusedError: The Milestone is not a live row, or the
            question carries free text with a leak shape.
        DaemonValidationError: A Batch is not verified, the presentation
            differs from the filed bundle, the journey breaks a bundle
            rule, or it cites evidence the tree does not hold.
    """
    with context.session([str(args.urn)]) as session:
        milestone = _milestone_of(session.read_document(), args.urn)
        bundle, new_bundle = _bundle_to_ask_about(session, milestone=milestone, args=args, now=now)
        held = _actions(session.read_document())
        standing = standing_question(held.values(), bundle=bundle)
        if standing is not None:
            logger.info(f"open_acceptance_approval standing action={standing.id}")
            return ApprovalCommit(
                answer=_answer(
                    standing,
                    urn=standing.urn,
                    bundle=bundle,
                    receipt=None,
                    reason=f"{standing.id} already asks about revision {bundle.revision}",
                )
            )
        envelopes: list[Envelope] = []
        if new_bundle:
            envelopes.append(_append_bundle(session, bundle))
        taken = document_rows(session.read_document(), Epoch2Collection.PENDING_ACTION)
        key = next_action_key(taken)
        urn = _action_urn(args.urn, key)
        action = acceptance_question(
            key=key, urn=urn, bundle=bundle, requested_by=args.requested_by, at=now
        )
        request = ActionCommitRequest(
            urn=urn,
            idempotency_key=action.idempotency_key,
            actor=args.actor,
            operation="open",
            detail={"bundle_digest": bundle.digest()},
        )
        committed = _commit_action(
            context=context,
            session=session,
            request=request,
            before=None,
            after=action,
            event_name=APPROVAL_OPENED_EVENT,
            now=now,
        )
    assert committed.envelope is not None, "a fresh commit always carries its firehose row"
    logger.info(
        f"open_acceptance_approval milestone={args.urn.entity_key} action={action.id} "
        f"revision={bundle.revision} sequence={committed.receipt.canonical_sequence}"
    )
    return ApprovalCommit(
        answer=_answer(
            action,
            urn=urn,
            bundle=bundle,
            receipt=committed.receipt,
            reason=f"{action.id} asks the operator to accept revision {bundle.revision}",
        ),
        envelopes=(*envelopes, committed.envelope),
    )


def _append_bundle(session: RootSession, bundle: MilestoneAcceptanceBundle) -> Envelope:
    """File one bundle revision as a line of the Milestone ledger."""
    return commit_ledger_append(
        session,
        LedgerRecord(
            collection=Epoch2Collection.MILESTONE,
            record_key=f"{BUNDLE_KEY_PREFIX}{bundle.revision:04d}-{bundle.milestone_ref.entity_key}",
            status=f"revision-{bundle.revision}",
            recorded_at=bundle.sealed_at,
            payload=bundle.model_dump(mode="json"),
        ),
    )


def _action_at(document: dict[str, Any], urn: QualifiedUrn) -> PendingAction:
    """Return the pending action the document holds under *urn*.

    Raises:
        TransactionRefusedError: The document holds no readable action.
    """
    row = document_rows(document, Epoch2Collection.PENDING_ACTION).get(urn.entity_key)
    if row is not None:
        try:
            return PendingAction.model_validate(row)
        except ValidationError:
            logger.warning(f"pending action unreadable key={urn.entity_key!r}")
    raise TransactionRefusedError(
        code=TransactionRefusalCode.IDENTITY_NOT_FOUND,
        detail=f"the document holds no readable pending action keyed {urn.entity_key!r}",
        entity_ref=str(urn),
        remediation="Answer a question the tree holds.",
    )


def _seal_request(args: ApprovalSealParams) -> ActionCommitRequest:
    """Return the commit request one seal is filed and replayed under."""
    return ActionCommitRequest(
        urn=args.urn,
        idempotency_key=args.idempotency_key,
        actor=args.actor,
        operation="seal",
        detail=args.model_dump(mode="json", exclude={"urn", "idempotency_key", "actor"}),
    )


def _sealed(action: PendingAction, *, args: ApprovalSealParams, now: datetime) -> PendingAction:
    """Return *action* sealed with the answer *args* carries, or refuse.

    Raises:
        TransactionRefusedError: The action is not waiting, moved past the
            answered revision, or does not offer the chosen option.
    """
    try:
        return seal_question(
            action,
            expected_revision=args.expected_revision,
            resolver=args.resolver,
            option_id=args.option_id,
            receipt_ref=args.receipt_ref,
            at=now,
        )
    except ApprovalRefusedError as error:
        raise TransactionRefusedError(
            code=_SEAL_REFUSAL_CODES.get(error.code, TransactionRefusalCode.ILLEGAL_TRANSITION),
            detail=str(error),
            entity_ref=str(args.urn),
            guard=error.code.value,
            remediation="Re-read the question; a sealed one is answered, so ask a new one.",
            revision=action.revision,
        ) from error
    except ValidationError as error:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
            detail=f"{action.id} offers {', '.join(action.option_ids)}, not {args.option_id!r}",
            entity_ref=str(args.urn),
            remediation="Choose one of the options the question offers.",
            revision=action.revision,
        ) from error


def _answered_after_seal(
    action: PendingAction, *, args: ApprovalSealParams, now: datetime
) -> AnswerResult:
    """Return the result of an answer that reaches an action the tree already sealed.

    This alone writes nothing: the seal the answer would have made was
    already made, by this same request under an earlier idempotency key
    or by somebody else's, so the race is reported rather than retried
    or refused. The caller persists the loser's own disposition when the
    result is superseded; the identical winning answer retried needs no
    write at all.

    Raises:
        TransactionRefusedError: The option named is not one the question
            offers.
    """
    try:
        return action.answer(
            expected_revision=args.expected_revision,
            resolver=args.resolver,
            option_id=args.option_id,
            receipt_ref=args.receipt_ref,
            at=now,
        )
    except ValueError as error:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
            detail=f"{action.id} offers {', '.join(action.option_ids)}, not {args.option_id!r}",
            entity_ref=str(args.urn),
            remediation="Choose one of the options the question offers.",
            revision=action.revision,
        ) from error


def seal_acceptance_approval(
    context: Epoch2RootContext, args: ApprovalSealParams, *, now: datetime
) -> ApprovalCommit:
    """Seal a waiting question with the answer the operator gave.

    Args:
        context: The native context of the tree the question lives in.
        args: The validated request.
        now: When the answer was given.

    Returns:
        The sealed question and the row to publish, the winner's own
        disposition riding the same commit. The winning answer retried
        under its own idempotency key, and a losing answer retried under
        its, each return the question as it stands having written
        nothing a second time. A conflicting answer reached for the first
        time -- a different principal's, or the same principal choosing
        differently -- records that principal's own disposition beside
        the standing seal without moving it, and every answer once one
        already has reports the ``superseded`` or ``sealed`` outcome and
        the winning choice rather than a refusal.

    Raises:
        TransactionRefusedError: The tree holds no such question, a
            still-waiting question moved past the answered revision, the
            option is not offered, the receipt is not an evidence row the
            tree holds, or the idempotency key already committed
            different parameters. Nothing was written.
    """
    request = _seal_request(args)
    with context.session([str(args.urn)]) as session:
        replayed = _replayed_receipt(context, request=request)
        action = _action_at(session.read_document(), args.urn)
        if action.status is PendingActionStatus.SEALED:
            result = _answered_after_seal(action, args=args, now=now)
            logger.info(
                f"seal_acceptance_approval action={action.id} outcome={result.outcome.value} "
                f"option={args.option_id} replayed={replayed is not None}"
            )
            envelopes: tuple[Envelope, ...] = ()
            if replayed is None and result.outcome is AnswerOutcome.SUPERSEDED:
                committed = _commit_action(
                    context=context,
                    session=session,
                    request=request,
                    before=action,
                    after=result.action,
                    event_name=APPROVAL_ANSWER_RECORDED_EVENT,
                    now=now,
                )
                envelopes = (committed.envelope,)
            return ApprovalCommit(
                answer=_answer(
                    result.action,
                    urn=args.urn,
                    bundle=None,
                    receipt=None,
                    outcome=result.outcome,
                    reason=(
                        f"{action.id} is answered {result.option_id!r} by "
                        f"{result.resolution_actor.principal_id}"
                    ),
                ),
                envelopes=envelopes,
            )
        if _evidence(session).row(args.receipt_ref.entity_key) is None:
            raise TransactionRefusedError(
                code=TransactionRefusalCode.IDENTITY_NOT_FOUND,
                detail=f"the evidence ledger holds no {args.receipt_ref.entity_key} to record "
                "the answer",
                entity_ref=str(args.urn),
                guard=AcceptanceRefusal.EVIDENCE_UNHELD.value,
                remediation="Record the answer as evidence first, then cite its key.",
                revision=action.revision,
            )
        sealed = _sealed(action, args=args, now=now)
        recorded = sealed.with_disposition(
            principal_id=args.resolver.principal_id,
            outcome=AnswerOutcome.SEALED,
            option_id=args.option_id,
        )
        committed = _commit_action(
            context=context,
            session=session,
            request=request,
            before=action,
            after=recorded,
            event_name=APPROVAL_SEALED_EVENT,
            now=now,
        )
    assert committed.envelope is not None, "a fresh commit always carries its firehose row"
    logger.info(
        f"seal_acceptance_approval action={recorded.id} option={args.option_id} "
        f"sequence={committed.receipt.canonical_sequence}"
    )
    return ApprovalCommit(
        answer=_answer(
            recorded,
            urn=args.urn,
            bundle=None,
            receipt=committed.receipt,
            outcome=AnswerOutcome.SEALED,
            reason=f"{recorded.id} was answered {args.option_id!r}",
        ),
        envelopes=(committed.envelope,),
    )


def _validated[T: BaseModel](model: type[T], params: dict[str, Any]) -> T:
    """Validate request params, dropping the key the fence already used.

    Raises:
        DaemonValidationError: The request does not parse. The detail is
            reduced to field paths so a submitted value never reaches a log.
    """
    try:
        return model.model_validate(
            {key: value for key, value in params.items() if key != REPO_ROOT_PARAM}
        )
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        raise DaemonValidationError(
            f"validation_failed: schema_validation_failed: check {', '.join(fields)}"
        ) from error


def _published(ctx: MethodContext, envelopes: Iterable[Envelope]) -> None:
    """Publish the committed rows after the locks are released."""
    for envelope in envelopes:
        if not publish_projection(ctx.bus, envelope):
            logger.warning(f"approval publish degraded event={envelope.payload.get('name')!r}")


@native_mutator(DELIVERY_OPEN_APPROVAL_METHOD)
async def _open_acceptance_approval(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Open the protected approval a verified Milestone's acceptance needs."""
    args = _validated(ApprovalOpenParams, params)
    context = ctx.native_root_context(authority.root)
    commit = await asyncio.to_thread(open_acceptance_approval, context, args, now=datetime.now(UTC))
    _published(ctx, commit.envelopes)
    return commit.answer.model_dump(mode="json")


@native_mutator(DELIVERY_SEAL_APPROVAL_METHOD)
async def _seal_acceptance_approval(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Seal a waiting acceptance question with the operator's answer."""
    args = _validated(ApprovalSealParams, params)
    context = ctx.native_root_context(authority.root)
    commit = await asyncio.to_thread(seal_acceptance_approval, context, args, now=datetime.now(UTC))
    _published(ctx, commit.envelopes)
    return commit.answer.model_dump(mode="json")


__all__ = [
    "APPROVAL_ANSWER_RECORDED_EVENT",
    "APPROVAL_EVENT_SCHEMA_VERSION",
    "APPROVAL_OPENED_EVENT",
    "APPROVAL_SEALED_EVENT",
    "DELIVERY_OPEN_APPROVAL_METHOD",
    "DELIVERY_SEAL_APPROVAL_METHOD",
    "ActionCommitRequest",
    "ApprovalAnswer",
    "ApprovalCommit",
    "ApprovalOpenParams",
    "ApprovalSealParams",
    "open_acceptance_approval",
    "seal_acceptance_approval",
]
