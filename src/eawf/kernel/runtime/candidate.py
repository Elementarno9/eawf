"""What a worker proposes for integration, and what the daemon seals.

Three records carry one candidate and each has exactly one writer.
:class:`CandidateSubmission` is the worker's claim, written once and never
edited: it states the tree the work produced and the paths it touched, and
its ``report_binding`` reads ``pending`` forever, because a submission that
could learn its own report would be a record the worker gets to revise
after the fact. :class:`CandidateReportBinding` is the daemon's, appended
when the Run's terminal report is accepted. :class:`CandidateBundle` is the
daemon's too, and is the only one of the three that means "this may be
integrated".

:class:`AcceptedReport` is a fourth record, filed against the Run rather
than a candidate: a Run's terminal report is accepted before anybody
names which candidate it is about, so the acceptance is recorded on its
own and a later request that binds a candidate's seal may name only the
Run and read the schema, digest and verdict back rather than repeat what
the daemon already checked.

The bundle is not a rename of the submission. It exists only once every
member of :class:`SealCheck` holds against durable records, so the three
facts that make a submission deliverable -- a report was accepted, it was
accepted about this tree, and the workspace the tree came from is still
the one the lease points at -- are proven rather than assumed. A single
unmet check leaves the submission standing and unsealed, which is the
state a retry or a repair starts from.

Identity is derived, not minted. A candidate is named by its Task and the
tree it produced, so two Runs that end at byte-identical trees for one
Task are one candidate however many times the provider was retried, and a
resubmission after a provider loss replays the standing record instead of
appending a second one.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import AfterValidator, Field, StringConstraints, model_validator

from eawf.kernel.runtime.compiled import canonical_digest
from eawf.kernel.runtime.lease import LeaseId, WorkspaceHandle
from eawf.kernel.runtime.provider import (
    ArtifactUrn,
    Digest,
    RuntimeRecord,
    SchemaUrn,
    reject_repeats,
)
from eawf.kernel.runtime.semantic import RepoRelativePath
from eawf.kernel.state.enums import AgentReportVerdict
from eawf.kernel.state.epoch2.base import StrictPositiveInt
from eawf.kernel.state.epoch2.urns import RunUrn, TaskUrn
from eawf.kernel.state.models import ShaStr
from eawf.kernel.state.types import UtcDatetime

#: Version of the three persisted candidate record shapes.
CANDIDATE_SCHEMA_VERSION: Final = "1"

#: A candidate's own identifier. Derived from the Task and the resulting
#: tree rather than minted, so the same work resubmitted names the same
#: candidate and a retry cannot produce a second one.
CandidateId = Annotated[str, StringConstraints(strict=True, pattern=r"^CND-[0-9a-f]{32}$")]

#: How many hex characters of the identity digest a candidate id carries.
_CANDIDATE_ID_WIDTH: Final = 32

#: The most paths one submission may name, matching the semantic tool's
#: own bound so a candidate filed natively and one filed through the
#: gateway are the same size of claim.
_MAX_CHANGED_PATHS: Final = 512


class SealCheck(StrEnum):
    """What a submission must prove before a bundle exists for it.

    Every member is a fact two durable records can be compared on, and
    every one fails closed: an absent binding or an unreadable lease is
    an unmet check rather than a check nobody could evaluate, because the
    thing being decided -- that this tree may be integrated -- is exactly
    what the missing record was supposed to establish.
    """

    REPORT_BOUND = "report_bound"
    TREE_DIGEST_AGREES = "tree_digest_agrees"
    VERDICT_ADMITS_DELIVERY = "verdict_admits_delivery"
    PATHS_WITHIN_WRITE_SET = "paths_within_write_set"
    WORKSPACE_GENERATION_CURRENT = "workspace_generation_current"
    BASE_COMMIT_AGREES = "base_commit_agrees"


class CandidateRefusal(StrEnum):
    """The stable codes a submission or a report binding is refused with."""

    LEASE_ABSENT = "candidate_lease_absent"
    PAYLOAD_CONFLICT = "candidate_payload_conflict"
    SUBMISSION_ABSENT = "candidate_submission_absent"
    BINDING_CONFLICT = "candidate_binding_conflict"
    SUBMISSION_NOT_COMMIT = "candidate_submission_not_commit"
    SUBMISSION_NOT_HEAD = "candidate_submission_not_head"
    SUBMISSION_OFF_BASE = "candidate_submission_off_base"
    REPORT_UNRESOLVED = "candidate_report_unresolved"


#: The verdicts that propose work for integration. A failed or blocked Run
#: states that its work is not ready, and sealing it would make the
#: report's own conclusion unreadable downstream.
DELIVERABLE_VERDICTS: Final[frozenset[AgentReportVerdict]] = frozenset(
    {AgentReportVerdict.PASS, AgentReportVerdict.PASS_WITH_FOLLOWUPS}
)

#: The changed-path tuple a submission and a bundle both carry.
ChangedPaths = Annotated[
    tuple[RepoRelativePath, ...],
    Field(min_length=1, max_length=_MAX_CHANGED_PATHS),
    AfterValidator(reject_repeats),
]


def candidate_identity(*, task_ref: str, resulting_tree_digest: str) -> str:
    """Return the candidate id one Task and one resulting tree name.

    Args:
        task_ref: The Task the work was done for.
        resulting_tree_digest: The digest of the tree the work produced.

    Returns:
        A :data:`CandidateId`. Equal inputs return equal ids, which is
        what makes a resubmission a replay rather than a duplicate.
    """
    body = canonical_digest(
        {"task_ref": task_ref, "resulting_tree_digest": resulting_tree_digest}
    ).removeprefix("sha256:")
    return f"CND-{body[:_CANDIDATE_ID_WIDTH]}"


class CandidateSubmission(RuntimeRecord):
    """One worker's claim that a tree is ready to be integrated.

    The record is written once and never edited. Everything the daemon
    later decides about it -- whether a report was accepted, whether it
    sealed -- lives in records of its own, so the claim stays exactly
    what the worker made.

    Attributes:
        payload_kind: The discriminator separating a submission from
            every other line of the run ledger.
        schema_version: Version of this record's shape.
        candidate_ref: The derived identity of the candidate.
        run_ref: The Run that submitted it.
        task_ref: The Task the work was done for.
        lease_id: The lease the daemon resolved for the Run at submit
            time. The worker does not name it: a claim that chose its own
            lease could claim a workspace it never held.
        workspace_handle: The opaque workspace the work was done in.
        workspace_generation: Which materialization of that workspace the
            handle pointed at when the claim was made.
        base_commit: The commit the workspace was materialized at.
        submission_ref: The artifact carrying the work itself.
        changed_paths: Every repository-relative path the work touched.
        resulting_tree_digest: The digest of the tree it produced.
        report_binding: Always ``pending``. A submission never learns its
            own report; the binding is a separate record, which is how
            this one stays immutable.
        submitted_at: When the daemon recorded the claim.
    """

    payload_kind: Literal["candidate_submission"] = "candidate_submission"
    schema_version: Literal["1"] = CANDIDATE_SCHEMA_VERSION
    candidate_ref: CandidateId
    run_ref: RunUrn
    task_ref: TaskUrn
    lease_id: LeaseId
    workspace_handle: WorkspaceHandle
    workspace_generation: StrictPositiveInt
    base_commit: ShaStr
    submission_ref: ArtifactUrn
    changed_paths: ChangedPaths
    resulting_tree_digest: Digest
    report_binding: Literal["pending"] = "pending"
    submitted_at: UtcDatetime

    @model_validator(mode="after")
    def _identity_is_derived_from_the_claim(self) -> Self:
        """Refuse a submission whose id does not name its own content.

        Raises:
            ValueError: The candidate id is not the one the Task and the
                resulting tree derive, which would let two trees share an
                identity and a replay return the wrong claim.
        """
        derived = candidate_identity(
            task_ref=str(self.task_ref), resulting_tree_digest=self.resulting_tree_digest
        )
        if derived != self.candidate_ref:
            raise ValueError(
                f"candidate_ref {self.candidate_ref} is not the identity this task and tree "
                f"derive ({derived})"
            )
        return self


class CandidateReportBinding(RuntimeRecord):
    """The accepted terminal report, bound to the candidate it is about.

    Attributes:
        payload_kind: The discriminator separating a binding line from
            every other line of the run ledger.
        schema_version: Version of this record's shape.
        candidate_ref: The candidate the report is about.
        run_ref: The Run whose report was accepted.
        report_schema_ref: The schema the accepted body satisfies.
        report_digest: The digest of that body.
        verdict: The verdict the body carried.
        resulting_tree_digest: The tree the report was accepted about,
            compared against the submission's rather than assumed equal
            to it: a report written before the last edit describes a tree
            that is not the one being proposed.
        bound_at: When the daemon recorded the acceptance.
    """

    payload_kind: Literal["candidate_report_binding"] = "candidate_report_binding"
    schema_version: Literal["1"] = CANDIDATE_SCHEMA_VERSION
    candidate_ref: CandidateId
    run_ref: RunUrn
    report_schema_ref: SchemaUrn
    report_digest: Digest
    verdict: AgentReportVerdict
    resulting_tree_digest: Digest
    bound_at: UtcDatetime


class AcceptedReport(RuntimeRecord):
    """A Run's own terminal report, accepted before any candidate names it.

    Filed once ``submit_report`` accepts, so a request that binds a
    candidate's report -- ``runtime.candidate.report.bind``, typically
    reached through ``/integrate seal`` -- may resolve the schema, the
    digest and the verdict from the Run alone when the caller does not
    repeat them.

    Attributes:
        payload_kind: The discriminator separating this row from every
            other line of the run ledger.
        schema_version: Version of this record's shape.
        run_ref: The Run whose report was accepted.
        report_schema_ref: The schema the accepted body satisfies.
        report_digest: The digest of that body.
        verdict: The verdict the body carried.
        accepted_at: When the daemon recorded the acceptance.
    """

    payload_kind: Literal["accepted_report"] = "accepted_report"
    schema_version: Literal["1"] = CANDIDATE_SCHEMA_VERSION
    run_ref: RunUrn
    report_schema_ref: SchemaUrn
    report_digest: Digest
    verdict: AgentReportVerdict
    accepted_at: UtcDatetime


class CandidateBundle(RuntimeRecord):
    """One sealed candidate: the single record integration may act on.

    A bundle exists only where every :class:`SealCheck` held, so its
    presence is the proof rather than a summary of one. It repeats the
    facts the checks were taken against so a later reader does not have
    to re-derive them from two other records to know what was sealed.

    Attributes:
        payload_kind: The discriminator separating a bundle line from
            every other line of the run ledger.
        schema_version: Version of this record's shape.
        candidate_ref: The candidate this seals.
        run_ref: The Run that submitted it.
        task_ref: The Task the work was done for.
        submission_ref: The artifact carrying the work.
        report_digest: The accepted report the seal rested on.
        verdict: The verdict that report carried.
        changed_paths: The paths the sealed work touched.
        resulting_tree_digest: The tree being proposed.
        base_commit: The commit that tree started from.
        workspace_generation: The materialization it was produced in.
        checks_passed: Every check that held, which is all of them.
        sealed_at: When the daemon sealed it.
    """

    payload_kind: Literal["candidate_bundle"] = "candidate_bundle"
    schema_version: Literal["1"] = CANDIDATE_SCHEMA_VERSION
    candidate_ref: CandidateId
    run_ref: RunUrn
    task_ref: TaskUrn
    submission_ref: ArtifactUrn
    report_digest: Digest
    verdict: AgentReportVerdict
    changed_paths: ChangedPaths
    resulting_tree_digest: Digest
    base_commit: ShaStr
    workspace_generation: StrictPositiveInt
    checks_passed: Annotated[tuple[SealCheck, ...], Field(min_length=1)]
    sealed_at: UtcDatetime

    @model_validator(mode="after")
    def _seal_names_every_check(self) -> Self:
        """Refuse a bundle that does not name every declared check.

        Raises:
            ValueError: A check is missing or repeated, which would let a
                bundle claim a seal one of the checks never took.
        """
        declared = tuple(SealCheck)
        if self.checks_passed != declared:
            missing = ", ".join(
                check.value for check in declared if check not in self.checks_passed
            )
            raise ValueError(
                "a sealed bundle names every check in declaration order; "
                f"this one is missing or reorders {missing or 'none'}"
            )
        return self


__all__ = [
    "CANDIDATE_SCHEMA_VERSION",
    "DELIVERABLE_VERDICTS",
    "AcceptedReport",
    "CandidateBundle",
    "CandidateId",
    "CandidateRefusal",
    "CandidateReportBinding",
    "CandidateSubmission",
    "ChangedPaths",
    "SealCheck",
    "candidate_identity",
]
