"""``/integrate`` skill body — one daemon-owned integration action.

The report says which candidates were considered, why each was included
or rejected, the generation the attempt produced, the conflicts it
recorded, and the receipts bound to it. A candidate is never chosen by
intuition, so a selection with no stated reason is not a valid report.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.workflow.skills.bodies.user_question import UserQuestion

#: The closed action branches of the ``/integrate`` grammar.
IntegrateAction = Literal["seal", "select", "apply", "retry", "show"]

#: The closed terminal outcomes of one ``/integrate`` invocation.
IntegrateOutcome = Literal[
    "shown",
    "sealed",
    "selected",
    "integrated",
    "conflicted",
    "stale",
    "blocked",
]


class CandidateDisposition(BaseModel):
    """Why one candidate was included in, or kept out of, the delivery.

    Attributes:
        candidate_ref: The candidate the row is about.
        included: Whether the selection policy took it.
        reason: The policy's stated reason, never an empty string.
    """

    model_config = ConfigDict(extra="forbid")

    candidate_ref: str
    included: bool
    reason: str = Field(min_length=1)


class IntegrateBody(BaseModel):
    """Body for ``/integrate`` — the integration skill report.

    Attributes:
        kind: The output-schema discriminator.
        action: The grammar branch this invocation selected.
        subject_ref: The Batch or candidate the action addressed.
        method: The JSON-RPC method the action called, or ``None`` when
            the invocation stopped before any call.
        candidates: Every candidate considered, with its disposition.
        generation_ids: Every integration generation the attempt selected.
        conflict_refs: The conflict frames the attempt recorded.
        verification_receipts: Receipts bound by ``--verify-after``.
        unresolved_request_fields: The request fields the addressed verb
            names that no surface this invocation reaches resolves. A
            non-empty list is why no call was made.
        refusal_code: The stable code a refusal carried, or ``None``.
        outcome: The terminal outcome.
        reason: One sentence an operator reads.
        user_question: Populated when the action stops for an operator
            choice; ``None`` otherwise.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["integration_skill_report"] = "integration_skill_report"
    action: IntegrateAction
    subject_ref: str
    method: str | None = None
    candidates: list[CandidateDisposition] = Field(default_factory=list)
    generation_ids: list[str] = Field(default_factory=list)
    conflict_refs: list[str] = Field(default_factory=list)
    verification_receipts: list[str] = Field(default_factory=list)
    unresolved_request_fields: list[str] = Field(default_factory=list)
    refusal_code: str | None = None
    outcome: IntegrateOutcome
    reason: str
    user_question: UserQuestion | None = None


__all__ = [
    "CandidateDisposition",
    "IntegrateAction",
    "IntegrateBody",
    "IntegrateOutcome",
]
