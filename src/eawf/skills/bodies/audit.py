"""``/audit`` skill body.

Per ``docs/architecture/envelope.md``:

    /audit body: { scope_id, kind: evaluation|ship-gate, checks_run:
                   [{check_id, command, status, output_blob}],
                   outcomes_measured: [{outcome_id, value, threshold,
                   verdict}], hypothesis_verdicts: [{hypothesis_id,
                   verdict, evidence_commit}], findings: [{severity,
                   location, summary, kind: blocker|fix-now|follow-up|
                   false-positive}], audit_artifact_urn }
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.skills.bodies.user_question import UserQuestion

# Frozen literals per §15.2.
AuditKind = Literal["evaluation", "ship-gate"]
FindingKind = Literal["blocker", "fix-now", "follow-up", "false-positive"]


class AuditCheckRun(BaseModel):
    """One executed check during an audit."""

    model_config = ConfigDict(extra="forbid")

    check_id: str
    command: str
    status: str
    output_blob: str | None = None


class AuditOutcome(BaseModel):
    """Outcome measurement compared against its threshold."""

    model_config = ConfigDict(extra="forbid")

    outcome_id: str
    value: float
    threshold: float
    verdict: str


class AuditHypothesisVerdict(BaseModel):
    """Per-hypothesis verdict reached during the audit."""

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str
    verdict: str
    evidence_commit: str | None = None


class AuditFinding(BaseModel):
    """One finding raised during the audit."""

    model_config = ConfigDict(extra="forbid")

    severity: str
    location: str
    summary: str
    kind: FindingKind


class AuditorDispatch(BaseModel):
    """Fresh-context auditor dispatch directive.

    The ``/audit`` skill does not spawn the auditor thread itself; it
    emits this directive so a model-invoked runtime spawns a
    fresh-context auditor subagent. The fresh-context mandate is the
    point: the auditor receives only the diff base and the criteria
    under review, never the parent conversation, so its verdict cannot
    be biased by the implementation context that produced the diff.
    """

    model_config = ConfigDict(extra="forbid")

    #: Frozen session policy — the auditor MUST run in a fresh context.
    session_policy: Literal["fresh"] = "fresh"
    #: Wave the auditor re-verifies.
    wave_id: str
    #: Git ref the auditor diffs ``HEAD`` against to read the change.
    diff_base: str
    #: The exact success criteria handed to the auditor (diff + these
    #: are the auditor's only inputs).
    criteria: list[str] = Field(default_factory=list)
    #: Human-readable instruction the runtime expands into the auditor
    #: prompt.
    instruction: str


class AuditBody(BaseModel):
    """Body for ``/audit``."""

    model_config = ConfigDict(extra="forbid")

    scope_id: str
    kind: AuditKind
    checks_run: list[AuditCheckRun] = Field(default_factory=list)
    outcomes_measured: list[AuditOutcome] = Field(default_factory=list)
    hypothesis_verdicts: list[AuditHypothesisVerdict] = Field(default_factory=list)
    findings: list[AuditFinding] = Field(default_factory=list)
    audit_artifact_urn: str | None = None
    auditor_dispatch: AuditorDispatch | None = None
    user_question: UserQuestion | None = None


__all__ = [
    "AuditBody",
    "AuditCheckRun",
    "AuditFinding",
    "AuditHypothesisVerdict",
    "AuditKind",
    "AuditOutcome",
    "AuditorDispatch",
    "FindingKind",
]
