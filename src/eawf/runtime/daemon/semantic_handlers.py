"""What an admitted semantic call is actually answered with.

The gateway decides whether a call may be answered; this module answers
it. The two are separate files because they fail differently: a missing
guard admits a call nobody checked, while a missing handler admits a call
nobody can serve, and the second must never be papered over with a
plausible-looking output. :data:`BROKERED_TOOLS` is therefore derived
from the handler table rather than declared beside it -- a tool is
brokered exactly when a function answers it, so the set cannot grow past
what is implemented.

Five tools are answered here today.

``budget_status`` reports only what the daemon measured itself. Wall time
is the difference between two stamps it wrote and the call count is the
receipts it holds; the token, cost and output ceilings are reported
absent rather than as zero, because a budget that reads as untouched is
how one is overrun while it looks observed.

``submit_plan`` routes a proposal into the PlanRevision validator and
hands back its findings. It records nothing: the tool's contract is that
the daemon validates the artifact before any proposal exists, and
creating the revision record is an operator-authority verb
(``planning.plan_revision.submit``) rather than something a worker earns
by having its plan validate. So acceptance here means "this plan would
submit", and the findings are the refusal the submission would have
produced -- the same function decides both, so the preview cannot drift
from the act.

``submit_candidate`` records one worker's claim that a tree is ready to
be integrated. Sealing is a second, separate act gated on an accepted
terminal report and is not performed here, so the handler reports the
standing bundle when one already exists and reports none otherwise; the
claim itself is written once and replayed rather than duplicated when the
same candidate is submitted again with the same content. Before that
first write, the submission's commit is pinned under the candidate's ref
through the same check ``runtime.candidate.submit`` pins through, because
a claim this gateway wrote and a claim the JSON-RPC method wrote must be
equally resolvable once the leased branch is gone. A submission naming an
identity another claim already holds with different content, or naming a
commit the pin check will not hold, reaches no handler answer at all: it
raises :class:`HandlerRefusalError`, which the gateway lets propagate
rather than files as a receipt, because there is no output that could
report it without inventing one.

``submit_report`` does not read the report body either. The body travels
by reference, so the one thing this handler alone can check is that the
call names the exact schema and contract this Run was actually sealed
under -- the two facts the capsule already pins and the payload cannot
spoof. Acceptance does record the report's schema, digest and verdict
against the Run, once, so the separate, operator-driven act that binds
an accepted report to a candidate's seal decision
(:data:`eawf.runtime.daemon.methods.candidate.CANDIDATE_REPORT_BIND_METHOD`)
can read them back rather than take them typed by hand. Recording them
is not the same as sealing anything: acceptance here answers only "this
Run may file this report", never "this candidate is sealed".

``submit_evidence`` promotes a verified spike's measured contracts onto
the v1 evidence path, the same store a plan's approval resolves
``contract_refs`` against. It reads the filed :class:`SpikeReport` the
way ``submit_plan`` reads a filed proposal, so an unverified report, a
stale digest, or a contract measured in another repository is reported
as a finding rather than raised; a contract this daemon already promoted
is the one refusal that comes back from the v1 mutator underneath it
instead. The report itself reaches this ledger through
``runtime.evidence.spike_report.file`` (:func:`file_spike_report`), a
plain native mutator rather than a sixth brokered tool -- the same split
that keeps ``runtime.candidate.submit`` apart from ``submit_candidate``,
because filing is a record of what a Run produced, not a judged call.

No handler opens a session of its own. The gateway is already inside one
with the Run's locks and the document lock held, and a nested session
would block on the lock the caller is holding. What a handler needs from
the tree it reads through the session it was handed, and the Run's
active lease -- already resolved by the gateway's own lease check -- is
handed alongside it for the same reason.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, model_validator

from eawf.kernel.runtime.candidate import AcceptedReport, CandidateSubmission, candidate_identity
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.compiled import JsonPointer, canonical_digest
from eawf.kernel.runtime.lease import WorkLease
from eawf.kernel.runtime.provider import ArtifactUrn, Digest, RuntimeRecord
from eawf.kernel.runtime.semantic import (
    BudgetStatusOutput,
    BudgetUsage,
    FindingCode,
    SemanticCall,
    SemanticToolId,
    SemanticToolOutput,
    SubmitCandidateInput,
    SubmitCandidateOutput,
    SubmitEvidenceInput,
    SubmitEvidenceOutput,
    SubmitPlanInput,
    SubmitPlanOutput,
    SubmitReportInput,
    SubmitReportOutput,
    ValidationFinding,
)
from eawf.kernel.spec.measured_contract import SpikeReport
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey
from eawf.kernel.state.epoch2.run import Run
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.kernel.state.io import StateValidationError
from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.candidate.seal import (
    accepted_report_of,
    accepted_report_record,
    bundle_of,
    submission_of,
    submission_record,
)
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession, canonical_entity_urn
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_mutator
from eawf.runtime.integration.git_workspace import CandidatePinError, pin_submission_commit
from eawf.runtime.lock import portalock
from eawf.surfaces.cli.errors import UserError
from eawf.surfaces.cli.errors import ValidationError as CliValidationError
from eawf.workflow.evidence._io import append_jsonl, atomic_write_state, load_state, store_paths
from eawf.workflow.evidence.measured_contract import submit_evidence as promote_spike_evidence
from eawf.workflow.planning.apply import PlanRevisionProposal, validate_plan_proposal
from eawf.workflow.planning.revision import PlanRefusal, PlanRefusalCode

logger = logging.getLogger(__name__)


#: The discriminator separating a plan proposal from the other payload
#: kinds the artifact ledger may come to hold.
PLAN_PROPOSAL_PAYLOAD_KIND: Final = "plan_proposal"

#: The discriminator separating a spike report from the other payload
#: kinds the artifact ledger may come to hold.
SPIKE_REPORT_PAYLOAD_KIND: Final = "spike_report"

#: A refusal ``UserError.kind`` that does not itself fit the
#: ``FindingCode`` grammar, mapped to one that does. ``InvalidInput`` is
#: :func:`~eawf.workflow.evidence.artifact.add_artifact`'s generic
#: duplicate-id guard, reached when a contract this daemon already
#: promoted is submitted again.
_EVIDENCE_REFUSAL_CODES: Final[Mapping[str, str]] = MappingProxyType(
    {"InvalidInput": "contract_already_promoted"}
)

#: How long a finding message may be before it is trimmed to fit the
#: bounded-text field the output carries.
_MESSAGE_LIMIT: Final = 500

#: Validates one derived finding code against the closed grammar the
#: output field pins, so a refusal code that cannot be reported is a
#: startup failure rather than a validation error under load.
_FINDING_CODES: Final = TypeAdapter[Any](FindingCode)

#: Validates a derived pointer against the grammar the finding field pins.
_POINTERS: Final = TypeAdapter[Any](JsonPointer)

#: The finding code a report is refused with when it names a schema other
#: than the one this Run's capsule pins.
_REPORT_SCHEMA_MISMATCH: Final = "report_schema_mismatch"

#: The finding code a report is refused with when it names a contract
#: digest other than the one this Run was actually sealed under.
_REPORT_CONTRACT_MISMATCH: Final = "report_contract_mismatch"

#: The finding code a report is refused with when its Run already holds an
#: accepted report naming another body or verdict.
_REPORT_ALREADY_ACCEPTED: Final = "report_already_accepted"


class HandlerTableError(RuntimeError):
    """A handler table is not total over the vocabulary it covers."""


class PlanProposalArtifact(RuntimeRecord):
    """One planner proposal, filed as an artifact a call may name.

    The proposal travels by reference because a plan is larger than an
    envelope should be, so the envelope names an artifact and the daemon
    reads the body from its own ledger. The body is kept as submitted
    rather than as a parsed proposal: a proposal that does not validate
    must still be storable, or the schema finding the tool is defined to
    return could never be produced.

    Attributes:
        payload_kind: The discriminator separating this row from
            anything else the artifact ledger holds.
        schema_version: Version of this record's shape.
        artifact_ref: The reference a semantic call names it by.
        content_digest: The digest of ``proposal`` in canonical form.
        proposal: The submitted create document, unparsed.
    """

    payload_kind: Literal["plan_proposal"] = PLAN_PROPOSAL_PAYLOAD_KIND
    schema_version: Literal["plan-proposal/v1"] = "plan-proposal/v1"
    artifact_ref: ArtifactUrn
    content_digest: Digest
    proposal: Mapping[str, Any]

    @model_validator(mode="after")
    def _digest_covers_the_body(self) -> Self:
        """Bind the recorded digest to the body stored beside it.

        Raises:
            ValueError: The digest does not cover the proposal, which
                would let a caller's digest match a row whose body was
                replaced.
        """
        if canonical_digest(dict(self.proposal)) != self.content_digest:
            raise ValueError("content_digest does not cover the proposal")
        return self


class SpikeReportArtifact(RuntimeRecord):
    """One verified spike's report, filed as an artifact a call may name.

    Mirrors :class:`PlanProposalArtifact`: the report travels by reference
    because a spike may carry several contracts, and the body is kept as
    submitted rather than as a parsed :class:`SpikeReport` so a report
    that fails to validate is still storable and reportable as a finding.

    Attributes:
        payload_kind: The discriminator separating this row from
            anything else the artifact ledger holds.
        schema_version: Version of this record's shape.
        artifact_ref: The reference a semantic call names it by.
        content_digest: The digest of ``report`` in canonical form.
        report: The submitted spike report, unparsed.
    """

    payload_kind: Literal["spike_report"] = SPIKE_REPORT_PAYLOAD_KIND
    schema_version: Literal["spike-report/v1"] = "spike-report/v1"
    artifact_ref: ArtifactUrn
    content_digest: Digest
    report: Mapping[str, Any]

    @model_validator(mode="after")
    def _digest_covers_the_body(self) -> Self:
        """Bind the recorded digest to the body stored beside it.

        Raises:
            ValueError: The digest does not cover the report, which would
                let a caller's digest match a row whose body was replaced.
        """
        if canonical_digest(dict(self.report)) != self.content_digest:
            raise ValueError("content_digest does not cover the report")
        return self


@dataclass(frozen=True, slots=True)
class HandlerInputs:
    """Everything a handler may read, gathered by the gateway.

    Attributes:
        session: The locked pass the gateway opened. A handler reads
            through it and never opens one of its own.
        call: The envelope that was admitted.
        capsule: The authority it runs under.
        run: The stored Run record.
        calls_so_far: How many receipts the Run already holds.
        now: The instant the answer is stamped at.
        lease: The Run's active workspace lease, already resolved by the
            gateway's own lease check. ``None`` for a tool the lease check
            does not gate.
    """

    session: RootSession
    call: SemanticCall
    capsule: AuthorityCapsule
    run: Run
    calls_so_far: int
    now: datetime
    lease: WorkLease | None = None


class HandlerRefusalError(ValueError):
    """A call reached its handler and still cannot be answered as made.

    Raised instead of returned so the caller's session exits with no
    receipt appended -- the same "nothing is written" guarantee a
    pre-handler refusal has, for the one class of refusal that can only be
    known once the handler reads the tree.

    Attributes:
        code: The stable code a client branches on.
        detail: The operator-facing explanation.
    """

    def __init__(self, *, code: str, detail: str) -> None:
        """Bind the typed code to its one-sentence detail."""
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _budget_status(inputs: HandlerInputs) -> SemanticToolOutput:
    """Return what this Run has measurably spent and what remains of it."""
    ceiling = inputs.capsule.budget.wall_seconds
    started = inputs.run.started_at
    elapsed = 0 if started is None else int((inputs.now - started).total_seconds())
    return BudgetStatusOutput(
        tool_id="budget_status",
        used=BudgetUsage(wall_seconds=elapsed, tool_calls=inputs.calls_so_far),
        remaining=BudgetUsage(wall_seconds=max(0, ceiling - elapsed)),
        quality="measured",
        includes_children=False,
    )


def _submit_plan(inputs: HandlerInputs) -> SemanticToolOutput:
    """Validate the named proposal and return the verdict with its findings."""
    payload = inputs.call.payload
    assert isinstance(payload, SubmitPlanInput), "the envelope binds the payload to its tool"
    artifact = _plan_proposal_artifact(inputs.session, ref=payload.proposal_ref)
    if artifact is None:
        return _refused_plan(
            PlanRefusalCode.IDENTITY_NOT_FOUND,
            message=f"no plan proposal is filed under {payload.proposal_ref}",
            field_path="/proposal_ref",
        )
    if artifact.content_digest != payload.proposal_digest:
        return _refused_plan(
            PlanRefusalCode.PROOF_STALE,
            message="the call names a digest the filed proposal does not have",
            field_path="/proposal_digest",
        )
    try:
        proposal = PlanRevisionProposal.model_validate(dict(artifact.proposal))
    except ValidationError as error:
        return _refused_plan(
            PlanRefusalCode.SCHEMA_VALIDATION_FAILED,
            message="the filed proposal is not a plan revision this daemon can read",
            field_path=_pointer(error.errors()[0]["loc"]),
        )
    outcome = validate_plan_proposal(
        inputs.session.read_document(), proposal=proposal, at=inputs.now
    )
    if isinstance(outcome, PlanRefusal):
        logger.info(f"_submit_plan refused guard={outcome.guard}")
        return _refused_plan(outcome.code, message=outcome.detail)
    return SubmitPlanOutput(
        tool_id="submit_plan",
        accepted=True,
        proposal_ref=proposal.body.milestone_urn,
    )


def _submit_candidate(inputs: HandlerInputs) -> SemanticToolOutput:
    """Record one worker's claim, or answer the standing one it repeats.

    Raises:
        HandlerRefusalError: The lease this call names is not the one the
            Run holds, the submission names no commit or one that is not
            the leased worktree's own HEAD descending from its base, or
            the candidate identity is already held by a claim naming
            other content.
    """
    payload = inputs.call.payload
    assert isinstance(payload, SubmitCandidateInput), "the envelope binds the payload to its tool"
    lease = inputs.lease
    assert lease is not None, "the lease check admits only a run holding an active lease"
    if (
        canonical_entity_urn(lease.task_ref) != canonical_entity_urn(payload.task_ref)
        or lease.lease_id != payload.lease_id
    ):
        raise HandlerRefusalError(
            code="candidate_lease_mismatch",
            detail=(
                f"run {inputs.run.key!r} holds lease {lease.lease_id!r} for task "
                f"{lease.task_ref.entity_key}, which is not what this call names"
            ),
        )
    candidate_ref = candidate_identity(
        task_ref=str(payload.task_ref), resulting_tree_digest=payload.resulting_tree_digest
    )
    records = read_ledger_records(inputs.session.ledger_path(Epoch2Collection.RUN))
    standing = submission_of(records, candidate_ref)
    if standing is None:
        try:
            pin_submission_commit(
                inputs.session.context,
                lease=lease,
                candidate_ref=candidate_ref,
                submission_ref=payload.submission_ref,
            )
        except CandidatePinError as error:
            raise HandlerRefusalError(code=error.code.value, detail=error.detail) from error
        standing = CandidateSubmission(
            candidate_ref=candidate_ref,
            run_ref=inputs.call.run_ref,
            task_ref=payload.task_ref,
            lease_id=lease.lease_id,
            workspace_handle=lease.workspace_handle,
            workspace_generation=lease.workspace_generation,
            base_commit=lease.base_commit,
            submission_ref=payload.submission_ref,
            changed_paths=payload.changed_paths,
            resulting_tree_digest=payload.resulting_tree_digest,
            submitted_at=inputs.now,
        )
        commit_ledger_append(inputs.session, submission_record(standing))
        logger.info(
            f"_submit_candidate recorded candidate={candidate_ref} "
            f"run={inputs.run.key!r} paths={len(standing.changed_paths)}"
        )
    elif (
        standing.submission_ref != payload.submission_ref
        or standing.changed_paths != payload.changed_paths
    ):
        raise HandlerRefusalError(
            code="candidate_payload_conflict",
            detail=(
                f"candidate {candidate_ref} is already held by a claim naming other "
                "content, so replaying it would answer for a request nobody made"
            ),
        )
    bundle = bundle_of(records, candidate_ref)
    return SubmitCandidateOutput(
        tool_id="submit_candidate",
        candidate_ref=candidate_ref,
        resulting_tree_digest=payload.resulting_tree_digest,
        sealed_at=None if bundle is None else bundle.sealed_at,
    )


def _submit_report(inputs: HandlerInputs) -> SemanticToolOutput:
    """Accept the Run's terminal report exactly when it names its own contract.

    The body travels by reference and is not read here, so acceptance
    checks only what the capsule already pins for this Run: the schema it
    must report against and the contract it was sealed under. Either
    mismatching is refused with a finding rather than accepted on a claim
    this handler cannot verify. Acceptance names the Run itself, which is
    the one entity a terminal report is ever about; no fresh identity is
    minted for the report body.

    Acceptance also records the report's schema, digest and verdict
    against the Run, once, so a request that later binds a candidate's
    seal to this Run's report -- typically ``/integrate seal``, presented
    only the Run -- can read them back instead of repeating what this
    handler already checked.

    A retry naming the standing report's digest and verdict is accepted
    again without a second row. A resubmission naming another body or
    verdict is refused rather than accepted and dropped, matching how a
    Run's candidate binding refuses a second, different report.
    """
    payload = inputs.call.payload
    assert isinstance(payload, SubmitReportInput), "the envelope binds the payload to its tool"
    if payload.report_schema_ref != inputs.capsule.report_schema_ref:
        return _refused_report(
            _REPORT_SCHEMA_MISMATCH,
            message=(
                f"this run is sealed to report against {inputs.capsule.report_schema_ref!r}, "
                f"not {payload.report_schema_ref!r}"
            ),
            field_path="/report_schema_ref",
        )
    if payload.contract_digest != inputs.capsule.contract_digest:
        return _refused_report(
            _REPORT_CONTRACT_MISMATCH,
            message="the call names a contract digest this run was not sealed under",
            field_path="/contract_digest",
        )
    run_ref = str(inputs.call.run_ref)
    records = read_ledger_records(inputs.session.ledger_path(Epoch2Collection.RUN))
    standing = accepted_report_of(records, run_ref)
    if standing is None:
        commit_ledger_append(
            inputs.session,
            accepted_report_record(
                AcceptedReport(
                    run_ref=inputs.call.run_ref,
                    report_schema_ref=payload.report_schema_ref,
                    report_digest=payload.body_digest,
                    verdict=payload.verdict,
                    accepted_at=inputs.now,
                )
            ),
        )
    elif standing.report_digest != payload.body_digest or standing.verdict != payload.verdict:
        # A seal reads the one standing report back, so accepting a different
        # body here without recording it would bind a report the worker has
        # already replaced.
        return _refused_report(
            _REPORT_ALREADY_ACCEPTED,
            message=(
                f"run {inputs.call.run_ref.entity_key!r} already holds an accepted report "
                f"with verdict {standing.verdict.value!r}; a Run files one terminal report"
            ),
            field_path=(
                "/body_digest" if standing.report_digest != payload.body_digest else "/verdict"
            ),
        )
    return SubmitReportOutput(
        tool_id="submit_report", accepted=True, report_ref=inputs.call.run_ref
    )


def _refused_report(code: str, *, message: str, field_path: str) -> SemanticToolOutput:
    """Return the refusal output one report finding produces."""
    return SubmitReportOutput(
        tool_id="submit_report",
        accepted=False,
        findings=(
            ValidationFinding(code=code, message=message[:_MESSAGE_LIMIT], field_path=field_path),
        ),
    )


def _evidence_finding_code(kind: str) -> str:
    """Return *kind* as a code the ``FindingCode`` grammar admits.

    Args:
        kind: A raised :class:`~eawf.surfaces.cli.errors.UserError`'s
            ``kind``.

    Returns:
        *kind* unchanged when it already fits the grammar, or its mapped
        equivalent from :data:`_EVIDENCE_REFUSAL_CODES`.
    """
    return _EVIDENCE_REFUSAL_CODES.get(kind, kind)


def _refused_evidence(*, code: str, message: str) -> SemanticToolOutput:
    """Return the refusal output one evidence-submission finding produces."""
    return SubmitEvidenceOutput(
        tool_id="submit_evidence",
        accepted=False,
        findings=(ValidationFinding(code=code, message=message[:_MESSAGE_LIMIT]),),
    )


def _state_write_refusal(error: StateValidationError | OSError) -> SemanticToolOutput:
    """Return the refusal a failed ``state.json`` write answers with.

    A leak refusal's text can quote the leaking value itself, so neither
    the receipt nor the daemon log carries it; only its kind is reported.

    Args:
        error: What the locked state writer raised.

    Returns:
        A refused evidence output naming one finding.
    """
    if isinstance(error, StateValidationError):
        logger.warning("_submit_evidence write_refused=leak")
        return _refused_evidence(
            code="state_leak_refused",
            message="the promotion would write a leak-shaped value into state.json",
        )
    logger.warning(f"_submit_evidence write_refused=os_error errno={error.errno}")
    return _refused_evidence(
        code="state_write_failed",
        message=f"state.json could not be written: {error.strerror or type(error).__name__}",
    )


def _submit_evidence(inputs: HandlerInputs) -> SemanticToolOutput:
    """Promote a verified SpikeReport's contracts through the v1 evidence path.

    Reads the filed report the same way ``_submit_plan`` reads a filed
    proposal, then hands each of its contracts to
    :func:`eawf.workflow.evidence.measured_contract.submit_evidence`
    against the v1 ``state.json`` beside this tree -- the same store
    :func:`eawf.workflow.planning.apply.approve_plan_revision` resolves a
    plan's ``contract_refs`` against, so a contract promoted here is
    immediately citable from a plan's approval.
    """
    payload = inputs.call.payload
    assert isinstance(payload, SubmitEvidenceInput), "the envelope binds the payload to its tool"
    artifact = _spike_report_artifact(inputs.session, ref=payload.spike_report_ref)
    if artifact is None:
        return _refused_evidence(
            code="identity_not_found",
            message=f"no spike report is filed under {payload.spike_report_ref}",
        )
    if artifact.content_digest != payload.spike_report_digest:
        return _refused_evidence(
            code="proof_stale",
            message="the call names a digest the filed report does not have",
        )
    try:
        report = SpikeReport.model_validate(dict(artifact.report))
    except ValidationError:
        return _refused_evidence(
            code="schema_validation_failed",
            message="the filed report is not a spike report this daemon can read",
        )
    state_path = inputs.session.context.identity.tree_root / "state.json"
    try:
        with portalock.acquire(state_path, timeout=5.0):
            try:
                state = load_state(state_path)
            except UserError, CliValidationError:
                return _refused_evidence(
                    code="no_promoted_contract_store",
                    message=(
                        f"{state_path} carries no readable v1 state to promote a contract into"
                    ),
                )
            if state.project is None:
                return _refused_evidence(
                    code="no_promoted_contract_store",
                    message=f"{state_path} carries no project to scope a promotion under",
                )
            try:
                promotions = promote_spike_evidence(
                    state, report=report, scope_id=state.project.code
                )
            except UserError as error:
                code = _evidence_finding_code(error.kind or "measured_contract_rejected")
                return _refused_evidence(code=code, message=str(error))
            # The locked, leak-refusing writer is the chokepoint every other
            # state.json mutator funnels through (see
            # eawf.kernel.state.io.write_state_unlocked); events append only
            # after it returns, so a refused leak or a failed write leaves no
            # orphan event behind it.
            try:
                atomic_write_state(state_path, state, native_authority=inputs.session.authority)
            except (StateValidationError, OSError) as error:
                return _state_write_refusal(error)
            paths = store_paths(state_path)
            for promotion in promotions:
                append_jsonl(paths[StoreKind.EVENT], promotion.artifact_event)
                append_jsonl(paths[StoreKind.EVIDENCE], promotion.evidence_envelope)
    except portalock.LockTimeout as error:
        return _refused_evidence(code="state_write_conflict", message=str(error))
    logger.info(f"_submit_evidence report_id={report.report_id!r} contracts={len(promotions)}")
    return SubmitEvidenceOutput(
        tool_id="submit_evidence",
        accepted=True,
        contract_refs=tuple(promotion.urn for promotion in promotions),
    )


#: Which function answers each brokered tool. A tool reaches a handler or
#: it reaches none: the gateway refuses an admitted call whose tool is
#: absent here rather than inventing an outcome for it.
SEMANTIC_HANDLERS: Final[Mapping[SemanticToolId, Callable[[HandlerInputs], SemanticToolOutput]]] = (
    MappingProxyType(
        {
            SemanticToolId.BUDGET_STATUS: _budget_status,
            SemanticToolId.SUBMIT_CANDIDATE: _submit_candidate,
            SemanticToolId.SUBMIT_EVIDENCE: _submit_evidence,
            SemanticToolId.SUBMIT_PLAN: _submit_plan,
            SemanticToolId.SUBMIT_REPORT: _submit_report,
        }
    )
)

#: The tools this daemon answers itself, derived from the handler table
#: so the two cannot disagree. Every other catalog tool needs a handler
#: written before it can be brokered, and an admitted call for one is
#: refused with no receipt rather than answered with a fabricated
#: outcome.
BROKERED_TOOLS: Final[frozenset[SemanticToolId]] = frozenset(SEMANTIC_HANDLERS)


def _plan_proposal_artifact(session: RootSession, *, ref: str) -> PlanProposalArtifact | None:
    """Return the plan proposal filed under *ref*, or ``None``.

    Raises:
        ValidationError: A line claims to be a plan proposal and does not
            validate as one, which means the ledger is corrupt rather
            than merely unfamiliar.
    """
    records = read_ledger_records(session.ledger_path(Epoch2Collection.ARTIFACT))
    for item in records:
        if item.payload.get("payload_kind") != PLAN_PROPOSAL_PAYLOAD_KIND:
            continue
        artifact = PlanProposalArtifact.model_validate(item.payload)
        if artifact.artifact_ref == ref:
            return artifact
    return None


def _record_plan_proposal_artifact(
    session: RootSession, artifact: PlanProposalArtifact, *, now: datetime
) -> None:
    """File one plan proposal as a line of the root's artifact ledger."""
    commit_ledger_append(
        session,
        LedgerRecord(
            collection=Epoch2Collection.ARTIFACT,
            record_key=artifact.artifact_ref,
            status=PLAN_PROPOSAL_PAYLOAD_KIND,
            recorded_at=now,
            payload=artifact.model_dump(mode="json"),
        ),
    )


def _spike_report_artifact(session: RootSession, *, ref: str) -> SpikeReportArtifact | None:
    """Return the spike report filed under *ref*, or ``None``.

    Raises:
        ValidationError: A line claims to be a spike report and does not
            validate as one, which means the ledger is corrupt rather
            than merely unfamiliar.
    """
    records = read_ledger_records(session.ledger_path(Epoch2Collection.ARTIFACT))
    for item in records:
        if item.payload.get("payload_kind") != SPIKE_REPORT_PAYLOAD_KIND:
            continue
        artifact = SpikeReportArtifact.model_validate(item.payload)
        if artifact.artifact_ref == ref:
            return artifact
    return None


def _record_spike_report_artifact(
    session: RootSession, artifact: SpikeReportArtifact, *, now: datetime
) -> None:
    """File one spike report as a line of the root's artifact ledger."""
    commit_ledger_append(
        session,
        LedgerRecord(
            collection=Epoch2Collection.ARTIFACT,
            record_key=artifact.artifact_ref,
            status=SPIKE_REPORT_PAYLOAD_KIND,
            recorded_at=now,
            payload=artifact.model_dump(mode="json"),
        ),
    )


#: The verb a Run files its own verified spike report under, so a later
#: ``submit_evidence`` call has something other than a test fixture to
#: resolve. Separate from the ``semantic.call`` gateway (rather than a
#: sixth brokered tool) because filing is a record of what a Run
#: produced, not a judged tool call -- the same distinction that keeps
#: ``runtime.candidate.submit`` apart from ``submit_candidate``.
EVIDENCE_SPIKE_REPORT_FILE_METHOD: Final = "runtime.evidence.spike_report.file"


class SpikeReportFileParams(BaseModel):
    """Params of :data:`EVIDENCE_SPIKE_REPORT_FILE_METHOD`.

    Attributes:
        urn: The Run filing its own report.
        actor: Who asked.
        artifact_ref: The reference a later ``submit_evidence`` call names
            this report by.
        report: The spike report body, unparsed -- kept as submitted so an
            invalid report is still storable and reportable as a finding
            when ``submit_evidence`` reads it back, the same reason
            :class:`SpikeReportArtifact` keeps it unparsed.
    """

    model_config = ConfigDict(extra="forbid")

    urn: RunUrn
    actor: PrincipalKey
    artifact_ref: ArtifactUrn
    report: Mapping[str, Any]


def file_spike_report(
    context: Epoch2RootContext, args: SpikeReportFileParams, *, now: datetime
) -> SpikeReportArtifact:
    """File one Run's spike report so a later ``submit_evidence`` call can resolve it.

    Written once and replayed rather than duplicated when the same ref is
    filed again with the same content, mirroring how a candidate
    submission's identity replay works -- a retry after a dropped
    response is a no-op rather than a conflict.

    Args:
        context: The native context of the canary the Run lives in.
        args: The validated filing request.
        now: The stamp the artifact is recorded at.

    Returns:
        The standing artifact, whether this request filed it or an
        earlier one did.

    Raises:
        DaemonValidationError: *artifact_ref* is already filed under
            different content, which would let a caller quietly repoint a
            reference ``submit_evidence`` has already resolved.
    """
    content_digest = canonical_digest(dict(args.report))
    with context.session([args.urn]) as session:
        standing = _spike_report_artifact(session, ref=args.artifact_ref)
        if standing is not None:
            if standing.content_digest != content_digest:
                raise DaemonValidationError(
                    f"validation_failed: spike_report_payload_conflict: {args.artifact_ref} "
                    "is already filed with different content"
                )
            return standing
        artifact = SpikeReportArtifact(
            artifact_ref=args.artifact_ref, content_digest=content_digest, report=args.report
        )
        _record_spike_report_artifact(session, artifact, now=now)
    logger.info(
        f"file_spike_report recorded artifact_ref={args.artifact_ref!r} run={args.urn.entity_key!r}"
    )
    return artifact


@native_mutator(EVIDENCE_SPIKE_REPORT_FILE_METHOD)
async def _file_spike_report(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Record one Run's spike report as an artifact ``submit_evidence`` may name."""
    try:
        args = SpikeReportFileParams.model_validate(
            {key: value for key, value in params.items() if key != REPO_ROOT_PARAM}
        )
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        raise DaemonValidationError(
            f"validation_failed: schema_validation_failed: check {', '.join(fields)}"
        ) from error
    context = ctx.native_root_context(authority.root)
    artifact = await asyncio.to_thread(file_spike_report, context, args, now=datetime.now(UTC))
    return artifact.model_dump(mode="json")


def _refused_plan(
    code: PlanRefusalCode, *, message: str, field_path: str | None = None
) -> SemanticToolOutput:
    """Return the refusal output one plan finding produces."""
    return SubmitPlanOutput(
        tool_id="submit_plan",
        accepted=False,
        findings=(
            ValidationFinding(
                code=code.value, message=message[:_MESSAGE_LIMIT], field_path=field_path
            ),
        ),
    )


def _pointer(loc: Sequence[str | int]) -> str | None:
    """Return the JSON pointer *loc* spells, or ``None`` when it spells none.

    A pydantic location can name a union member or a model class, which
    the pointer grammar does not admit. Reporting no path is better than
    reporting one no reader can resolve, so a location that does not fit
    is dropped rather than mangled.
    """
    pointer = "".join(f"/{part}" for part in loc)
    try:
        return str(_POINTERS.validate_python(pointer))
    except ValidationError:
        return None


def _compile_finding_codes() -> frozenset[PlanRefusalCode]:
    """Return the plan refusal codes once each is reportable as a finding.

    Returns:
        Every declared refusal code.

    Raises:
        HandlerTableError: A refusal code does not satisfy the finding
            grammar, so the one path that reports it would fail at the
            moment a plan was refused.
    """
    for code in PlanRefusalCode:
        try:
            _FINDING_CODES.validate_python(code.value)
        except ValidationError as error:
            raise HandlerTableError(
                f"plan refusal code {code.value!r} is not a reportable finding code"
            ) from error
    return frozenset(PlanRefusalCode)


REPORTABLE_PLAN_CODES: Final[frozenset[PlanRefusalCode]] = _compile_finding_codes()


__all__ = [
    "BROKERED_TOOLS",
    "EVIDENCE_SPIKE_REPORT_FILE_METHOD",
    "PLAN_PROPOSAL_PAYLOAD_KIND",
    "REPORTABLE_PLAN_CODES",
    "SEMANTIC_HANDLERS",
    "SPIKE_REPORT_PAYLOAD_KIND",
    "HandlerInputs",
    "HandlerRefusalError",
    "HandlerTableError",
    "PlanProposalArtifact",
    "SpikeReportArtifact",
    "SpikeReportFileParams",
    "file_spike_report",
]
