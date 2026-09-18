"""Holding an acceptance approval against the exact bundle it was given to.

Accepting a Milestone is the one lifecycle move the daemon will not take
on a caller's word, and this module is where the word is checked. Three
rules decide it, and each is enforced where it cannot be forgotten.

The resolver is a person. That is not tested here, because it cannot be
false: :attr:`~eawf.kernel.state.epoch2.pending_action.PendingAction.resolution_actor`
is typed
:class:`~eawf.kernel.state.epoch2.pending_action.HumanPrincipal`, and an
agent principal is a different closed shape that the loader refuses in
that slot. A sealed action carrying an agent resolver is unconstructible
rather than rejected, so there is no path that could reach this module
having skipped a check.

The approval is bound to bytes. A sealed protected approval records the
digest of the
:class:`~eawf.kernel.delivery.acceptance.MilestoneAcceptanceBundle` it was
given to, and :func:`require_sealed_acceptance` recomputes that digest
from the bundle presented with the request. A caller may therefore
present whatever bundle it likes: a bundle that is not the one approved
digests differently and the acceptance is refused. That is what keeps the
approval from being a ticket good against any later contents.

A repair opens a successor. An operator who asks for changes has not
declined and has not approved; the bundle they read stays exactly as it
was, and :func:`request_repair` appends the next revision carrying the
prior revision's own digest. Nothing rewrites the approved bytes, so an
approval already given stays bound to what it was given to.

Nothing here writes a record or moves a Milestone.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Self

from pydantic import ConfigDict, model_validator

from eawf.kernel.delivery.acceptance import (
    AcceptanceBundleLedger,
    AcceptanceStepOutcome,
    EvidenceView,
    MilestoneAcceptanceBundle,
)
from eawf.kernel.state.epoch2.base import Epoch2Model, Sha256DigestStr
from eawf.kernel.state.epoch2.milestone import MilestoneStatus
from eawf.kernel.state.epoch2.pending_action import (
    HumanPrincipal,
    OptionEffect,
    PendingAction,
    PendingActionKind,
    PendingActionStatus,
)
from eawf.kernel.state.epoch2.transitions import DenialCode
from eawf.kernel.state.epoch2.urns import EvidenceUrn, MilestoneUrn
from eawf.kernel.state.epoch2.values import ExactRevisionBinding, TransitionReason
from eawf.kernel.state.types import UtcDatetime

logger = logging.getLogger(__name__)


class AcceptanceRefusal(StrEnum):
    """The stable codes a Milestone acceptance is refused with.

    ``JOURNEY_INCOMPLETE`` is bound to the transition registry's own
    denial code rather than re-spelled: a bundle whose steps did not all
    pass is exactly the predicate the registry's acceptance guard names,
    and a caller routing on that code must keep routing when the refusal
    arrives from the bundle instead of from the guard.
    """

    APPROVAL_UNSEALED = "acceptance_approval_unsealed"
    APPROVAL_MISBOUND = "acceptance_approval_misbound"
    APPROVAL_NOT_PROTECTED = "acceptance_approval_not_protected"
    APPROVAL_WITHHELD = "acceptance_approval_withheld"
    BUNDLE_SUPERSEDED = "acceptance_bundle_superseded"
    EVIDENCE_UNHELD = "acceptance_evidence_unheld"
    JOURNEY_INCOMPLETE = DenialCode.ACCEPTANCE_JOURNEY_INCOMPLETE


class AcceptanceRefusedError(ValueError):
    """One acceptance step refused, with the code that says which rule.

    Attributes:
        code: The stable refusal code a caller routes on.
    """

    def __init__(self, code: AcceptanceRefusal, detail: str) -> None:
        """Keep the code beside the sentence an operator reads.

        Args:
            code: The stable refusal code.
            detail: One sentence naming what was refused and why.
        """
        self.code = code
        super().__init__(f"{code.value}: {detail}")


class AcceptanceApproval(Epoch2Model):
    """The proof one acceptance is allowed to proceed on.

    The model only exists once every rule has held, so a caller holding
    one is holding the finished answer rather than the inputs to it.
    Building it anywhere but :func:`require_sealed_acceptance` would be
    building the conclusion, which is why the resolver and the digest are
    both required and are checked against each other on the way in.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    milestone_ref: MilestoneUrn
    bundle_revision: int
    approved_digest: Sha256DigestStr
    resolved_by: HumanPrincipal
    receipt_ref: EvidenceUrn
    accepted_binding: ExactRevisionBinding
    approved_at: UtcDatetime

    @model_validator(mode="after")
    def _revision_is_a_real_ordinal(self) -> Self:
        """Require the approved revision to be one a bundle can carry.

        Raises:
            ValueError: The revision is not positive, so it names no
                bundle.
        """
        if self.bundle_revision < 1:
            raise ValueError(f"bundle_revision {self.bundle_revision} is not a bundle ordinal")
        return self


def _require_sealed_protected(action: PendingAction) -> None:
    """Refuse an approval that is not a sealed protected one.

    Raises:
        AcceptanceRefusedError: The action asks a different sort of
            question, or nobody has answered it yet.
    """
    if action.kind is not PendingActionKind.PROTECTED_APPROVAL:
        raise AcceptanceRefusedError(
            AcceptanceRefusal.APPROVAL_NOT_PROTECTED,
            f"action {action.id} is a {action.kind.value}, and acceptance needs a "
            f"{PendingActionKind.PROTECTED_APPROVAL.value}",
        )
    if action.status is not PendingActionStatus.SEALED:
        raise AcceptanceRefusedError(
            AcceptanceRefusal.APPROVAL_UNSEALED,
            f"action {action.id} is {action.status.value}: nobody has answered it, so there is "
            "no approval to accept on",
        )


def _require_approving_answer(action: PendingAction) -> None:
    """Refuse a sealed action whose answer was not the approving one.

    Raises:
        AcceptanceRefusedError: The resolver declined or asked for a
            repair, so the acceptance would proceed on an answer that
            said not to.
    """
    chosen = action.selected_option
    assert chosen is not None, "a sealed action always resolves to a declared option"
    if chosen.effect is not OptionEffect.APPROVE:
        raise AcceptanceRefusedError(
            AcceptanceRefusal.APPROVAL_WITHHELD,
            f"action {action.id} was answered {chosen.option_id!r}, which "
            f"{chosen.effect.value}s rather than approves",
        )


def _require_bundle_binding(
    action: PendingAction, bundle: MilestoneAcceptanceBundle, *, milestone_ref: MilestoneUrn
) -> str:
    """Return the digest the sealed answer was given to, refusing any drift.

    Raises:
        AcceptanceRefusedError: The action approves another Milestone, the
            bundle belongs to another Milestone, or the presented bundle
            does not digest to what was approved.
    """
    if str(action.subject_ref) != str(milestone_ref):
        raise AcceptanceRefusedError(
            AcceptanceRefusal.APPROVAL_MISBOUND,
            f"action {action.id} approves {action.subject_ref.entity_key}, not "
            f"{milestone_ref.entity_key}",
        )
    if bundle.milestone_ref != milestone_ref:
        raise AcceptanceRefusedError(
            AcceptanceRefusal.APPROVAL_MISBOUND,
            f"the presented bundle is {bundle.milestone_ref.entity_key}'s, not "
            f"{milestone_ref.entity_key}'s",
        )
    approved = action.bundle_digest
    assert approved is not None, "a protected approval always records the digest it approves"
    presented = bundle.digest()
    if presented != approved:
        raise AcceptanceRefusedError(
            AcceptanceRefusal.BUNDLE_SUPERSEDED,
            f"action {action.id} approved another set of bytes than revision "
            f"{bundle.revision} of {milestone_ref.entity_key} presents, so the approval does "
            "not cover it",
        )
    return approved


def _require_complete_journey(bundle: MilestoneAcceptanceBundle) -> None:
    """Refuse a bundle whose journey did not finish.

    Raises:
        AcceptanceRefusedError: A step of the acceptance journey did not
            pass, so the outcome was not demonstrated.
    """
    blocking = bundle.blocking_step_ids
    if blocking:
        raise AcceptanceRefusedError(
            AcceptanceRefusal.JOURNEY_INCOMPLETE,
            f"steps {', '.join(blocking)} of {bundle.milestone_ref.entity_key} did not pass, so "
            "the outcome has not been demonstrated",
        )


def require_sealed_acceptance(
    action: PendingAction,
    *,
    bundle: MilestoneAcceptanceBundle,
    milestone_ref: MilestoneUrn,
    status: MilestoneStatus,
) -> AcceptanceApproval:
    """Return the approval an acceptance may proceed on, or refuse it.

    Building the approval is the only way to reach an acceptance, so every
    rule is checked here once rather than at each call site that might
    have remembered to. The resolver's being a person is not among them:
    it is a property of the record's own type.

    Args:
        action: The PendingAction the tree holds for this acceptance,
            read rather than presented, so a caller cannot supply its own
            approval.
        bundle: The acceptance bundle the request presents. A bundle that
            is not the approved one digests differently and is refused,
            so presenting it costs a caller nothing it could exploit.
        milestone_ref: The Milestone being accepted.
        status: The status the Milestone stands in.

    Returns:
        The approval, carrying who gave it and the exact digest it covers.

    Raises:
        AcceptanceRefusedError: The action is not a sealed protected
            approval, its answer was not the approving one, it approves
            another Milestone or another set of bytes, the Milestone is
            not in acceptance review, or a journey step did not pass.
    """
    if status is not MilestoneStatus.ACCEPTANCE_REVIEW:
        raise AcceptanceRefusedError(
            AcceptanceRefusal.APPROVAL_MISBOUND,
            f"{milestone_ref.entity_key} is {status.value}, and an acceptance is given in "
            f"{MilestoneStatus.ACCEPTANCE_REVIEW.value}",
        )
    _require_sealed_protected(action)
    _require_approving_answer(action)
    approved = _require_bundle_binding(action, bundle, milestone_ref=milestone_ref)
    _require_complete_journey(bundle)
    resolver = action.resolution_actor
    receipt = action.receipt_ref
    assert resolver is not None, "a sealed action always records who answered"
    assert receipt is not None, "a sealed action always records the receipt of the answer"
    logger.info(
        f"require_sealed_acceptance milestone={milestone_ref.entity_key} "
        f"action={action.id} revision={bundle.revision} resolver={resolver.principal_id}"
    )
    return AcceptanceApproval(
        milestone_ref=milestone_ref,
        bundle_revision=bundle.revision,
        approved_digest=approved,
        resolved_by=resolver,
        receipt_ref=receipt,
        accepted_binding=bundle.accepted_binding,
        approved_at=action.updated_at,
    )


def request_repair(
    ledger: AcceptanceBundleLedger,
    *,
    action: PendingAction,
    steps: Sequence[AcceptanceStepOutcome],
    accepted_binding: ExactRevisionBinding,
    at: UtcDatetime,
) -> AcceptanceBundleLedger:
    """Return the ledger with the successor revision a repair request earns.

    The head revision is not touched. The successor is built from it and
    records its digest, so the chain refuses to validate if the revision
    an approval was given to is ever rewritten.

    Args:
        ledger: The Milestone's bundle revisions as they stand.
        action: The sealed PendingAction whose answer asked for repair.
            Its selected option supplies the reason, so the successor
            cannot be opened without somebody having asked for it.
        steps: What the repaired journey shows.
        accepted_binding: The exact tree the successor is taken on.
        at: When the successor was sealed.

    Returns:
        The ledger with one more revision appended.

    Raises:
        AcceptanceRefusedError: The ledger holds no revision to supersede,
            the action is not sealed, or its answer was not the repair
            one.
        ValueError: The successor breaks a bundle or chain rule.
    """
    head = ledger.head
    if head is None:
        raise AcceptanceRefusedError(
            AcceptanceRefusal.BUNDLE_SUPERSEDED,
            f"{ledger.milestone_ref.entity_key} has no sealed bundle, so there is nothing for a "
            "repair to supersede",
        )
    _require_sealed_protected(action)
    chosen = action.selected_option
    assert chosen is not None, "a sealed action always resolves to a declared option"
    if chosen.effect is not OptionEffect.REQUEST_REPAIR:
        raise AcceptanceRefusedError(
            AcceptanceRefusal.APPROVAL_WITHHELD,
            f"action {action.id} was answered {chosen.option_id!r}, which "
            f"{chosen.effect.value}s rather than asks for a repair",
        )
    reason = TransitionReason(
        code=chosen.effect.value.replace("_", "-"),
        message=f"{chosen.label} was chosen on {action.id}",
        evidence_refs=() if action.receipt_ref is None else (action.receipt_ref,),
    )
    successor = head.open_successor(
        reason=reason, steps=steps, accepted_binding=accepted_binding, at=at
    )
    logger.info(
        f"request_repair milestone={ledger.milestone_ref.entity_key} "
        f"from_revision={head.revision} to_revision={successor.revision}"
    )
    return AcceptanceBundleLedger(
        milestone_ref=ledger.milestone_ref, bundles=(*ledger.bundles, successor)
    )


def acceptance_evidence(view: EvidenceView, bundle: MilestoneAcceptanceBundle) -> tuple[str, ...]:
    """Return the evidence keys *bundle* cites, refusing any the view lacks.

    The lookup is arithmetic over the rows already in hand: the view holds
    no path and no handle, so reading it cannot reach the tree even by
    accident.

    Args:
        view: The evidence already read back.
        bundle: The revision whose citations are being resolved.

    Returns:
        Every cited ``EVD-####``, sorted.

    Raises:
        AcceptanceRefusedError: The bundle cites evidence the view holds
            no row for, so a step would be demonstrated by a reference
            pointing at nothing.
    """
    cited = bundle.evidence_keys
    missing = view.missing(cited)
    if missing:
        raise AcceptanceRefusedError(
            AcceptanceRefusal.EVIDENCE_UNHELD,
            f"revision {bundle.revision} of {bundle.milestone_ref.entity_key} cites "
            f"{', '.join(missing)}, which no evidence row holds",
        )
    return cited


def evidence_view(rows: Iterable[object]) -> EvidenceView:
    """Return the read-only view over already-read evidence payloads.

    Args:
        rows: The stored evidence payloads, as the ledger holds them.

    Returns:
        The view, with one row per evidence key.

    Raises:
        pydantic.ValidationError: A payload is not an evidence row, or two
            rows share a key.
    """
    return EvidenceView.model_validate({"rows": list(rows)})


__all__ = [
    "AcceptanceApproval",
    "AcceptanceRefusal",
    "AcceptanceRefusedError",
    "acceptance_evidence",
    "evidence_view",
    "request_repair",
    "require_sealed_acceptance",
]
