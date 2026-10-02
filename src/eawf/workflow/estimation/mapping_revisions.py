"""Stored effort-mapping revisions, and the one in force.

A re-fit that applies is stored as a line on the estimate ledger, beside the
estimates that cite it, and a re-fit waiting on the operator is stored there
as a proposal. Nothing is ever edited: the revision in force is the highest
applied revision the ledger holds, and the shipped proposal default when it
holds none.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from eawf.kernel.runtime.compiled import canonical_digest
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.ledger import LedgerRecord
from eawf.workflow.estimation.calibration import RefitOutcome
from eawf.workflow.estimation.mapping import CURRENT_EFFORT_MAPPING, EffortMapping

#: The payload discriminator of an applied revision.
APPLIED_KIND: Final = "effort_mapping"

#: The payload discriminator of a revision waiting on an operator decision.
PROPOSED_KIND: Final = "effort_mapping_proposal"

_Ref = Annotated[str, Field(min_length=1, max_length=200)]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AppliedMapping(_Frozen):
    """One mapping revision in force from the moment it was stored.

    Attributes:
        payload_kind: The payload discriminator.
        mapping: The revision.
        digest: Its digest, stored so a later read can detect an edit.
        request_ref: The re-fit request that stored it.
        actor: Who asked for the re-fit.
        recorded_at: When it was stored.
        notice: The notice it was announced with.
        decision_ref: The operator decision that adopted it, for a revision
            past the threshold; ``None`` for one applied without asking.
    """

    payload_kind: Literal["effort_mapping"] = APPLIED_KIND
    mapping: EffortMapping
    digest: _Ref
    request_ref: _Ref
    actor: _Ref
    recorded_at: UtcDatetime
    notice: _Ref
    decision_ref: _Ref | None = None


class ProposedMapping(_Frozen):
    """One fitted revision the operator is asked to adopt or refuse.

    Attributes:
        payload_kind: The payload discriminator.
        mapping: The proposed revision.
        relative_change: How far it moves the effort constant.
        action_key: The idempotency key its decision is filed under.
        request_ref: The re-fit request that proposed it.
        actor: Who asked for the re-fit.
        recorded_at: When it was proposed.
    """

    payload_kind: Literal["effort_mapping_proposal"] = PROPOSED_KIND
    mapping: EffortMapping
    relative_change: Annotated[float, Field(ge=0.0)]
    action_key: _Ref
    request_ref: _Ref
    actor: _Ref
    recorded_at: UtcDatetime


class RefitResult(StrEnum):
    """What one re-fit request did."""

    NOT_DUE = "not_due"
    APPLIED = "applied"
    DECISION_OPENED = "decision_opened"
    AWAITING_DECISION = "awaiting_decision"
    DECLINED = "declined"


class RefitAnswer(BaseModel):
    """The answer to one re-fit request.

    Attributes:
        result: What the request did.
        revision: The mapping revision in force afterwards.
        digest: That revision's digest.
        outcome: The re-fit computed, when one ran.
        notice: The notice an applied revision is announced with.
        action_ref: The operator decision filed or waited on.
        decision: The decision's own answer, bound host question included.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    result: RefitResult
    revision: int
    digest: str
    outcome: RefitOutcome | None = None
    notice: str | None = None
    action_ref: str | None = None
    decision: dict[str, Any] | None = None


def applied_mappings(records: Sequence[LedgerRecord]) -> tuple[AppliedMapping, ...]:
    """Return every applied revision on the estimate ledger, in the order stored."""
    return tuple(
        AppliedMapping.model_validate(item.payload)
        for item in records
        if item.payload.get("payload_kind") == APPLIED_KIND
    )


def proposed_mappings(records: Sequence[LedgerRecord]) -> tuple[ProposedMapping, ...]:
    """Return every proposed revision on the estimate ledger, in the order stored."""
    return tuple(
        ProposedMapping.model_validate(item.payload)
        for item in records
        if item.payload.get("payload_kind") == PROPOSED_KIND
    )


def mapping_in_force(records: Sequence[LedgerRecord]) -> EffortMapping:
    """Return the highest applied revision, or the shipped proposal default.

    Args:
        records: Every line of the estimate ledger.

    Returns:
        The mapping new estimates cite.
    """
    applied = applied_mappings(records)
    if not applied:
        return CURRENT_EFFORT_MAPPING
    return max((row.mapping for row in applied), key=lambda mapping: mapping.revision)


def proposal_key(current: EffortMapping, proposed: EffortMapping) -> str:
    """Return the idempotency key a proposal's decision is filed under.

    The key is drawn from what the fit decided -- the revision it starts
    from, the constant it fitted and the sample it fitted on -- and not from
    the date, so the same fit asked again finds the question already filed.
    """
    assert proposed.fit is not None, "a proposal is always a fitted revision"
    digest = canonical_digest(
        {
            "from": current.digest,
            "effort_eu": proposed.effort_eu,
            "sample": list(proposed.fit.input_sample),
        }
    )
    return f"effort-refit-r{proposed.revision}-{digest.removeprefix('sha256:')[:16]}"


def revision_record_key(revision: StrictInt, *, proposed: bool) -> str:
    """Return the ledger key a stored revision or proposal is filed under."""
    return f"MAP-{revision:04d}{'-PROPOSED' if proposed else ''}"


__all__ = [
    "APPLIED_KIND",
    "PROPOSED_KIND",
    "AppliedMapping",
    "ProposedMapping",
    "RefitAnswer",
    "RefitResult",
    "applied_mappings",
    "mapping_in_force",
    "proposal_key",
    "proposed_mappings",
    "revision_record_key",
]
