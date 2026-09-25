"""What an admitted semantic call is actually answered with.

The gateway decides whether a call may be answered; this module answers
it. The two are separate files because they fail differently: a missing
guard admits a call nobody checked, while a missing handler admits a call
nobody can serve, and the second must never be papered over with a
plausible-looking output. :data:`BROKERED_TOOLS` is therefore derived
from the handler table rather than declared beside it -- a tool is
brokered exactly when a function answers it, so the set cannot grow past
what is implemented.

Two tools are answered here today.

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

No handler opens a session of its own. The gateway is already inside one
with the Run's locks and the document lock held, and a nested session
would block on the lock the caller is holding. What a handler needs from
the tree it reads through the session it was handed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final, Literal, Self

from pydantic import TypeAdapter, ValidationError, model_validator

from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.compiled import JsonPointer, canonical_digest
from eawf.kernel.runtime.provider import ArtifactUrn, Digest, RuntimeRecord
from eawf.kernel.runtime.semantic import (
    BudgetStatusOutput,
    BudgetUsage,
    FindingCode,
    SemanticCall,
    SemanticToolId,
    SemanticToolOutput,
    SubmitPlanInput,
    SubmitPlanOutput,
    ValidationFinding,
)
from eawf.kernel.state.epoch2.run import Run
from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import RootSession
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.workflow.planning.apply import PlanRevisionProposal, validate_plan_proposal
from eawf.workflow.planning.revision import PlanRefusal, PlanRefusalCode

logger = logging.getLogger(__name__)


#: The discriminator separating a plan proposal from the other payload
#: kinds the artifact ledger may come to hold.
PLAN_PROPOSAL_PAYLOAD_KIND: Final = "plan_proposal"

#: How long a finding message may be before it is trimmed to fit the
#: bounded-text field the output carries.
_MESSAGE_LIMIT: Final = 500

#: Validates one derived finding code against the closed grammar the
#: output field pins, so a refusal code that cannot be reported is a
#: startup failure rather than a validation error under load.
_FINDING_CODES: Final = TypeAdapter[Any](FindingCode)

#: Validates a derived pointer against the grammar the finding field pins.
_POINTERS: Final = TypeAdapter[Any](JsonPointer)


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
    """

    session: RootSession
    call: SemanticCall
    capsule: AuthorityCapsule
    run: Run
    calls_so_far: int
    now: datetime


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


#: Which function answers each brokered tool. A tool reaches a handler or
#: it reaches none: the gateway refuses an admitted call whose tool is
#: absent here rather than inventing an outcome for it.
SEMANTIC_HANDLERS: Final[Mapping[SemanticToolId, Callable[[HandlerInputs], SemanticToolOutput]]] = (
    MappingProxyType(
        {
            SemanticToolId.BUDGET_STATUS: _budget_status,
            SemanticToolId.SUBMIT_PLAN: _submit_plan,
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
    "PLAN_PROPOSAL_PAYLOAD_KIND",
    "REPORTABLE_PLAN_CODES",
    "SEMANTIC_HANDLERS",
    "HandlerInputs",
    "HandlerTableError",
    "PlanProposalArtifact",
]
