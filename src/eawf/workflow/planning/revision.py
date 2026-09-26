"""The PlanRevision machine, its approval rule, and the drift predicates.

Everything here is pure. A caller hands in a record and what the locked
document says about the world, and gets back a successor record or a
typed refusal; nothing reads a file, takes a lock, or writes a byte. That
is what lets the same rules be exercised by a unit test and by the apply
transaction without the two being able to disagree.

Four rules carry the weight.

The machine is a table, so an edge that is not in it does not exist. A
revision reaches ``APPLIED`` from ``APPROVED`` and from nowhere else,
which is the whole of what "only an approved plan applies" means in code.

Approval is bound to content, not to a revision id. The receipt records
the digest of the body it was given, and every later check recomputes the
digest from the stored body rather than trusting the digest field beside
it. Editing one character of the plan after approval therefore produces a
body whose digest no receipt is bound to, and the edit is refused without
anyone having to notice it.

Approval also refuses an unverified claim. A criterion still graded
``assumed`` on the edge into ``APPROVED`` blocks that edge outright, so a
plan reaches APPROVED only once every criterion it carries is either
measured against a resolving contract or accepted as a knowingly-shipped
risk; the revision stays VALIDATED and reviewable rather than silently
carrying the gap forward.

Drift is checked against the document, never against the request. The
four bound inputs -- the Track's revision, its policy revision, the
repository heads, and the content digest -- are compared with what the
locked document holds at the moment of the apply, so a world that moved
between approval and apply refuses rather than applying a plan written
for a world that no longer exists.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from eawf.kernel.spec.common import CriterionGrounding
from eawf.kernel.state.epoch2.plan_revision import (
    PLAN_REVISION_EDGES,
    PlanApproval,
    PlanBody,
    PlanRevision,
    PlanRevisionStatus,
    milestone_counts,
    plan_content_digest,
)
from eawf.kernel.state.epoch2.track import TrackStatus

logger = logging.getLogger(__name__)


#: The event one revision transition emits, per target status. The map is
#: total over the status enum so a newly registered edge cannot reach a
#: hole here. Names sit in the ``planning`` namespace rather than
#: ``domain``: a plan revision is not a lifecycle entity and emits no
#: ``domain.<entity>.<verb>`` row.
PLAN_REVISION_EVENTS: Final[Mapping[PlanRevisionStatus, str]] = {
    PlanRevisionStatus.DRAFT: "planning.plan_revision.submitted",
    PlanRevisionStatus.VALIDATED: "planning.plan_revision.validated",
    PlanRevisionStatus.REJECTED: "planning.plan_revision.rejected",
    PlanRevisionStatus.APPROVED: "planning.plan_revision.approved",
    PlanRevisionStatus.APPLIED: "planning.plan_revision.applied",
    PlanRevisionStatus.SUPERSEDED: "planning.plan_revision.superseded",
}


class PlanRefusalCode(StrEnum):
    """The stable wire codes a plan refusal carries.

    Every value is also a declared native domain error code, which the
    daemon method checks at import, so a refusal decided here always
    reaches a client as a code it already branches on.
    """

    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    IDENTITY_NOT_FOUND = "identity_not_found"
    REVISION_CONFLICT = "revision_conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    ILLEGAL_TRANSITION = "illegal_transition"
    TRANSITION_GUARD_FAILED = "transition_guard_failed"
    PROOF_STALE = "proof_stale"
    PROTECTED_APPROVAL_REQUIRED = "protected_approval_required"


@dataclass(frozen=True, slots=True)
class PlanRefusal:
    """One refused plan operation, with nothing written.

    Attributes:
        code: The stable code a client branches on.
        guard: The exact rule that refused, which is what distinguishes
            two refusals sharing one wire code.
        detail: The operator-facing explanation.
        remediation: One sentence saying what to do about it.
        revision: The revision the record is still at, or ``None`` when
            no record was read.
    """

    code: PlanRefusalCode
    guard: str
    detail: str
    remediation: str
    revision: int | None = None


@dataclass(frozen=True, slots=True)
class PlanRevisionAdvanced:
    """The successor one legal edge produced.

    Attributes:
        record: The revision as it is after the edge.
        event_name: The event the commit appends for it.
    """

    record: PlanRevision
    event_name: str


@dataclass(frozen=True, slots=True)
class ObservedPlanWorld:
    """What the locked document says about the four bound inputs.

    Every field is read from the document at apply time. ``None`` means
    the document could not answer, and every predicate treats that as
    drift, so a missing Track or an unreadable repository row shuts the
    apply rather than waving it through.

    Attributes:
        track_revision: The primary Track's compare-and-swap revision.
        track_status: The primary Track's status token.
        policy_revision: The primary Track's policy revision.
        heads: Each repository URN mapped to the head the document
            records for it.
        claimed_keys: The ``<collection>/<key>`` locators the document
            already holds, restricted to the ones the plan would create.
    """

    track_revision: int | None
    track_status: str | None
    policy_revision: int | None
    heads: Mapping[str, str | None]
    claimed_keys: tuple[str, ...]


def citations_are_uncounted(body: PlanBody) -> bool:
    """Return whether the plan's citations move no Milestone figure.

    The check is a live one rather than a comment: the counts are derived
    twice, once from the plan as written and once from the same plan with
    every citation removed, and the two must be equal. A future consumer
    that starts deriving a count from a citation makes this false, and the
    edge into ``VALIDATED`` shuts.

    Args:
        body: The plan content.

    Returns:
        ``True`` when removing every citation changes no count.
    """
    if not body.citations:
        return True
    return milestone_counts(body) == milestone_counts(body.model_copy(update={"citations": ()}))


def ungrounded_approval_criteria(
    body: PlanBody,
    *,
    contract_is_resolvable: Callable[[str], bool] | None = None,
    decision_is_resolvable: Callable[[str], bool] | None = None,
) -> tuple[str, ...]:
    """Return the ids of every criterion the plan would approve unverified.

    Every criterion any Task in the plan carries is part of what the plan's
    Milestone is accepted against -- this model has no criterion that sits
    outside that surface -- so the approval gate reads the whole set rather
    than a separately drawn subset.

    A criterion graded ``measured`` names a contract in ``contract_refs``
    and one graded ``accepted-risk`` names a Decision in
    ``accepted_risk_decision_ref``, but this module reads and writes
    nothing on its own (see the module docstring), so whether either
    citation actually resolves is answered by *contract_is_resolvable*
    and *decision_is_resolvable*, the two seams this pure function is
    handed into the world outside it. Passing ``None`` for either skips
    that citation kind's check entirely -- a caller with no promoted-
    contract store or no v1 state to consult (a tree that predates
    either store, or one that never provisioned it) cannot honestly
    refuse a citation it has no way to look up.

    Args:
        body: The plan content to scan.
        contract_is_resolvable: Returns whether a ``contract_refs`` entry
            resolves to a promoted contract, or ``None`` to skip that
            check.
        decision_is_resolvable: Returns whether an
            ``accepted_risk_decision_ref`` resolves to a recorded, still-active
            Decision, or ``None`` to skip that check.

    Returns:
        The ``<task-urn>/<criterion-id>`` pairs still graded assumed, or
        graded measured with at least one unresolvable ``contract_refs``
        entry, or graded accepted-risk with an unresolvable
        ``accepted_risk_decision_ref``, in plan order.
    """
    ids: list[str] = []
    for task in body.tasks:
        for criterion in task.criteria:
            ungrounded = (
                criterion.grounding is CriterionGrounding.ASSUMED
                or (
                    criterion.grounding is CriterionGrounding.MEASURED
                    and contract_is_resolvable is not None
                    and not all(contract_is_resolvable(ref) for ref in criterion.contract_refs)
                )
                or (
                    criterion.grounding is CriterionGrounding.ACCEPTED_RISK
                    and decision_is_resolvable is not None
                    and criterion.accepted_risk_decision_ref is not None
                    and not decision_is_resolvable(criterion.accepted_risk_decision_ref)
                )
            )
            if ungrounded:
                ids.append(f"{task.urn}/{criterion.id}")
    return tuple(ids)


def _ungrounded_reason(body: PlanBody, ref: str) -> str:
    """Return why the ``<task-urn>/<criterion-id>`` pair *ref* names is ungrounded.

    Re-reads the criterion *ref* names rather than threading a reason
    through :func:`ungrounded_approval_criteria`'s return value, so a
    caller wanting only the ids (a summary, a count) still gets a single
    scalar per criterion. The distinction matters at the message: a
    criterion still graded ``assumed`` needs a grade, one graded
    ``measured`` or ``accepted-risk`` already carries a citation that
    just does not resolve, and conflating the two under one "still
    graded assumed" phrase names the wrong fix for the second case.

    Args:
        body: The plan content *ref* was drawn from.
        ref: One ``<task-urn>/<criterion-id>`` pair
            :func:`ungrounded_approval_criteria` returned.

    Returns:
        A phrase naming which citation is missing or unresolved.
    """
    task_urn, criterion_id = ref.rsplit("/", 1)
    for task in body.tasks:
        if str(task.urn) != task_urn:
            continue
        for criterion in task.criteria:
            if criterion.id != criterion_id:
                continue
            if criterion.grounding is CriterionGrounding.MEASURED:
                return f"{ref} cites a contract_refs entry that does not resolve"
            if criterion.grounding is CriterionGrounding.ACCEPTED_RISK:
                return f"{ref} cites a Decision that does not resolve"
            return f"{ref} is still graded assumed"
    return f"{ref} is still graded assumed"


def approval_binds_content(revision: PlanRevision, approval: PlanApproval) -> PlanRefusal | None:
    """Return why *approval* does not authorise *revision*, or ``None``.

    The digest is recomputed from the stored body rather than read off the
    record, so a body edited after submission fails here even though its
    own digest field was edited to match.

    Args:
        revision: The revision the approval is offered for.
        approval: The receipt a human principal sealed.

    Returns:
        The refusal, or ``None`` when the receipt binds this exact plan in
        this exact world.
    """
    computed = plan_content_digest(revision.body)
    if approval.content_digest != computed or revision.content_digest != computed:
        return PlanRefusal(
            code=PlanRefusalCode.PROTECTED_APPROVAL_REQUIRED,
            guard="content_digest_bound",
            detail=(
                f"the approval is bound to {approval.content_digest} but the stored plan "
                f"body digests to {computed}"
            ),
            remediation="Re-submit the edited plan as a child revision and approve that.",
            revision=revision.revision,
        )
    bound = (
        approval.base_state_revision,
        approval.policy_revision,
        approval.head_bindings,
    )
    recorded = (revision.base_state_revision, revision.policy_revision, revision.head_bindings)
    if bound != recorded:
        return PlanRefusal(
            code=PlanRefusalCode.PROTECTED_APPROVAL_REQUIRED,
            guard="approval_bindings_match",
            detail="the approval names other base bindings than the revision it approves",
            remediation="Approve the revision against the bindings it recorded at submission.",
            revision=revision.revision,
        )
    return None


def advance_plan_revision(
    revision: PlanRevision,
    *,
    to: PlanRevisionStatus,
    at: datetime,
    approval: PlanApproval | None = None,
    contract_is_resolvable: Callable[[str], bool] | None = None,
    decision_is_resolvable: Callable[[str], bool] | None = None,
) -> PlanRevisionAdvanced | PlanRefusal:
    """Return the successor of one plan-revision edge, or why it is refused.

    Args:
        revision: The revision to move.
        to: The status to move it to.
        at: When the move happened, stamped onto the successor.
        approval: The receipt, required by the edge into ``APPROVED`` and
            refused on every other edge, because an approval arriving with
            a rejection or a supersession would record consent nobody was
            asked for.
        contract_is_resolvable: Forwarded to
            :func:`ungrounded_approval_criteria` on the edge into
            ``APPROVED``; ignored on every other edge.
        decision_is_resolvable: Forwarded to
            :func:`ungrounded_approval_criteria` on the edge into
            ``APPROVED``; ignored on every other edge.

    Returns:
        The successor beside the event it emits, or the typed refusal.
    """
    if to not in PLAN_REVISION_EDGES[revision.status]:
        admitted = ", ".join(sorted(str(item) for item in PLAN_REVISION_EDGES[revision.status]))
        return PlanRefusal(
            code=PlanRefusalCode.ILLEGAL_TRANSITION,
            guard="plan_revision_edge_registered",
            detail=(
                f"a {revision.status.value} revision moves to {admitted or 'nothing'}, "
                f"not to {to.value}"
            ),
            remediation="Drive the revision along a registered edge, or supersede it.",
            revision=revision.revision,
        )
    approving = to is PlanRevisionStatus.APPROVED
    if approving and approval is None:
        return PlanRefusal(
            code=PlanRefusalCode.PROTECTED_APPROVAL_REQUIRED,
            guard="human_approval_sealed",
            detail="approving a plan needs a sealed human-principal receipt and none was given",
            remediation="Seal the approval as a PendingAction and retry with its receipt.",
            revision=revision.revision,
        )
    if not approving and approval is not None:
        return PlanRefusal(
            code=PlanRefusalCode.SCHEMA_VALIDATION_FAILED,
            guard="approval_only_on_approval_edge",
            detail=f"an approval receipt has no meaning on the edge to {to.value}",
            remediation="Retry without the approval receipt.",
            revision=revision.revision,
        )
    if approval is not None:
        refusal = approval_binds_content(revision, approval)
        if refusal is not None:
            return refusal
    if approving:
        ungrounded = ungrounded_approval_criteria(
            revision.body,
            contract_is_resolvable=contract_is_resolvable,
            decision_is_resolvable=decision_is_resolvable,
        )
        if ungrounded:
            reasons = ", ".join(_ungrounded_reason(revision.body, ref) for ref in ungrounded)
            return PlanRefusal(
                code=PlanRefusalCode.TRANSITION_GUARD_FAILED,
                guard="criteria_grounded_at_approval",
                detail=reasons,
                remediation=(
                    "Bind a probe gate or cite a promoted MeasuredContract to grade a "
                    "claim measured, grade it accepted-risk citing an active Decision, "
                    "or fix a citation that does not resolve."
                ),
                revision=revision.revision,
            )
    if to is PlanRevisionStatus.VALIDATED and not citations_are_uncounted(revision.body):
        return PlanRefusal(
            code=PlanRefusalCode.TRANSITION_GUARD_FAILED,
            guard="citations_contribute_zero",
            detail="a cited Campaign finding moves a Milestone count, which is a progress edge",
            remediation="Model the dependency as a required Batch, not as a citation.",
            revision=revision.revision,
        )
    successor = revision.model_copy(
        update={
            "status": to,
            "revision": revision.revision + 1,
            "updated_at": at,
            "approval": approval if approving else revision.approval,
        }
    )
    return PlanRevisionAdvanced(
        record=PlanRevision.model_validate(successor.model_dump(mode="json")),
        event_name=PLAN_REVISION_EVENTS[to],
    )


def detect_plan_drift(revision: PlanRevision, *, world: ObservedPlanWorld) -> PlanRefusal | None:
    """Return the first bound input that no longer matches the world.

    The order is fixed so one drifted tree always produces one answer:
    content first, because a body nobody approved makes every other
    binding moot, then the Track, its policy, the heads, and finally the
    keys the plan would claim.

    Args:
        revision: The approved revision about to be applied.
        world: What the locked document says about the bound inputs.

    Returns:
        The refusal, or ``None`` when every binding still holds.

    Raises:
        ValueError: The revision carries no approval, which an
            ``APPROVED`` record cannot do.
    """
    if revision.approval is None:
        raise ValueError(f"revision {revision.key} is {revision.status.value} and has no approval")
    content = approval_binds_content(revision, revision.approval)
    if content is not None:
        return content
    if world.track_revision != revision.base_state_revision:
        return PlanRefusal(
            code=PlanRefusalCode.REVISION_CONFLICT,
            guard="base_state_revision_pinned",
            detail=(
                f"the plan was approved over Track revision {revision.base_state_revision} "
                f"and the Track is now at {world.track_revision}"
            ),
            remediation="Re-submit the plan over the current Track revision and approve it.",
            revision=revision.revision,
        )
    if world.track_status != TrackStatus.ACTIVE.value:
        return PlanRefusal(
            code=PlanRefusalCode.TRANSITION_GUARD_FAILED,
            guard="track_active",
            detail=(
                f"the primary Track is {world.track_status}, so no Milestone is planned under it"
            ),
            remediation="Plan the Milestone under an active Track.",
            revision=revision.revision,
        )
    if world.policy_revision != revision.policy_revision:
        return PlanRefusal(
            code=PlanRefusalCode.TRANSITION_GUARD_FAILED,
            guard="policy_revision_pinned",
            detail=(
                f"the plan was approved under policy revision {revision.policy_revision} "
                f"and the Track now carries {world.policy_revision}"
            ),
            remediation="Re-submit the plan under the current policy and approve it.",
            revision=revision.revision,
        )
    moved = sorted(
        str(binding.repository_ref)
        for binding in revision.head_bindings
        if world.heads.get(str(binding.repository_ref)) != binding.head_sha
    )
    if moved:
        return PlanRefusal(
            code=PlanRefusalCode.PROOF_STALE,
            guard="base_head_bindings_pinned",
            detail=f"these repositories moved since the plan was approved: {', '.join(moved)}",
            remediation="Re-submit the plan over the current heads and approve it.",
            revision=revision.revision,
        )
    if world.claimed_keys:
        return PlanRefusal(
            code=PlanRefusalCode.REVISION_CONFLICT,
            guard="plan_keys_unclaimed",
            detail=f"the document already holds {', '.join(sorted(world.claimed_keys))}",
            remediation="Re-key the plan as a child revision, or supersede this one.",
            revision=revision.revision,
        )
    return None


__all__ = [
    "PLAN_REVISION_EVENTS",
    "ObservedPlanWorld",
    "PlanRefusal",
    "PlanRefusalCode",
    "PlanRevisionAdvanced",
    "advance_plan_revision",
    "approval_binds_content",
    "citations_are_uncounted",
    "detect_plan_drift",
    "ungrounded_approval_criteria",
]
