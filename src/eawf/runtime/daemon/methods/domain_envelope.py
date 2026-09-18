"""The strict machine envelope every native domain response is wrapped in.

A machine caller must be able to branch on one answer shape whether the
mutation committed or was refused, so both come back as the same
:class:`DomainEnvelope`: the status says which, the result carries the
receipt of a commit, and the error rows carry the refusals. A refusal is
not a transport failure -- the request was well formed and the daemon
answered it -- so it does not become a JSON-RPC error either.

Error codes are a closed vocabulary. :class:`DomainErrorCode` is the one
list, and every code the transaction can refuse with is checked against
it at import, so a refusal cannot reach a client carrying a code nobody
declared. The registry's own denial codes are finer than that list --
they name the guard that failed -- so they travel in the row's ``guard``
field rather than being widened into a second spelling of the wire code.

This module also registers the one native transition RPC. It sits behind
the epoch-2 fence, so it cannot be reached on a tree that has not been
shown to hold native authority, and it runs the transaction off the event
loop because the transaction blocks on file locks.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import StrictPositiveInt
from eawf.runtime.daemon.epoch2_recovery import PROJECTION_DEGRADED, publish_projection
from eawf.runtime.daemon.epoch2_transaction import (
    MutationReceipt,
    TransactionRefusalCode,
    TransactionRefusedError,
    TransitionRequest,
    run_transaction,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_mutator

logger = logging.getLogger(__name__)


#: The dotted JSON-RPC name of the one native transition verb. The
#: per-entity verbs are spelled ``domain.<entity>.<verb>`` and are added
#: beside it; this one is the entity-agnostic path they all commit
#: through.
DOMAIN_TRANSITION_METHOD: Final = "domain.transition.apply"

#: Version of the envelope shape itself, bumped when a field is added or
#: removed rather than when a code is.
ENVELOPE_SCHEMA_VERSION: Final = "1"

#: What an error row names as its subject when the request named none.
UNNAMED_SUBJECT: Final = "unknown"

#: How wide an error row's subject may be. A URN is far shorter; the bound
#: exists so an oversized parameter cannot make the refusal unbuildable.
ENTITY_REF_WIDTH: Final = 400


class DomainStatus(StrEnum):
    """Whether the daemon committed the mutation or refused it."""

    OK = "ok"
    ERROR = "error"


class DomainErrorCode(StrEnum):
    """The closed vocabulary of stable native-domain refusal codes.

    A consumer groups and routes on this value, so the values are part of
    the public contract and are never re-spelled to read better.
    """

    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    WORKSPACE_NOT_REGISTERED = "workspace_not_registered"
    WORKSPACE_AMBIGUOUS = "workspace_ambiguous"
    PROJECT_NOT_MEMBER = "project_not_member"
    IDENTITY_NOT_FOUND = "identity_not_found"
    IDENTITY_KIND_MISMATCH = "identity_kind_mismatch"
    LEGACY_IDENTITY_READ_ONLY = "legacy_identity_read_only"
    REVISION_CONFLICT = "revision_conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    ILLEGAL_TRANSITION = "illegal_transition"
    TRANSITION_GUARD_FAILED = "transition_guard_failed"
    CHILD_SCOPE_ACTIVE = "child_scope_active"
    PROOF_STALE = "proof_stale"
    MERGE_TRUTH_AMBIGUOUS = "merge_truth_ambiguous"
    PROTECTED_APPROVAL_REQUIRED = "protected_approval_required"
    MIGRATION_NOT_QUIESCENT = "migration_not_quiescent"
    MIGRATION_SOURCE_CHANGED = "migration_source_changed"
    MIGRATION_COUNT_MISMATCH = "migration_count_mismatch"
    MIGRATION_REFERENCE_UNRESOLVED = "migration_reference_unresolved"
    MIGRATION_FABRICATION_DETECTED = "migration_fabrication_detected"
    MIGRATION_IDEMPOTENCE_FAILED = "migration_idempotence_failed"
    LEGACY_OPERATION_REMOVED = "legacy_operation_removed"
    ROLLBACK_BOUNDARY_CROSSED = "rollback_boundary_crossed"
    NATIVE_AUTHORITY_REQUIRED = "native_authority_required"


def _compile_refusal_codes() -> Mapping[TransactionRefusalCode, DomainErrorCode]:
    """Return the transaction's refusal codes as declared domain codes.

    Returns:
        Every refusal code mapped to the domain code of the same value.

    Raises:
        ValueError: A refusal code is not in the closed domain
            vocabulary, which would let a refusal reach a client carrying
            a code nobody declared. Raised at import so the mismatch is a
            startup failure rather than a surprise on the wire.
    """
    declared = {code.value for code in DomainErrorCode}
    undeclared = sorted(code.value for code in TransactionRefusalCode if code.value not in declared)
    if undeclared:
        raise ValueError(f"refusal codes are not declared domain errors: {', '.join(undeclared)}")
    return {code: DomainErrorCode(code.value) for code in TransactionRefusalCode}


#: Which declared domain code each transaction refusal is reported as.
REFUSAL_ERROR_CODES: Final[Mapping[TransactionRefusalCode, DomainErrorCode]] = (
    _compile_refusal_codes()
)


class DomainError(BaseModel):
    """One refusal row of a machine response.

    Attributes:
        code: The stable code a client branches on.
        message: The operator-facing explanation. It never carries a
            stack trace or a local path.
        entity_ref: The record the refusal is about.
        guard: The finer denial or guard name, when a predicate was
            reached; ``None`` for a structural refusal.
        remediation: One sentence saying what to do about it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: DomainErrorCode
    message: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=1000)]
    entity_ref: Annotated[
        str, StringConstraints(strict=True, min_length=1, max_length=ENTITY_REF_WIDTH)
    ]
    guard: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=120)] | None = (
        None
    )
    remediation: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]


class DomainEnvelope(BaseModel):
    """The one answer shape of every native domain operation.

    Attributes:
        schema_version: Version of the envelope shape.
        status: Whether the mutation committed or was refused.
        operation: The operation that was asked for.
        revision_before: The subject's revision before the operation, or
            ``None`` when the refusal happened before any record was read.
        revision_after: The subject's revision after it. On a refusal it
            equals ``revision_before``, because nothing moved.
        result: The receipt of a committed mutation; ``None`` on refusal.
        warnings: Non-fatal notes about an answer that still stands.
        errors: The refusal rows; empty exactly when the status is ok.
        links: Named references a client may follow.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=16)]
    status: DomainStatus
    operation: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=120)]
    revision_before: StrictPositiveInt | None = None
    revision_after: StrictPositiveInt | None = None
    result: dict[str, Any] | None = None
    warnings: tuple[str, ...] = ()
    errors: tuple[DomainError, ...] = ()
    links: dict[str, str] = Field(default_factory=dict)


def accepted_envelope(
    receipt: MutationReceipt, *, operation: str, warnings: tuple[str, ...] = ()
) -> DomainEnvelope:
    """Return the machine response of one committed mutation.

    Args:
        receipt: What the transaction committed.
        operation: The operation the client asked for.
        warnings: Non-fatal notes about an answer that still stands, such
            as a post-commit publish that did not reach the projection.

    Returns:
        An ``ok`` envelope carrying the receipt as its result.
    """
    return DomainEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        status=DomainStatus.OK,
        operation=operation,
        revision_before=receipt.revision_before,
        revision_after=receipt.revision_after,
        result=receipt.model_dump(mode="json"),
        warnings=warnings,
    )


def refused_envelope(refusal: TransactionRefusedError, *, operation: str) -> DomainEnvelope:
    """Return the machine response of one refused mutation.

    Args:
        refusal: Why the transaction refused, with nothing written.
        operation: The operation the client asked for.

    Returns:
        An ``error`` envelope whose one row carries a declared code. Both
        revisions read the same, because a refused mutation moved nothing.
    """
    return DomainEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        status=DomainStatus.ERROR,
        operation=operation,
        revision_before=refusal.revision,
        revision_after=refusal.revision,
        errors=(
            DomainError(
                code=REFUSAL_ERROR_CODES[refusal.code],
                message=refusal.detail,
                entity_ref=refusal.entity_ref,
                guard=refusal.guard,
                remediation=refusal.remediation,
            ),
        ),
    )


def _schema_refusal(error: ValidationError, *, params: dict[str, Any]) -> DomainEnvelope:
    """Return the envelope of a request that does not parse.

    The pydantic detail is reduced to the offending field paths: the full
    error text repeats the submitted values, and a refusal that travels to
    terminals and daemon logs must not carry them. The subject is whatever
    the request called a URN, bounded to the row's own width.
    """
    fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
    named = params.get("urn")
    subject = named[:ENTITY_REF_WIDTH] if isinstance(named, str) and named else UNNAMED_SUBJECT
    return DomainEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        status=DomainStatus.ERROR,
        operation=DOMAIN_TRANSITION_METHOD,
        errors=(
            DomainError(
                code=DomainErrorCode.SCHEMA_VALIDATION_FAILED,
                message=f"the request is not a valid transition; check {', '.join(fields)}",
                entity_ref=subject,
                remediation="Correct the named parameters and retry.",
            ),
        ),
    )


@native_mutator(DOMAIN_TRANSITION_METHOD)
async def _apply_domain_transition(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Commit one native transition on the addressed epoch-2 tree.

    Args:
        ctx: Server context, which owns the per-root native contexts and
            the subscription bus.
        params: The request parameters, less the routing key the fence
            already consumed.
        authority: The epoch-2 answer the fence resolved for the tree.

    Returns:
        The machine envelope, as a JSON-mode mapping. A commit whose
        post-commit publish did not reach the projection still answers
        ``ok`` with its receipt, carrying the ``projection_degraded``
        warning: the mutation is durable and only the fan-out was lost.
        A retry the transaction answered from its receipt store publishes
        nothing, because the original commit already did.
    """
    try:
        request = TransitionRequest.model_validate(
            {key: value for key, value in params.items() if key != REPO_ROOT_PARAM}
        )
    except ValidationError as error:
        return _schema_refusal(error, params=params).model_dump(mode="json")
    context = ctx.native_root_context(authority.root)
    try:
        committed = await asyncio.to_thread(
            run_transaction,
            context=context,
            request=request,
            now=datetime.now(UTC),
        )
    except TransactionRefusedError as refusal:
        logger.info(f"_apply_domain_transition refused code={refusal.code.value}")
        return refused_envelope(refusal, operation=DOMAIN_TRANSITION_METHOD).model_dump(mode="json")
    warnings: tuple[str, ...] = ()
    if committed.envelope is not None and not publish_projection(ctx.bus, committed.envelope):
        warnings = (PROJECTION_DEGRADED,)
    return accepted_envelope(
        committed.receipt, operation=DOMAIN_TRANSITION_METHOD, warnings=warnings
    ).model_dump(mode="json")


__all__ = [
    "DOMAIN_TRANSITION_METHOD",
    "ENTITY_REF_WIDTH",
    "ENVELOPE_SCHEMA_VERSION",
    "REFUSAL_ERROR_CODES",
    "UNNAMED_SUBJECT",
    "DomainEnvelope",
    "DomainError",
    "DomainErrorCode",
    "DomainStatus",
    "accepted_envelope",
    "refused_envelope",
]
