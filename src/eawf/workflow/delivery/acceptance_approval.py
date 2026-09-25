"""Asking the operator to accept a Milestone once its Batches are verified.

Milestone acceptance reads a sealed PendingAction bound to the exact
acceptance bundle; this module decides when that question may be asked,
what it asks, and whether an answer may seal it. It writes nothing:
committing the question and its seal is the daemon's job.

When. A Milestone is asked about only in acceptance review and only once
every Batch it owns stands at a verified head. A Batch that is still in
work, or a required Batch the tree does not hold at all, keeps the
question closed, because an approval asked for early is an approval of
work nobody has checked.

What. The question is a protected approval bound to the digest of the
bundle revision it shows, offering approve, decline and repair. The same
bundle always digests the same way, so asking twice finds the question
already standing rather than filing a second one.

Whether an answer seals it. Only a waiting question at the revision the
answer was given against is sealed. A second seal therefore refuses: the
question is no longer waiting, and nothing leaves ``SEALED``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Final

from eawf.kernel.delivery.acceptance import (
    FIRST_BUNDLE_REVISION,
    AcceptanceStepOutcome,
    MilestoneAcceptanceBundle,
)
from eawf.kernel.identity import EntityKind, format_entity_key
from eawf.kernel.state.epoch2.batch import BatchStatus, DeliveryBatch
from eawf.kernel.state.epoch2.milestone import Milestone, MilestoneStatus
from eawf.kernel.state.epoch2.pending_action import (
    AgentPrincipal,
    HumanPrincipal,
    OptionEffect,
    PendingAction,
    PendingActionKind,
    PendingActionOption,
    PendingActionStatus,
)
from eawf.kernel.state.epoch2.urns import BatchUrn, EvidenceUrn, PendingActionUrn
from eawf.kernel.state.epoch2.values import ExactRevisionBinding

logger = logging.getLogger(__name__)


#: The Batch statuses that stand on a verified head. A Batch reaches
#: ``READY_TO_MERGE`` only once its verification cycle cleared, and every
#: later status is that same head moving towards the target branch.
VERIFIED_BATCH_STATUSES: Final[frozenset[BatchStatus]] = frozenset(
    {
        BatchStatus.READY_TO_MERGE,
        BatchStatus.MERGING,
        BatchStatus.MERGED_PENDING_RECONCILIATION,
        BatchStatus.COMPLETED,
    }
)

#: The three answers an acceptance question offers. Repair is separate
#: from decline because asking for changes opens a successor bundle,
#: while declining leaves the Milestone where it stands.
ACCEPTANCE_OPTIONS: Final[tuple[PendingActionOption, ...]] = (
    PendingActionOption(
        option_id="approve", label="Accept the Milestone", effect=OptionEffect.APPROVE
    ),
    PendingActionOption(option_id="decline", label="Do not accept it", effect=OptionEffect.DECLINE),
    PendingActionOption(
        option_id="repair", label="Ask for changes", effect=OptionEffect.REQUEST_REPAIR
    ),
)

#: How many hex digits of the bundle digest name the question's
#: idempotency key: enough that two bundles of one Milestone do not
#: collide, short enough to stay readable in a log line.
_DIGEST_PREFIX_LENGTH: Final = 16


class ApprovalRefusal(StrEnum):
    """The stable codes opening or sealing an acceptance question refuses with."""

    MILESTONE_NOT_IN_REVIEW = "acceptance_milestone_not_in_review"
    BATCHES_UNVERIFIED = "acceptance_batches_unverified"
    BUNDLE_DIVERGED = "acceptance_bundle_diverged"
    ACTION_NOT_WAITING = "acceptance_action_not_waiting"
    ACTION_STALE = "acceptance_action_stale"


class ApprovalRefusedError(ValueError):
    """One acceptance question was refused, with the code that says which rule.

    Attributes:
        code: The stable refusal code a caller routes on.
    """

    def __init__(self, code: ApprovalRefusal, detail: str) -> None:
        """Keep the code beside the sentence an operator reads.

        Args:
            code: The stable refusal code.
            detail: One sentence naming what was refused and why.
        """
        self.code = code
        super().__init__(f"{code.value}: {detail}")


def require_verified_batches(
    milestone: Milestone, batches: Iterable[DeliveryBatch]
) -> tuple[BatchUrn, ...]:
    """Return the Milestone's Batches, refusing unless every one is verified.

    A cancelled Batch delivers nothing and is left out, unless the
    Milestone names it as required: a required Batch that was cancelled
    is a required Batch that is missing.

    Args:
        milestone: The Milestone the question would be asked about.
        batches: Every Batch the tree holds, from either storage tier.

    Returns:
        The Milestone's live Batch URNs, sorted by key.

    Raises:
        ApprovalRefusedError: The Milestone is not in acceptance review,
            owns no live Batch, a required Batch is absent, or a Batch
            does not stand on a verified head.
    """
    key = milestone.key
    if milestone.status is not MilestoneStatus.ACCEPTANCE_REVIEW:
        raise ApprovalRefusedError(
            ApprovalRefusal.MILESTONE_NOT_IN_REVIEW,
            f"{key} is {milestone.status.value}, and acceptance is asked about in "
            f"{MilestoneStatus.ACCEPTANCE_REVIEW.value}",
        )
    owned = {
        item.key: item
        for item in batches
        if str(item.milestone_ref) == str(milestone.urn)
        and item.status is not BatchStatus.CANCELLED
    }
    absent = sorted({ref.entity_key for ref in milestone.required_batch_refs} - set(owned))
    if absent:
        raise ApprovalRefusedError(
            ApprovalRefusal.BATCHES_UNVERIFIED,
            f"{key} requires {', '.join(absent)}, which the tree holds no live batch for",
        )
    if not owned:
        raise ApprovalRefusedError(
            ApprovalRefusal.BATCHES_UNVERIFIED,
            f"{key} owns no batch, so nothing it delivers has been verified",
        )
    unverified = sorted(
        f"{batch_key} ({item.status.value})"
        for batch_key, item in owned.items()
        if item.status not in VERIFIED_BATCH_STATUSES
    )
    if unverified:
        raise ApprovalRefusedError(
            ApprovalRefusal.BATCHES_UNVERIFIED,
            f"{key} has batches that stand on no verified head: {', '.join(unverified)}",
        )
    return tuple(owned[batch_key].urn for batch_key in sorted(owned))


def first_bundle(
    milestone: Milestone,
    *,
    steps: Sequence[AcceptanceStepOutcome],
    accepted_binding: ExactRevisionBinding,
    batch_refs: Sequence[BatchUrn],
    at: datetime,
) -> MilestoneAcceptanceBundle:
    """Return revision one of the Milestone's acceptance bundle.

    Args:
        milestone: The Milestone the bundle is for.
        steps: What the acceptance journey showed.
        accepted_binding: The exact tree the acceptance is taken on.
        batch_refs: The verified Batches the bundle covers.
        at: When the bundle was sealed.

    Returns:
        The first revision.

    Raises:
        pydantic.ValidationError: The steps break a bundle rule, such as
            a repeated step id or a passing step citing no evidence.
    """
    return MilestoneAcceptanceBundle.model_validate(
        {
            "milestone_ref": milestone.urn,
            "revision": FIRST_BUNDLE_REVISION,
            "accepted_binding": accepted_binding.model_dump(),
            "required_batch_refs": [str(ref) for ref in batch_refs],
            "steps": [item.model_dump() for item in steps],
            "sealed_at": at,
        }
    )


def require_same_bundle(
    head: MilestoneAcceptanceBundle,
    *,
    steps: Sequence[AcceptanceStepOutcome],
    accepted_binding: ExactRevisionBinding,
    batch_refs: Sequence[BatchUrn],
) -> MilestoneAcceptanceBundle:
    """Return the filed head, refusing a presentation that differs from it.

    A filed revision is never rewritten, so a caller presenting different
    steps, a different binding or a different Batch set is describing a
    bundle that does not exist; a changed journey is a repair.

    Args:
        head: The newest revision the Milestone ledger holds.
        steps: What the caller presents the journey as.
        accepted_binding: The tree the caller presents.
        batch_refs: The verified Batches as read now.

    Returns:
        *head*, unchanged.

    Raises:
        ApprovalRefusedError: The presentation differs from the head.
    """
    presented = (
        tuple(item.model_dump(mode="json") for item in steps),
        accepted_binding.model_dump(mode="json"),
        tuple(str(ref) for ref in batch_refs),
    )
    filed = (
        tuple(item.model_dump(mode="json") for item in head.steps),
        head.accepted_binding.model_dump(mode="json"),
        tuple(str(ref) for ref in head.required_batch_refs),
    )
    if presented != filed:
        raise ApprovalRefusedError(
            ApprovalRefusal.BUNDLE_DIVERGED,
            f"revision {head.revision} of {head.milestone_ref.entity_key} is already filed with "
            "another journey, binding or batch set; ask for a repair to revise it",
        )
    return head


def approval_idempotency_key(bundle: MilestoneAcceptanceBundle) -> str:
    """Return the idempotency key the question about *bundle* is filed under.

    The key is derived from the Milestone and the bundle digest, so asking
    about the same bytes twice names the same request.
    """
    digest = bundle.digest().removeprefix("sha256:")
    return f"accept-{bundle.milestone_ref.entity_key}-{digest[:_DIGEST_PREFIX_LENGTH]}"


def standing_question(
    actions: Iterable[PendingAction], *, bundle: MilestoneAcceptanceBundle
) -> PendingAction | None:
    """Return the question already asked about exactly *bundle*, if one was.

    Args:
        actions: Every pending action the tree holds.
        bundle: The bundle revision the question would be about.

    Returns:
        The protected approval on the same Milestone and digest, or
        ``None`` when none was asked.
    """
    digest = bundle.digest()
    subject = str(bundle.milestone_ref)
    for item in actions:
        if (
            item.kind is PendingActionKind.PROTECTED_APPROVAL
            and str(item.subject_ref) == subject
            and item.bundle_digest == digest
        ):
            return item
    return None


def next_action_key(taken: Iterable[str]) -> str:
    """Return the ``ACT-####`` key after the highest one *taken*.

    Args:
        taken: Every pending-action key the tree holds.

    Returns:
        The next key, ``ACT-0001`` for an empty tree.

    Raises:
        IdentityError: The next ordinal no longer fits the key grammar.
    """
    ordinals = [int(key.split("-", 1)[1]) for key in taken if key.startswith("ACT-")]
    return format_entity_key(EntityKind.PENDING_ACTION, max(ordinals, default=0) + 1)


def acceptance_question(
    *,
    key: str,
    urn: PendingActionUrn,
    bundle: MilestoneAcceptanceBundle,
    requested_by: HumanPrincipal | AgentPrincipal,
    at: datetime,
) -> PendingAction:
    """Return the protected approval asking to accept exactly *bundle*.

    The question is born ``WAITING``: committing it into the document is
    what puts it in front of the attention projection, so there is no
    moment at which it exists and nobody could be shown it.

    Args:
        key: The ``ACT-####`` key the question is filed under.
        urn: The question's own canonical address, in the Milestone's
            repository.
        bundle: The revision the operator is asked to accept.
        requested_by: Who asks.
        at: When the question was asked.

    Returns:
        The waiting question.
    """
    milestone_key = bundle.milestone_ref.entity_key
    return PendingAction(
        id=key,
        urn=urn,
        kind=PendingActionKind.PROTECTED_APPROVAL,
        subject_ref=bundle.milestone_ref,
        question=f"Accept {milestone_key} on revision {bundle.revision} of its acceptance bundle?",
        bundle_digest=bundle.digest(),
        options=ACCEPTANCE_OPTIONS,
        idempotency_key=approval_idempotency_key(bundle),
        status=PendingActionStatus.WAITING,
        requested_by=requested_by,
        created_at=at,
        updated_at=at,
    )


def seal_question(
    action: PendingAction,
    *,
    expected_revision: int,
    resolver: HumanPrincipal,
    option_id: str,
    receipt_ref: EvidenceUrn,
    at: datetime,
) -> PendingAction:
    """Return *action* sealed with the operator's answer, or refuse.

    Args:
        action: The question as the tree holds it.
        expected_revision: The revision the answer was given against.
        resolver: The person who answered.
        option_id: The answer they chose.
        receipt_ref: The evidence row recording the answer.
        at: When the answer was given.

    Returns:
        The sealed question, one revision on.

    Raises:
        ApprovalRefusedError: The question is not waiting, which is what
            a second seal finds, or it moved past the revision the answer
            was given against.
        ValueError: The option is not one the question offers.
    """
    if action.status is not PendingActionStatus.WAITING:
        raise ApprovalRefusedError(
            ApprovalRefusal.ACTION_NOT_WAITING,
            f"{action.id} is {action.status.value}; only a "
            f"{PendingActionStatus.WAITING.value} question is sealed, and a sealed one never "
            "moves again",
        )
    if action.revision != expected_revision:
        raise ApprovalRefusedError(
            ApprovalRefusal.ACTION_STALE,
            f"{action.id} is at revision {action.revision}, not the {expected_revision} the "
            "answer was given against",
        )
    sealed = action.seal(resolver=resolver, option_id=option_id, receipt_ref=receipt_ref, at=at)
    logger.info(
        f"seal_question action={action.id} option={option_id} resolver={resolver.principal_id}"
    )
    return sealed


__all__ = [
    "ACCEPTANCE_OPTIONS",
    "VERIFIED_BATCH_STATUSES",
    "ApprovalRefusal",
    "ApprovalRefusedError",
    "acceptance_question",
    "approval_idempotency_key",
    "first_bundle",
    "next_action_key",
    "require_same_bundle",
    "require_verified_batches",
    "seal_question",
    "standing_question",
]
