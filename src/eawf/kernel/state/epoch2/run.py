"""Run, the ten-variant scope it addresses, and why it is suspended.

A Run is one execution episode of one agent. What the episode is *about*
is a discriminated scope rather than a nullable pile of reference
fields: a record holding ``task_ref``, ``batch_ref`` and ``release_ref``
side by side has three answers to "what does this run touch" and no rule
saying which one wins, so every consumer would have to invent its own
precedence and two of them would disagree.

Only a task-scoped Run may write. That is why :class:`TaskScope` is the
one variant carrying a non-empty ``write_set`` and the one variant a
mutating :class:`RunPurpose` is admitted on: a review of a Milestone or
an observation of a Release has nothing to integrate, so a write set on
one of them is either a mislabelled run or an escape from the scope it
declared, and both are defects rather than unusual inputs.

``SUSPENDED`` never stands alone. A suspended Run names the
:class:`SuspensionReason` whose clearing fact resumes it, so the
Activity surface can bucket what is waiting on an operator, on a
permission, on other work, or on capacity, without re-deriving the
answer from a status history that does not carry it.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, ClassVar, Final, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from eawf.kernel.state.epoch2.base import Epoch2Model, RunKey
from eawf.kernel.state.epoch2.urns import (
    BatchUrn,
    CampaignUrn,
    ClaimUrn,
    EvidenceUrn,
    MilestoneUrn,
    QuestionUrn,
    ReleaseUrn,
    RepositoryUrn,
    RunUrn,
    TaskUrn,
    WorkspaceUrn,
)
from eawf.kernel.state.epoch2.values import Epoch2Record, TransitionReason
from eawf.kernel.state.types import UtcDatetime

#: One repository-relative path a task-scoped Run may write. A leading
#: separator or a backslash is refused because a write set is compared
#: against the repository tree, and an absolute or host-flavoured path
#: names something outside it.
WriteSetPath = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=500, pattern=r"^[^/\\][^\\]*$"),
]


class RunPurpose(StrEnum):
    """What one Run is being executed to do.

    The first three purposes change a repository and the rest only read
    it. The split is the whole point of the enumeration: it is what
    :class:`TaskScope` is checked against, so a run that intends to
    write cannot be filed under a scope that has nothing to write to.
    """

    IMPLEMENT = "implement"
    INTEGRATE = "integrate"
    REPAIR = "repair"
    REVIEW = "review"
    AUDIT = "audit"
    RESEARCH = "research"
    PLAN = "plan"
    OBSERVE = "observe"


#: The purposes that change a repository worktree. Admitted only on a
#: task-scoped Run, which is the one scope that owns a place to write.
MUTATING_PURPOSES: Final = frozenset(
    {RunPurpose.IMPLEMENT, RunPurpose.INTEGRATE, RunPurpose.REPAIR}
)


class _RunScopeBase(Epoch2Model):
    """The fields every Run scope carries, and the mutation rule.

    Each variant adds exactly one typed reference of its own kind. The
    shared half is the purpose the episode runs under and the write set
    it is permitted to touch, both of which are checked against
    :attr:`mutating` so the rule lives once instead of in ten copies.
    """

    #: Whether a Run at this scope owns a worktree it may write to.
    mutating: ClassVar[bool] = False

    purpose: RunPurpose
    write_set: tuple[WriteSetPath, ...] = ()

    @field_validator("write_set")
    @classmethod
    def _paths_are_contained_and_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject a write-set path that escapes the tree or repeats.

        Args:
            value: The declared write-set paths, in author order.

        Returns:
            *value* unchanged.

        Raises:
            ValueError: A path walks out of the repository through a
                ``..`` segment, or the same path is declared twice.
        """
        for path in value:
            if ".." in path.split("/"):
                raise ValueError(f"write_set path {path!r} walks out of the repository")
        if len(set(value)) != len(value):
            raise ValueError("write_set declares the same path twice")
        return value

    @model_validator(mode="after")
    def _mutation_rights_match_scope(self) -> Self:
        """Confine a write set and a mutating purpose to a task-scoped Run.

        Raises:
            ValueError: A non-task scope declares a write set or a
                mutating purpose, or a task-scoped mutating Run declares
                no write set to bound what it may touch.
        """
        if self.mutating:
            if self.purpose in MUTATING_PURPOSES and not self.write_set:
                raise ValueError(
                    f"purpose {self.purpose.value!r} writes, so write_set must name what it "
                    f"may touch"
                )
            return self
        if self.write_set:
            raise ValueError("write_set belongs to a task-scoped Run; this scope writes nothing")
        if self.purpose in MUTATING_PURPOSES:
            raise ValueError(
                f"purpose {self.purpose.value!r} mutates a repository, which only a "
                f"task-scoped Run may do"
            )
        return self


class TaskScope(_RunScopeBase):
    """A Run executing one Task: the only scope that may write."""

    mutating: ClassVar[bool] = True

    scope_kind: Literal["task"]
    task_ref: TaskUrn


class BatchScope(_RunScopeBase):
    """A Run reading one DeliveryBatch, such as an integration review."""

    scope_kind: Literal["batch"]
    batch_ref: BatchUrn


class MilestoneScope(_RunScopeBase):
    """A Run reading one Milestone, such as an acceptance sweep."""

    scope_kind: Literal["milestone"]
    milestone_ref: MilestoneUrn


class CampaignScope(_RunScopeBase):
    """A Run reading one Campaign, such as a research round."""

    scope_kind: Literal["campaign"]
    campaign_ref: CampaignUrn


class ReleaseScope(_RunScopeBase):
    """A Run reading one Release, such as a preflight or a read-back."""

    scope_kind: Literal["release"]
    release_ref: ReleaseUrn


class RepositoryScope(_RunScopeBase):
    """A Run reading one repository as a whole, such as an inventory."""

    scope_kind: Literal["repository"]
    repository_ref: RepositoryUrn


class WorkspaceScope(_RunScopeBase):
    """A Run reading a whole workspace, such as a cross-track plan."""

    scope_kind: Literal["workspace"]
    workspace_ref: WorkspaceUrn


class EvidenceScope(_RunScopeBase):
    """A Run reading one evidence record, such as a re-verification."""

    scope_kind: Literal["evidence"]
    evidence_ref: EvidenceUrn


class ClaimScope(_RunScopeBase):
    """A Run reading one claim, such as a ladder rung check."""

    scope_kind: Literal["claim"]
    claim_ref: ClaimUrn


class QuestionScope(_RunScopeBase):
    """A Run reading one open question, such as a differentiation."""

    scope_kind: Literal["question"]
    question_ref: QuestionUrn


#: A Run's scope, discriminated on ``scope_kind``. Exactly one variant
#: parses, so a Run holds one typed reference rather than ten nullable
#: ones.
RunScope = Annotated[
    TaskScope
    | BatchScope
    | MilestoneScope
    | CampaignScope
    | ReleaseScope
    | RepositoryScope
    | WorkspaceScope
    | EvidenceScope
    | ClaimScope
    | QuestionScope,
    Field(discriminator="scope_kind"),
]


class RunStatus(StrEnum):
    """The stored lifecycle states of a Run.

    ``SUSPENDED`` is a first-class state rather than a flag on
    ``RUNNING`` because it has its own exit condition: a suspended Run
    resumes on a named clearing fact, and a running one does not wait
    for anything.
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUSPENDED = "SUSPENDED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SuspensionReason(StrEnum):
    """Why a Run is suspended, named by the fact that clears it.

    The vocabulary is closed and every member is a *waiting-for*, not a
    diagnosis: an operator answer, a permission decision, another unit
    of work finishing, a lease releasing, or provider capacity coming
    back. A reason with no clearing fact would describe a Run that can
    never legitimately resume, which is a failure rather than a pause.
    """

    AWAITING_OPERATOR_INPUT = "AWAITING_OPERATOR_INPUT"
    AWAITING_HUMAN_REVIEW = "AWAITING_HUMAN_REVIEW"
    AWAITING_PERMISSION_GRANT = "AWAITING_PERMISSION_GRANT"
    AWAITING_DEPENDENCY = "AWAITING_DEPENDENCY"
    AWAITING_LEASE = "AWAITING_LEASE"
    AWAITING_PROVIDER_CAPACITY = "AWAITING_PROVIDER_CAPACITY"


class ActivityBucket(StrEnum):
    """The Activity sub-bucket a suspended Run is listed under.

    An operator reading Activity asks one question -- "is this waiting
    on me?" -- so the buckets separate what a person can clear from what
    only time or other work can.
    """

    NEEDS_OPERATOR = "needs_operator"
    NEEDS_PERMISSION = "needs_permission"
    BLOCKED_ON_WORK = "blocked_on_work"
    BLOCKED_ON_CAPACITY = "blocked_on_capacity"


#: Which Activity sub-bucket each suspension reason lands in. Total over
#: :class:`SuspensionReason` and single-valued, so the partition is a
#: property of this table rather than of whichever renderer reads it.
SUSPENSION_ACTIVITY_BUCKETS: Final[Mapping[SuspensionReason, ActivityBucket]] = {
    SuspensionReason.AWAITING_OPERATOR_INPUT: ActivityBucket.NEEDS_OPERATOR,
    SuspensionReason.AWAITING_HUMAN_REVIEW: ActivityBucket.NEEDS_OPERATOR,
    SuspensionReason.AWAITING_PERMISSION_GRANT: ActivityBucket.NEEDS_PERMISSION,
    SuspensionReason.AWAITING_DEPENDENCY: ActivityBucket.BLOCKED_ON_WORK,
    SuspensionReason.AWAITING_LEASE: ActivityBucket.BLOCKED_ON_WORK,
    SuspensionReason.AWAITING_PROVIDER_CAPACITY: ActivityBucket.BLOCKED_ON_CAPACITY,
}

#: The states a Run has stopped in. A stopped Run has both stamps, so
#: its duration is a recorded fact rather than a difference against now.
_TERMINAL: Final = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})


class Run(Epoch2Record):
    """One execution episode of one agent against one scope."""

    key: RunKey
    urn: RunUrn
    scope: RunScope
    status: RunStatus
    started_at: UtcDatetime | None = None
    ended_at: UtcDatetime | None = None
    suspension_reason: SuspensionReason | None = None
    failure: TransitionReason | None = None

    @property
    def activity_bucket(self) -> ActivityBucket | None:
        """The Activity sub-bucket this Run waits in, or ``None``.

        Returns:
            The bucket of :attr:`suspension_reason`, or ``None`` when
            the Run is not suspended and therefore waits for nothing.
        """
        if self.suspension_reason is None:
            return None
        return SUSPENSION_ACTIVITY_BUCKETS[self.suspension_reason]

    @model_validator(mode="after")
    def _suspension_reason_matches_status(self) -> Self:
        """Bind the suspension reason to the one status that means waiting.

        Raises:
            ValueError: A SUSPENDED Run names no reason, or a Run in any
                other status carries one.
        """
        suspended = self.status is RunStatus.SUSPENDED
        if suspended and self.suspension_reason is None:
            raise ValueError("a SUSPENDED Run requires the suspension_reason that clears it")
        if not suspended and self.suspension_reason is not None:
            raise ValueError(
                f"suspension_reason belongs to a SUSPENDED Run, not {self.status.value}"
            )
        return self

    @model_validator(mode="after")
    def _failure_matches_status(self) -> Self:
        """Require the failure reason exactly where the status implies it.

        Raises:
            ValueError: A FAILED Run carries no failure reason, or an
                unfailed Run carries one.
        """
        failed = self.status is RunStatus.FAILED
        if failed and self.failure is None:
            raise ValueError("a FAILED Run requires a failure reason")
        if not failed and self.failure is not None:
            raise ValueError(f"a failure reason belongs to a FAILED Run, not {self.status.value}")
        return self

    @model_validator(mode="after")
    def _clock_matches_status(self) -> Self:
        """Require the start and end stamps the status makes facts.

        Raises:
            ValueError: A started Run carries no start stamp, a queued
                Run carries one, a stopped Run carries no end stamp, an
                unstopped Run carries one, or the Run ended before it
                started.
        """
        queued = self.status is RunStatus.QUEUED
        if queued != (self.started_at is None):
            raise ValueError(f"status {self.status.value} disagrees with started_at")
        stopped = self.status in _TERMINAL
        if stopped != (self.ended_at is not None):
            raise ValueError(f"status {self.status.value} disagrees with ended_at")
        if (
            self.started_at is not None
            and self.ended_at is not None
            and self.ended_at < self.started_at
        ):
            raise ValueError("ended_at precedes started_at")
        return self


__all__ = [
    "MUTATING_PURPOSES",
    "SUSPENSION_ACTIVITY_BUCKETS",
    "ActivityBucket",
    "BatchScope",
    "CampaignScope",
    "ClaimScope",
    "EvidenceScope",
    "MilestoneScope",
    "QuestionScope",
    "ReleaseScope",
    "RepositoryScope",
    "Run",
    "RunPurpose",
    "RunScope",
    "RunStatus",
    "SuspensionReason",
    "TaskScope",
    "WorkspaceScope",
    "WriteSetPath",
]
