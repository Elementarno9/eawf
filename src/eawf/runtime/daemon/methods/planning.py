"""The three JSON-RPC verbs that move a plan revision to a real Milestone.

The verbs are deliberately separate calls. Submitting a plan, approving
it and applying it are three different acts by three possibly different
principals, and folding any pair of them into one call would let whoever
can do the first do the second -- which is exactly the authority boundary
approval exists to draw. The daemon never derives an approval from a
submission, and the apply reads its approval from the stored record
rather than from the request that asks for it.

Each verb sits behind the epoch-2 fence, so it cannot be reached on a
tree that has not been shown to hold native authority, and each runs its
transaction off the event loop because the transaction blocks on file
locks. A refusal is answered as the same machine envelope a commit is,
with a code from the closed native vocabulary: the planning refusal codes
are checked against that vocabulary at import, so a refusal cannot reach
a client carrying a code nobody declared.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Self

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError, model_validator

from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey, StrictPositiveInt
from eawf.kernel.state.epoch2.plan_revision import PlanRevisionKey, validate_plan_approver
from eawf.kernel.state.epoch2.urns import AnyEntityUrn
from eawf.kernel.state.epoch2.values import OwnerPrincipal
from eawf.runtime.daemon.epoch2_recovery import PROJECTION_DEGRADED, publish_projection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import CommittedTransaction
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.domain_envelope import (
    ENTITY_REF_WIDTH,
    ENVELOPE_SCHEMA_VERSION,
    UNNAMED_SUBJECT,
    DomainEnvelope,
    DomainError,
    DomainErrorCode,
    DomainStatus,
    accepted_envelope,
)
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_mutator
from eawf.workflow.planning.apply import (
    PlanRevisionProposal,
    apply_plan_revision,
    approve_plan_revision,
    submit_plan_revision,
)
from eawf.workflow.planning.revision import PlanRefusal, PlanRefusalCode

logger = logging.getLogger(__name__)


#: The dotted JSON-RPC name of each plan-revision verb.
PLAN_SUBMIT_METHOD: Final = "planning.plan_revision.submit"
PLAN_APPROVE_METHOD: Final = "planning.plan_revision.approve"
PLAN_APPLY_METHOD: Final = "planning.plan_revision.apply"

#: Every registered plan-revision method, in registration order.
PLANNING_METHODS: Final[tuple[str, ...]] = (
    PLAN_SUBMIT_METHOD,
    PLAN_APPROVE_METHOD,
    PLAN_APPLY_METHOD,
)


def _compile_planning_codes() -> Mapping[PlanRefusalCode, DomainErrorCode]:
    """Return the planning refusal codes as declared domain codes.

    Returns:
        Every planning code mapped to the domain code of the same value.

    Raises:
        ValueError: A planning code is not in the closed domain
            vocabulary. Raised at import, so a code nobody declared is a
            startup failure rather than a surprise on the wire.
    """
    declared = {code.value for code in DomainErrorCode}
    undeclared = sorted(code.value for code in PlanRefusalCode if code.value not in declared)
    if undeclared:
        raise ValueError(f"planning refusal codes are not domain errors: {', '.join(undeclared)}")
    return {code: DomainErrorCode(code.value) for code in PlanRefusalCode}


#: Which declared domain code each planning refusal is reported as.
PLANNING_ERROR_CODES: Final[Mapping[PlanRefusalCode, DomainErrorCode]] = _compile_planning_codes()


class SubmitPlanParams(BaseModel):
    """The strict parameters of a plan submission.

    Attributes:
        proposal: The strict create document the planner emitted.
        actor: Who asked, as an immutable qualified principal key.
        idempotency_key: The client's name for this request.
    """

    model_config = ConfigDict(extra="forbid")

    proposal: PlanRevisionProposal
    actor: PrincipalKey
    idempotency_key: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]


class ApprovePlanParams(BaseModel):
    """The strict parameters of a plan approval.

    The digest is absent by construction. A request that could name the
    digest it approves could name one the stored body does not have, so
    the daemon computes it from the record instead.

    Attributes:
        key: The plan revision to approve.
        expected_revision: The compare-and-swap token the caller read.
        action_ref: The PendingAction the approval is sealed as.
        approved_by: The human principal approving.
        actor: Who asked.
        idempotency_key: The client's name for this request.
    """

    model_config = ConfigDict(extra="forbid")

    key: PlanRevisionKey
    expected_revision: StrictPositiveInt
    action_ref: AnyEntityUrn
    approved_by: OwnerPrincipal
    actor: PrincipalKey
    idempotency_key: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]

    @model_validator(mode="after")
    def _approver_can_seal_a_plan(self) -> Self:
        """Require a human principal and a PendingAction reference.

        Raises:
            ValueError: The pair cannot seal a plan approval. Refused at
                the boundary so the transaction is never opened for a
                request whose receipt could not be written.
        """
        validate_plan_approver(action_ref=self.action_ref, approved_by=self.approved_by)
        return self


class ApplyPlanParams(BaseModel):
    """The strict parameters of a plan apply.

    Attributes:
        key: The plan revision to apply.
        expected_revision: The compare-and-swap token the caller read.
        actor: Who asked.
        idempotency_key: The client's name for this request.
    """

    model_config = ConfigDict(extra="forbid")

    key: PlanRevisionKey
    expected_revision: StrictPositiveInt
    actor: PrincipalKey
    idempotency_key: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]


def _schema_refusal(
    error: ValidationError, *, params: dict[str, Any], operation: str
) -> DomainEnvelope:
    """Return the envelope of a request that does not parse.

    The pydantic detail is reduced to the offending field paths, because
    the full error text repeats the submitted values and a refusal that
    travels to terminals and daemon logs must not carry them.

    Args:
        error: What the parameter model refused.
        params: The raw request parameters, read only for the subject.
        operation: The verb the client asked for.

    Returns:
        An ``error`` envelope naming the fields to correct.
    """
    fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
    named = params.get("key")
    subject = named[:ENTITY_REF_WIDTH] if isinstance(named, str) and named else UNNAMED_SUBJECT
    return DomainEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        status=DomainStatus.ERROR,
        operation=operation,
        errors=(
            DomainError(
                code=DomainErrorCode.SCHEMA_VALIDATION_FAILED,
                message=f"the request is not a valid {operation}; check {', '.join(fields)}",
                entity_ref=subject,
                remediation="Correct the named parameters and retry.",
            ),
        ),
    )


def refused_plan_envelope(refusal: PlanRefusal, *, operation: str, subject: str) -> DomainEnvelope:
    """Return the machine response of one refused plan operation.

    Args:
        refusal: Why the operation refused, with nothing written.
        operation: The verb the client asked for.
        subject: The plan revision the refusal is about.

    Returns:
        An ``error`` envelope whose one row carries a declared code. Both
        revisions read the same, because a refused operation moved nothing.
    """
    return DomainEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        status=DomainStatus.ERROR,
        operation=operation,
        revision_before=refusal.revision,
        revision_after=refusal.revision,
        errors=(
            DomainError(
                code=PLANNING_ERROR_CODES[refusal.code],
                message=refusal.detail,
                entity_ref=subject,
                guard=refusal.guard,
                remediation=refusal.remediation,
            ),
        ),
    )


async def _answer(
    ctx: MethodContext,
    *,
    operation: str,
    subject: str,
    run: Callable[[], CommittedTransaction | PlanRefusal],
) -> dict[str, Any]:
    """Run one plan transaction off the loop and wrap whatever it answers.

    Args:
        ctx: Server context, which owns the subscription bus.
        operation: The verb the client asked for.
        subject: The plan revision the answer is about.
        run: The already-bound transaction call.

    Returns:
        The machine envelope, as a JSON-mode mapping. A commit whose
        post-commit publish did not reach the projection still answers
        ``ok`` with its receipt and carries the ``projection_degraded``
        warning. A retry answered from the receipt store publishes
        nothing, because the original commit already did.
    """
    answered = await asyncio.to_thread(run)
    if isinstance(answered, PlanRefusal):
        logger.info(f"_answer refused operation={operation} guard={answered.guard}")
        return refused_plan_envelope(answered, operation=operation, subject=subject).model_dump(
            mode="json"
        )
    warnings: tuple[str, ...] = ()
    # A replayed answer carries no envelope, so a retry never republishes
    # the event the original commit already fanned out.
    if answered.envelope is not None and not publish_projection(ctx.bus, answered.envelope):
        warnings = (PROJECTION_DEGRADED,)
    return accepted_envelope(answered.receipt, operation=operation, warnings=warnings).model_dump(
        mode="json"
    )


def _request(params: dict[str, Any]) -> dict[str, Any]:
    """Return the request parameters, less the routing key the fence consumed."""
    return {key: value for key, value in params.items() if key != REPO_ROOT_PARAM}


def _context(ctx: MethodContext, authority: RootAuthority) -> Epoch2RootContext:
    """Return the native context of the tree the fence cleared."""
    return ctx.native_root_context(authority.root)


@native_mutator(PLAN_SUBMIT_METHOD)
async def _submit_plan_revision(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Record one planner proposal as a VALIDATED plan revision."""
    try:
        request = SubmitPlanParams.model_validate(_request(params))
    except ValidationError as error:
        return _schema_refusal(error, params=params, operation=PLAN_SUBMIT_METHOD).model_dump(
            mode="json"
        )
    context = _context(ctx, authority)
    now = datetime.now(UTC)
    return await _answer(
        ctx,
        operation=PLAN_SUBMIT_METHOD,
        subject=request.proposal.key,
        run=lambda: submit_plan_revision(
            context,
            proposal=request.proposal,
            actor=request.actor,
            idempotency_key=request.idempotency_key,
            at=now,
        ),
    )


@native_mutator(PLAN_APPROVE_METHOD)
async def _approve_plan_revision(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Seal one human principal's approval onto a VALIDATED plan revision."""
    try:
        request = ApprovePlanParams.model_validate(_request(params))
    except ValidationError as error:
        return _schema_refusal(error, params=params, operation=PLAN_APPROVE_METHOD).model_dump(
            mode="json"
        )
    context = _context(ctx, authority)
    now = datetime.now(UTC)
    return await _answer(
        ctx,
        operation=PLAN_APPROVE_METHOD,
        subject=request.key,
        run=lambda: approve_plan_revision(
            context,
            key=request.key,
            expected_revision=request.expected_revision,
            action_ref=request.action_ref,
            approved_by=request.approved_by,
            actor=request.actor,
            idempotency_key=request.idempotency_key,
            at=now,
        ),
    )


@native_mutator(PLAN_APPLY_METHOD)
async def _apply_plan_revision(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Materialise one APPROVED plan revision into its Milestone."""
    try:
        request = ApplyPlanParams.model_validate(_request(params))
    except ValidationError as error:
        return _schema_refusal(error, params=params, operation=PLAN_APPLY_METHOD).model_dump(
            mode="json"
        )
    context = _context(ctx, authority)
    now = datetime.now(UTC)
    return await _answer(
        ctx,
        operation=PLAN_APPLY_METHOD,
        subject=request.key,
        run=lambda: apply_plan_revision(
            context,
            key=request.key,
            expected_revision=request.expected_revision,
            actor=request.actor,
            idempotency_key=request.idempotency_key,
            at=now,
        ),
    )


__all__ = [
    "PLANNING_ERROR_CODES",
    "PLANNING_METHODS",
    "PLAN_APPLY_METHOD",
    "PLAN_APPROVE_METHOD",
    "PLAN_SUBMIT_METHOD",
    "ApplyPlanParams",
    "ApprovePlanParams",
    "SubmitPlanParams",
    "refused_plan_envelope",
]
