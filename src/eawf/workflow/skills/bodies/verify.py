"""``/verify`` skill body — one Batch judged at one exact revision.

Aggregate verdicts are derived from rows, never asserted: the body
carries per-criterion rows and the head they were taken on, and the
terminal outcome follows from them. Absence of evidence reads as
``unverified``, which blocks, rather than as a pass.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.workflow.skills.bodies.user_question import UserQuestion

#: The closed mode branches of the ``/verify`` grammar.
VerifyMode = Literal["gates", "audit", "review", "security", "all"]

#: The closed terminal outcomes of one ``/verify`` invocation.
VerifyOutcome = Literal["passed", "failed", "unverified", "stale", "blocked"]

#: The per-criterion verdict a row carries.
CriterionVerdict = Literal["passed", "failed", "unverified"]


class CriterionRow(BaseModel):
    """One criterion judged against the bound head.

    Attributes:
        criterion_id: The criterion the row is about.
        verdict: What the falsification attempt concluded.
        falsifier: What was attempted against it. Empty on an
            ``unverified`` row is the honest shape: nothing was run.
        locus: A repo-relative locus for the evidence, or ``None``.
    """

    model_config = ConfigDict(extra="forbid")

    criterion_id: str
    verdict: CriterionVerdict
    falsifier: str = ""
    locus: str | None = None


class VerifyBody(BaseModel):
    """Body for ``/verify`` — the verification report.

    Attributes:
        kind: The output-schema discriminator.
        subject_ref: The Batch or revision this pass judged.
        mode: The grammar branch this invocation selected.
        method: The JSON-RPC method the pass called, or ``None`` when it
            stopped before any call.
        head_generation: The exact ordinal the pass was taken on, or
            ``None`` when no head was bound.
        stage: Where the Batch's verification cycle stands afterwards.
        blocking_criterion_ids: Criteria a required row leaves open.
        settled_criterion_ids: Criteria an independent row cleared.
        rows: The per-criterion verdicts the aggregate is derived from.
        merge_ready: Whether the Batch cleared on this head.
        approval_ref: The acceptance question the pass opened or found
            standing, or ``None`` when it asked none.
        bundle_digest: The digest of the acceptance bundle that question
            is bound to.
        acceptance_bundle: That bundle, exactly as filed, which is what an
            acceptance must later present.
        unresolved_request_fields: The request fields the addressed verb
            names that no surface this invocation reaches resolves. A
            non-empty list is why no call was made.
        refusal_code: The stable code a refusal carried, or ``None``.
        outcome: The terminal outcome.
        reason: One sentence an operator reads.
        user_question: Populated when the pass stops for an operator;
            ``None`` otherwise.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["verification_report"] = "verification_report"
    subject_ref: str
    mode: VerifyMode
    method: str | None = None
    head_generation: int | None = None
    stage: str | None = None
    blocking_criterion_ids: list[str] = Field(default_factory=list)
    settled_criterion_ids: list[str] = Field(default_factory=list)
    rows: list[CriterionRow] = Field(default_factory=list)
    merge_ready: bool = False
    approval_ref: str | None = None
    bundle_digest: str | None = None
    acceptance_bundle: dict[str, Any] | None = None
    unresolved_request_fields: list[str] = Field(default_factory=list)
    refusal_code: str | None = None
    outcome: VerifyOutcome
    reason: str
    user_question: UserQuestion | None = None


__all__ = [
    "CriterionRow",
    "CriterionVerdict",
    "VerifyBody",
    "VerifyMode",
    "VerifyOutcome",
]
