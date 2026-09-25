"""``runtime.candidate.*``: record one claim, then decide whether it seals.

Two verbs and a strict order between them. ``runtime.candidate.submit``
writes the worker's claim and nothing else: it takes no verdict, reaches
no check and seals nothing, because a submission that could seal itself
would be a worker deciding its own work is deliverable.
``runtime.candidate.report.bind`` is the daemon's side -- it records the
accepted terminal report and then, in the same pass, puts the standing
claim through every seal check. A bundle appears exactly where all six
hold; one unmet check leaves the claim standing and unsealed, which is
the state a repair or a second acceptance starts from.

Neither verb lets the caller name its own lease. The daemon resolves the
Run's live lease itself and copies the workspace, its generation and its
base commit off that record, so a claim cannot assert a workspace it
never held. The worker names only what it alone knows: the artifact, the
paths and the resulting tree.

A provider loss does not cost a candidate. Identity is derived from the
Task and the resulting tree, so the same work presented again -- by the
same Run after a resume, or by the recovery Run a linked retry created --
finds the standing submission and replays it. Only a claim that names
that identity with different content is refused, because two different
claims under one identity is the one case where replaying would answer
for something the caller did not ask.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from eawf.kernel.runtime.candidate import (
    CandidateBundle,
    CandidateId,
    CandidateRefusal,
    CandidateReportBinding,
    CandidateSubmission,
    ChangedPaths,
    SealCheck,
    candidate_identity,
)
from eawf.kernel.runtime.lease import WorkLease
from eawf.kernel.runtime.provider import ArtifactUrn, Digest, SchemaUrn
from eawf.kernel.state.enums import AgentReportVerdict
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey
from eawf.kernel.state.epoch2.urns import RunUrn, TaskUrn
from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records
from eawf.runtime.candidate.seal import (
    SealInputs,
    binding_of,
    binding_record,
    bundle_of,
    bundle_record,
    seal_candidate,
    submission_of,
    submission_record,
)
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.native_dispatch import active_lease_of, run_ledger, stored_run
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_mutator

logger = logging.getLogger(__name__)


#: The verb a worker files its finished work under.
CANDIDATE_SUBMIT_METHOD: Final = "runtime.candidate.submit"

#: The verb that accepts a Run's terminal report and attempts the seal.
CANDIDATE_REPORT_BIND_METHOD: Final = "runtime.candidate.report.bind"

#: The client's name for one request, shared by both verbs.
IdempotencyKey = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]


class CandidateSubmitParams(BaseModel):
    """Params of :data:`CANDIDATE_SUBMIT_METHOD`.

    Attributes:
        urn: The Run submitting the work.
        actor: Who asked.
        idempotency_key: The client's name for this request.
        task_ref: The Task the work was done for, checked against the
            lease rather than trusted.
        submission_ref: The artifact carrying the work.
        changed_paths: Every repository-relative path it touched.
        resulting_tree_digest: The digest of the tree it produced.
    """

    model_config = ConfigDict(extra="forbid")

    urn: RunUrn
    actor: PrincipalKey
    idempotency_key: IdempotencyKey
    task_ref: TaskUrn
    submission_ref: ArtifactUrn
    changed_paths: ChangedPaths
    resulting_tree_digest: Digest


class CandidateReportParams(BaseModel):
    """Params of :data:`CANDIDATE_REPORT_BIND_METHOD`.

    Attributes:
        urn: The Run whose report was accepted. It is the submitting Run
            after a resume and the recovery Run after a linked retry, so
            it is recorded rather than required to match.
        actor: Who asked.
        idempotency_key: The client's name for this request.
        candidate_ref: The candidate the report is about.
        report_schema_ref: The schema the accepted body satisfies.
        report_digest: The digest of that body.
        verdict: The verdict the body carried.
        resulting_tree_digest: The tree the report was written about.
    """

    model_config = ConfigDict(extra="forbid")

    urn: RunUrn
    actor: PrincipalKey
    idempotency_key: IdempotencyKey
    candidate_ref: CandidateId
    report_schema_ref: SchemaUrn
    report_digest: Digest
    verdict: AgentReportVerdict
    resulting_tree_digest: Digest


class CandidateSubmitAnswer(BaseModel):
    """What one submission answers with.

    Attributes:
        candidate_ref: The derived identity of the candidate.
        run_ref: The Run the standing submission belongs to, which after
            a replay is the Run that first made the claim.
        replayed: Whether a standing submission answered the request.
        report_binding: The submission's binding state, always pending.
        submission: The standing submission, as recorded.
        reason: One sentence an operator reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_ref: CandidateId
    run_ref: RunUrn
    replayed: bool
    report_binding: Literal["pending"]
    submission: dict[str, Any]
    reason: str


class CandidateSealAnswer(BaseModel):
    """What one report acceptance answers with.

    Attributes:
        candidate_ref: The candidate the report was bound to.
        sealed: Whether a bundle now exists for it.
        replayed: Whether the candidate was already sealed when asked.
        failed_checks: Every seal check that did not hold, in declaration
            order. Empty exactly when the candidate is sealed.
        bundle: The sealed bundle, or ``None``.
        reason: One sentence an operator reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_ref: CandidateId
    sealed: bool
    replayed: bool
    failed_checks: tuple[SealCheck, ...] = ()
    bundle: dict[str, Any] | None = None
    reason: str


def _refused(code: CandidateRefusal, detail: str) -> DaemonValidationError:
    """Return the wire form of one candidate refusal."""
    return DaemonValidationError(f"validation_failed: {code.value}: {detail}")


def _params[ParamsT: BaseModel](model: type[ParamsT], params: dict[str, Any]) -> ParamsT:
    """Validate request params, dropping the key the fence already used.

    Raises:
        DaemonValidationError: The request does not parse. The pydantic
            detail is reduced to field paths so the refusal never repeats
            a submitted value into a log.
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


def _lease_for(context: Epoch2RootContext, *, urn: RunUrn, now: datetime) -> WorkLease:
    """Return the Run's live lease.

    Raises:
        DaemonValidationError: The Run holds no active lease, so there is
            no workspace whose contents a claim could be about.
    """
    lease = active_lease_of(context, run_ref=str(urn), now=now)
    if lease is None:
        raise _refused(
            CandidateRefusal.LEASE_ABSENT,
            f"run {urn.entity_key!r} holds no active lease, so nothing it submits can be shown "
            "to have come from a workspace the daemon issued",
        )
    return lease


def submit_candidate(
    context: Epoch2RootContext, args: CandidateSubmitParams, *, now: datetime
) -> CandidateSubmitAnswer:
    """Record one worker's claim that a tree is ready to be integrated.

    Nothing is checked against the report here and nothing is sealed: the
    claim is written as made, and every judgement of it happens when the
    terminal report is accepted.

    Args:
        context: The native context of the canary the Run lives in.
        args: The validated submission request.
        now: The stamp the claim is recorded at.

    Returns:
        The standing submission, whether this request wrote it or an
        earlier one did.

    Raises:
        DaemonValidationError: The Run holds no record or no lease, the
            lease is for another Task, or the identity is already held by
            a claim with different content.
    """
    candidate_ref = candidate_identity(
        task_ref=str(args.task_ref), resulting_tree_digest=args.resulting_tree_digest
    )
    with context.session([args.urn]) as session:
        records = read_ledger_records(run_ledger(session))
        standing = submission_of(records, candidate_ref)
        if standing is not None:
            return _replayed_submission(standing, args)
        stored_run(session, records, args.urn)
    lease = _lease_for(context, urn=args.urn, now=now)
    if lease.task_ref != args.task_ref:
        raise _refused(
            CandidateRefusal.PAYLOAD_CONFLICT,
            f"the lease of run {args.urn.entity_key!r} is held for another Task, so this claim "
            "would name work the workspace was not borrowed for",
        )
    submission = CandidateSubmission(
        candidate_ref=candidate_ref,
        run_ref=args.urn,
        task_ref=args.task_ref,
        lease_id=lease.lease_id,
        workspace_handle=lease.workspace_handle,
        workspace_generation=lease.workspace_generation,
        base_commit=lease.base_commit,
        submission_ref=args.submission_ref,
        changed_paths=args.changed_paths,
        resulting_tree_digest=args.resulting_tree_digest,
        submitted_at=now,
    )
    with context.session([args.urn]) as session:
        commit_ledger_append(session, submission_record(submission))
    logger.info(
        f"submit_candidate recorded candidate={candidate_ref} run={args.urn.entity_key!r} "
        f"paths={len(submission.changed_paths)}"
    )
    return CandidateSubmitAnswer(
        candidate_ref=candidate_ref,
        run_ref=submission.run_ref,
        replayed=False,
        report_binding=submission.report_binding,
        submission=submission.model_dump(mode="json"),
        reason=(
            f"candidate {candidate_ref} is recorded with its report binding pending; no bundle "
            "is sealed until a terminal report is accepted for it"
        ),
    )


def _replayed_submission(
    standing: CandidateSubmission, args: CandidateSubmitParams
) -> CandidateSubmitAnswer:
    """Answer a resubmission of work the ledger already holds.

    Raises:
        DaemonValidationError: The request names this identity with other
            content, which is a second claim rather than the first one
            presented again.
    """
    if (
        standing.submission_ref != args.submission_ref
        or standing.changed_paths != args.changed_paths
    ):
        raise _refused(
            CandidateRefusal.PAYLOAD_CONFLICT,
            f"candidate {standing.candidate_ref} is already held by a claim naming other "
            "content, so replaying it would answer for a request nobody made",
        )
    return CandidateSubmitAnswer(
        candidate_ref=standing.candidate_ref,
        run_ref=standing.run_ref,
        replayed=True,
        report_binding=standing.report_binding,
        submission=standing.model_dump(mode="json"),
        reason=(
            f"candidate {standing.candidate_ref} was already submitted by run "
            f"{standing.run_ref.entity_key}, so this request replays it and duplicates nothing"
        ),
    )


def accept_report(
    context: Epoch2RootContext, args: CandidateReportParams, *, now: datetime
) -> CandidateSealAnswer:
    """Bind one accepted terminal report, then attempt the seal.

    Args:
        context: The native context of the canary the Run lives in.
        args: The validated acceptance request.
        now: The stamp the binding and any seal are taken at.

    Returns:
        Whether the candidate sealed, and every check that did not hold.

    Raises:
        DaemonValidationError: No submission stands under the named
            candidate, or a different report is already bound to it.
    """
    with context.session([args.urn]) as session:
        records = read_ledger_records(run_ledger(session))
        submission = submission_of(records, args.candidate_ref)
        if submission is None:
            raise _refused(
                CandidateRefusal.SUBMISSION_ABSENT,
                f"no candidate submission stands under {args.candidate_ref}, so there is nothing "
                "for this report to be about",
            )
        sealed = bundle_of(records, args.candidate_ref)
        if sealed is not None:
            return _replayed_seal(sealed)
        binding = _bound_report(session, records, args, now=now)
    lease = active_lease_of(context, run_ref=str(submission.run_ref), now=now)
    outcome = seal_candidate(
        SealInputs(submission=submission, binding=binding, lease=lease), now=now
    )
    if outcome.bundle is None:
        named = ", ".join(check.value for check in outcome.failed_checks)
        return CandidateSealAnswer(
            candidate_ref=args.candidate_ref,
            sealed=False,
            replayed=False,
            failed_checks=outcome.failed_checks,
            reason=(
                f"the report is bound and seal checks {named} do not hold, so candidate "
                f"{args.candidate_ref} stands unsealed"
            ),
        )
    with context.session([args.urn]) as session:
        commit_ledger_append(session, bundle_record(outcome.bundle))
    logger.info(
        f"accept_report sealed candidate={args.candidate_ref} run={args.urn.entity_key!r} "
        f"verdict={binding.verdict.value}"
    )
    return _sealed_answer(outcome.bundle, replayed=False)


def _bound_report(
    session: RootSession,
    records: tuple[LedgerRecord, ...],
    args: CandidateReportParams,
    *,
    now: datetime,
) -> CandidateReportBinding:
    """Return the report binding of this candidate, appending it if new.

    A binding already standing for the same report is kept rather than
    written twice, so a second acceptance of one report is a second seal
    attempt over unchanged evidence.

    Raises:
        DaemonValidationError: Another report is already bound to the
            candidate, which two acceptances of one Run cannot both be.
    """
    binding = CandidateReportBinding(
        candidate_ref=args.candidate_ref,
        run_ref=args.urn,
        report_schema_ref=args.report_schema_ref,
        report_digest=args.report_digest,
        verdict=args.verdict,
        resulting_tree_digest=args.resulting_tree_digest,
        bound_at=now,
    )
    standing = binding_of(records, args.candidate_ref)
    if standing is None:
        commit_ledger_append(session, binding_record(binding))
        return binding
    if _report_identity(standing) != _report_identity(binding):
        raise _refused(
            CandidateRefusal.BINDING_CONFLICT,
            f"candidate {args.candidate_ref} is already bound to another report, so this one "
            "would replace evidence a seal decision already rested on",
        )
    return standing


def _report_identity(binding: CandidateReportBinding) -> tuple[str, str, str, str]:
    """Return what makes two report bindings the same acceptance.

    Which Run presented the report and when it was recorded are not part
    of it: a recovery Run may present the predecessor's report, and that
    is the same acceptance rather than a conflicting one.
    """
    return (
        binding.report_schema_ref,
        binding.report_digest,
        binding.verdict.value,
        binding.resulting_tree_digest,
    )


def _replayed_seal(bundle: CandidateBundle) -> CandidateSealAnswer:
    """Answer an acceptance for a candidate that is already sealed."""
    return _sealed_answer(bundle, replayed=True)


def _sealed_answer(bundle: CandidateBundle, *, replayed: bool) -> CandidateSealAnswer:
    """Build the answer one sealed bundle returns."""
    return CandidateSealAnswer(
        candidate_ref=bundle.candidate_ref,
        sealed=True,
        replayed=replayed,
        bundle=bundle.model_dump(mode="json"),
        reason=(
            f"candidate {bundle.candidate_ref} was already sealed"
            if replayed
            else f"every seal check holds, so candidate {bundle.candidate_ref} is sealed"
        ),
    )


@native_mutator(CANDIDATE_SUBMIT_METHOD)
async def _submit_candidate(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Record one worker's claim, immutably and with no report bound to it."""
    args = _params(CandidateSubmitParams, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(submit_candidate, context, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


@native_mutator(CANDIDATE_REPORT_BIND_METHOD)
async def _bind_candidate_report(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Bind the accepted report and seal the candidate if every check holds."""
    args = _params(CandidateReportParams, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(accept_report, context, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


__all__ = [
    "CANDIDATE_REPORT_BIND_METHOD",
    "CANDIDATE_SUBMIT_METHOD",
    "CandidateReportParams",
    "CandidateSealAnswer",
    "CandidateSubmitAnswer",
    "CandidateSubmitParams",
    "accept_report",
    "submit_candidate",
]
