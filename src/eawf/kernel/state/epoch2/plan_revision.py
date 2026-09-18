"""PlanRevision: the immutable plan a Milestone is materialised from.

A PlanRevision is the only thing an apply reads. It carries the whole
plan body -- one Milestone, its Batches and their Tasks -- beside the
four bindings the apply is allowed to run against: the state revision the
plan was written over, the repository heads it was written over, the
policy revision that governed it, and the digest of its own content.

The digest is what makes approval mean something. An operator approves a
digest, never "the plan": the approval receipt records the digest it was
given, so editing one character of the body after approval leaves the
receipt bound to a digest the body no longer has. The apply recomputes
the digest from the stored body and refuses when the two disagree, which
is why the body needs no separate tamper seal.

The record is deliberately not an :class:`Epoch2Record`. It has no
qualified URN, because it addresses no entity: it is a document about
entities that do not exist yet. Locking an apply therefore uses the URNs
the plan body names, which is the right granularity anyway -- two
revisions proposing the same Milestone serialise against each other and
two proposing different ones do not.

Citations are provenance and never progress. A promoted Campaign finding
may be cited by a plan, and the count helpers here derive every Milestone
figure from owned Batches and Tasks alone, so a citation cannot move a
completion number however many of them a plan carries.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Annotated, Final, Self

import orjson
from pydantic import ConfigDict, StringConstraints, field_validator, model_validator

from eawf.kernel.identity import EntityKind, QualifiedUrn
from eawf.kernel.spec.common import CriterionSpec
from eawf.kernel.state.epoch2.base import (
    Epoch2Model,
    NonEmptyStr,
    Sha256DigestStr,
    ShaStr,
    StrictNonNegativeInt,
    StrictPositiveInt,
)
from eawf.kernel.state.epoch2.milestone import MilestoneCreateSpec, reject_duplicate_refs
from eawf.kernel.state.epoch2.task import TaskPriority
from eawf.kernel.state.epoch2.urns import (
    AnyEntityUrn,
    BatchUrn,
    MilestoneUrn,
    RepositoryUrn,
    TaskUrn,
)
from eawf.kernel.state.epoch2.values import OwnerPrincipal
from eawf.kernel.state.types import UtcDatetime

#: A ``PRV-####`` plan-revision key. The grammar is local because a plan
#: revision is not an addressable entity and therefore has no row in the
#: identity key registry.
PlanRevisionKey = Annotated[str, StringConstraints(strict=True, pattern=r"^PRV-\d{4,}$")]

#: The principal kind a plan approval admits. A service account cannot
#: approve a plan: approval is the moment a human accepts the consequences
#: of the work, and a machine accepting them records nobody.
HUMAN_PRINCIPAL_KIND: Final = "operator"


class PlanRevisionStatus(StrEnum):
    """The stored lifecycle states of a PlanRevision.

    ``DRAFT`` is what a planner emits; every other state is the daemon's
    to assign. ``REJECTED`` and ``APPLIED`` and ``SUPERSEDED`` are where a
    revision stops: a repair is a new child revision, never an edit.
    """

    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    APPROVED = "APPROVED"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


#: Which successor states each state admits. The table is the whole
#: machine: an edge absent from it does not exist, so a caller cannot
#: reach ``APPLIED`` from anywhere but ``APPROVED``.
PLAN_REVISION_EDGES: Final[dict[PlanRevisionStatus, frozenset[PlanRevisionStatus]]] = {
    PlanRevisionStatus.DRAFT: frozenset(
        {PlanRevisionStatus.VALIDATED, PlanRevisionStatus.REJECTED}
    ),
    PlanRevisionStatus.VALIDATED: frozenset(
        {PlanRevisionStatus.APPROVED, PlanRevisionStatus.SUPERSEDED}
    ),
    PlanRevisionStatus.APPROVED: frozenset(
        {PlanRevisionStatus.APPLIED, PlanRevisionStatus.SUPERSEDED}
    ),
    PlanRevisionStatus.APPLIED: frozenset({PlanRevisionStatus.SUPERSEDED}),
    PlanRevisionStatus.REJECTED: frozenset(),
    PlanRevisionStatus.SUPERSEDED: frozenset(),
}

#: The states a revision never leaves under any edge.
TERMINAL_PLAN_REVISION_STATUSES: Final[frozenset[PlanRevisionStatus]] = frozenset(
    status for status, successors in PLAN_REVISION_EDGES.items() if not successors
)

#: The states in which an approval receipt is a recorded fact. Its
#: absence in one of them would claim an apply nobody authorised.
_APPROVAL_REQUIRED: Final = frozenset({PlanRevisionStatus.APPROVED, PlanRevisionStatus.APPLIED})

#: The states in which a receipt would claim an approval nobody gave.
#: ``SUPERSEDED`` is in neither set on purpose: a revision superseded
#: after approval keeps the record of who approved it, and one superseded
#: before approval never had one to keep.
_APPROVAL_FORBIDDEN: Final = frozenset(
    {PlanRevisionStatus.DRAFT, PlanRevisionStatus.VALIDATED, PlanRevisionStatus.REJECTED}
)


class RepositoryHeadBinding(Epoch2Model):
    """The exact head one repository was at when the plan was written.

    Attributes:
        repository_ref: The repository the head belongs to.
        head_sha: The commit the plan was written over.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository_ref: RepositoryUrn
    head_sha: ShaStr


def validate_plan_approver(*, action_ref: QualifiedUrn, approved_by: OwnerPrincipal) -> None:
    """Raise when this pair cannot seal a plan approval.

    The rule has one home because it is checked twice: once at the request
    boundary, so a bad pair is a parameter rejection rather than a
    transaction that dies mid-session, and once on the stored receipt, so
    a row nobody could have submitted is also a row nobody can have
    written.

    Args:
        action_ref: The reference the approval is sealed as.
        approved_by: Who is approving.

    Raises:
        ValueError: A team or service principal is approving, or the
            reference addresses something other than a PendingAction.
    """
    if approved_by.principal_kind != HUMAN_PRINCIPAL_KIND:
        raise ValueError(
            f"a plan approval requires a {HUMAN_PRINCIPAL_KIND} principal, "
            f"not {approved_by.principal_kind}"
        )
    if action_ref.kind is not EntityKind.PENDING_ACTION:
        raise ValueError(
            f"action_ref must address a {EntityKind.PENDING_ACTION.value}, "
            f"not a {action_ref.kind.value}"
        )


class PlanApproval(Epoch2Model):
    """One human principal's approval of one exact plan content.

    Every field is part of what was approved. The digest says which bytes,
    the three revisions say which world those bytes were written over, and
    the principal says who accepted them. An apply that cannot match all
    four against the tree it is about to write is applying an approval
    nobody gave for the situation in front of it.

    Attributes:
        action_ref: The PendingAction the approval was sealed as. It is
            asserted, not dereferenced: the record it addresses lands with
            the PendingAction producer, and the reference travels into the
            committed event so the approval stays readable from the event.
        approved_by: Who approved. The kind must be a human principal.
        approved_at: When the approval was given.
        content_digest: The digest of the plan body that was approved.
        base_state_revision: The primary Track's compare-and-swap
            revision the plan was written over. The Track is the container
            the Milestone lands under, so its token is the one that moves
            when the plan's world does and stays still when an unrelated
            record commits.
        policy_revision: The Track policy revision the plan was written
            under.
        head_bindings: The repository heads the plan was written over.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_ref: AnyEntityUrn
    approved_by: OwnerPrincipal
    approved_at: UtcDatetime
    content_digest: Sha256DigestStr
    base_state_revision: StrictPositiveInt
    policy_revision: StrictPositiveInt
    head_bindings: tuple[RepositoryHeadBinding, ...] = ()

    @model_validator(mode="after")
    def _approver_is_a_human_principal(self) -> Self:
        """Require the approving principal to be a human operator.

        Raises:
            ValueError: A team or service principal approved, or the
                action reference addresses something other than a
                PendingAction.
        """
        validate_plan_approver(action_ref=self.action_ref, approved_by=self.approved_by)
        return self


class PlannedBatch(Epoch2Model):
    """One Batch an apply materialises, in one repository.

    Attributes:
        urn: The Batch the apply creates.
        repository_ref: The one repository it integrates into.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    urn: BatchUrn
    repository_ref: RepositoryUrn


class PlannedTask(Epoch2Model):
    """One PLANNED Task an apply materialises inside one Batch.

    ``criteria`` is non-empty because a PLANNED Task with no criterion has
    no definition of done, and the Task model refuses one anyway; catching
    it here makes it a plan rejection rather than an apply-time surprise.

    Attributes:
        urn: The Task the apply creates.
        batch_ref: The Batch it belongs to, which the plan must declare.
        priority: How it orders against its siblings.
        intent: What the Task is for.
        criteria: Its typed success criteria.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    urn: TaskUrn
    batch_ref: BatchUrn
    priority: TaskPriority
    intent: NonEmptyStr
    criteria: tuple[CriterionSpec, ...]

    @field_validator("criteria")
    @classmethod
    def _criteria_are_present(cls, value: tuple[CriterionSpec, ...]) -> tuple[CriterionSpec, ...]:
        """Require at least one criterion.

        Raises:
            ValueError: The tuple is empty.
        """
        if not value:
            raise ValueError("a planned Task names at least one criterion")
        return value


class CampaignCitation(Epoch2Model):
    """A provenance edge from the planned Milestone to a promoted finding.

    A citation records where a plan's reasoning came from. It is never a
    dependency and never a unit of work, which is why it carries no status
    and no owner: nothing about it can complete, so nothing about it can
    contribute to a completion figure.

    Attributes:
        finding_ref: The promoted CampaignFinding the plan cites.
        note: What the plan took from it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_ref: AnyEntityUrn
    note: NonEmptyStr

    @model_validator(mode="after")
    def _citation_addresses_a_finding(self) -> Self:
        """Require the reference to address a promoted Campaign finding.

        Raises:
            ValueError: The reference addresses any other kind. A citation
                naming a Batch or a Task would be a delivery edge wearing
                a provenance label, and every count that walks the plan
                would then pick it up.
        """
        if self.finding_ref.kind is not EntityKind.CAMPAIGN_FINDING:
            raise ValueError(
                f"a citation addresses a {EntityKind.CAMPAIGN_FINDING.value}, "
                f"not a {self.finding_ref.kind.value}"
            )
        return self


class PlanBody(Epoch2Model):
    """Everything one apply materialises, and nothing else.

    The body is the digested unit: the digest covers exactly these fields,
    so the bindings recorded beside it stay outside the content they bind.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    milestone_urn: MilestoneUrn
    milestone: MilestoneCreateSpec
    batches: tuple[PlannedBatch, ...]
    tasks: tuple[PlannedTask, ...]
    citations: tuple[CampaignCitation, ...] = ()

    @model_validator(mode="after")
    def _plan_is_internally_resolvable(self) -> Self:
        """Require every reference inside the plan to resolve inside it.

        Raises:
            ValueError: The Milestone URN and key disagree, the plan
                creates no Batch or no Task, a URN repeats, a Task names a
                Batch the plan does not create, a required Batch is not
                one of them, or a finding is cited twice.
        """
        if self.milestone.key != self.milestone_urn.entity_key:
            raise ValueError(
                f"the plan keys its Milestone {self.milestone.key!r} but addresses "
                f"{self.milestone_urn.entity_key!r}"
            )
        if not self.batches:
            raise ValueError("a plan creates at least one Batch")
        if not self.tasks:
            raise ValueError("a plan creates at least one Task")
        batch_refs = tuple(batch.urn for batch in self.batches)
        reject_duplicate_refs(batch_refs, field="batches")
        reject_duplicate_refs(tuple(task.urn for task in self.tasks), field="tasks")
        reject_duplicate_refs(
            tuple(citation.finding_ref for citation in self.citations), field="citations"
        )
        declared = frozenset(batch_refs)
        orphans = sorted(str(task.urn) for task in self.tasks if task.batch_ref not in declared)
        if orphans:
            raise ValueError(f"these Tasks name a Batch the plan does not create: {orphans}")
        unknown = sorted(
            str(ref) for ref in self.milestone.required_batch_refs if ref not in declared
        )
        if unknown:
            raise ValueError(
                f"required_batch_refs name Batches the plan does not create: {unknown}"
            )
        return self


class PlanRevision(Epoch2Model):
    """One immutable plan, its bindings, and where it is in its lifecycle.

    Attributes:
        key: The ``PRV-####`` public key.
        revision: The compare-and-swap token of this record.
        status: Where the revision sits in its own machine.
        author: Who emitted the proposal.
        created_at: When the revision was first recorded.
        updated_at: When it last moved.
        content_digest: The digest of ``body`` as recorded at submission.
        base_state_revision: The primary Track's revision the plan was
            written over.
        policy_revision: The Track policy revision it was written under.
        head_bindings: The repository heads it was written over.
        body: What an apply would materialise.
        approval: The sealed approval, present exactly from ``APPROVED``.
        parent_key: The revision this one repairs, when it repairs one.
    """

    model_config = ConfigDict(extra="forbid")

    key: PlanRevisionKey
    revision: StrictPositiveInt
    status: PlanRevisionStatus
    author: OwnerPrincipal
    created_at: UtcDatetime
    updated_at: UtcDatetime
    content_digest: Sha256DigestStr
    base_state_revision: StrictPositiveInt
    policy_revision: StrictPositiveInt
    head_bindings: tuple[RepositoryHeadBinding, ...] = ()
    body: PlanBody
    approval: PlanApproval | None = None
    parent_key: PlanRevisionKey | None = None

    @model_validator(mode="after")
    def _approval_matches_status(self) -> Self:
        """Require the approval exactly where the status makes it a fact.

        Raises:
            ValueError: An approved or applied revision carries no
                approval, or a revision that nobody approved carries one.
        """
        if self.status in _APPROVAL_REQUIRED and self.approval is None:
            raise ValueError(f"status {self.status.value} requires an approval receipt")
        if self.status in _APPROVAL_FORBIDDEN and self.approval is not None:
            raise ValueError(
                f"an approval receipt belongs to an approved revision, not {self.status.value}"
            )
        return self

    @model_validator(mode="after")
    def _revision_is_not_its_own_parent(self) -> Self:
        """Refuse a revision that repairs itself.

        Raises:
            ValueError: The parent key is this revision's own key, which
                would make the repair chain a cycle of length one.
        """
        if self.parent_key == self.key:
            raise ValueError(f"revision {self.key} names itself as its parent")
        return self


class MilestoneCounts(Epoch2Model):
    """What a planned Milestone is counted as, derived from owned work.

    Every field is a count of records the Milestone owns. Citations are
    absent by construction rather than by subtraction: there is no field
    here a provenance edge could land in, so no consumer can be handed one
    to add up.

    Attributes:
        batch_count: How many Batches the Milestone owns.
        task_count: How many Tasks those Batches own.
        required_batch_count: How many of the Batches must close before
            acceptance.
        acceptance_step_count: How many steps the acceptance journey has.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_count: StrictNonNegativeInt
    task_count: StrictNonNegativeInt
    required_batch_count: StrictNonNegativeInt
    acceptance_step_count: StrictNonNegativeInt


def plan_content_digest(body: PlanBody) -> str:
    """Return the digest an approval of *body* is bound to.

    The body is serialised through its own model in JSON mode and encoded
    with sorted keys, so two spellings of one plan digest the same and any
    change to any field digests differently.

    Args:
        body: The plan content to digest.

    Returns:
        The ``sha256:`` prefixed hex digest, in the spelling the record's
        digest fields accept.
    """
    payload = orjson.dumps(body.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS)
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def milestone_counts(body: PlanBody) -> MilestoneCounts:
    """Return the Milestone figures *body* implies, from owned work alone.

    Args:
        body: The plan content.

    Returns:
        The counts. Adding, removing or rewriting any citation leaves
        every field unchanged, because no field is derived from one.
    """
    return MilestoneCounts(
        batch_count=len(body.batches),
        task_count=len(body.tasks),
        required_batch_count=len(body.milestone.required_batch_refs),
        acceptance_step_count=len(body.milestone.acceptance_journey),
    )


__all__ = [
    "HUMAN_PRINCIPAL_KIND",
    "PLAN_REVISION_EDGES",
    "TERMINAL_PLAN_REVISION_STATUSES",
    "CampaignCitation",
    "MilestoneCounts",
    "PlanApproval",
    "PlanBody",
    "PlanRevision",
    "PlanRevisionKey",
    "PlanRevisionStatus",
    "PlannedBatch",
    "PlannedTask",
    "RepositoryHeadBinding",
    "milestone_counts",
    "plan_content_digest",
    "validate_plan_approver",
]
