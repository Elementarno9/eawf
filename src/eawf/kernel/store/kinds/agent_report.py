"""Typed payload primitives for agent report store records."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eawf.kernel.spec.common import EvidenceKind
from eawf.kernel.state.enums import AgentReportVerdict, AgentSessionRole, Confidence, StoreKind
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.state.urn import build as build_urn

_REPORT_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_ROLE_TO_STORE_KIND: dict[AgentSessionRole, StoreKind] = {
    AgentSessionRole.RESEARCHER: StoreKind.RESEARCHER_REPORT,
    AgentSessionRole.PLANNER: StoreKind.PLANNER_REPORT,
    AgentSessionRole.EXECUTOR: StoreKind.EXECUTOR_REPORT,
    AgentSessionRole.AUDITOR: StoreKind.AUDITOR_REPORT,
    AgentSessionRole.REVIEWER: StoreKind.REVIEWER_REPORT,
    AgentSessionRole.POLISHER: StoreKind.POLISHER_REPORT,
    AgentSessionRole.OPERATOR: StoreKind.OPERATOR_REPORT,
    AgentSessionRole.DOMAIN_SPECIALIST: StoreKind.DOMAIN_SPECIALIST_REPORT,
}
_STORE_KIND_TO_ROLE: dict[StoreKind, AgentSessionRole] = {
    store_kind: role for role, store_kind in _ROLE_TO_STORE_KIND.items()
}


class _StrictModel(BaseModel):
    """Base report model with closed schemas."""

    model_config = ConfigDict(extra="forbid")


class AgentReportEvidenceRef(_StrictModel):
    """Pointer to evidence supporting a report claim.

    The ``kind`` Literal is imported from :data:`eawf.kernel.spec.common.EvidenceKind`
    so the agent-report vocabulary equals the spec-layer vocabulary —
    one canonical kind set across spec and report layers.
    """

    kind: EvidenceKind
    ref: Annotated[str, Field(min_length=1)]
    note: Annotated[str, Field(max_length=240)] | None = None


class AgentReportFollowup(_StrictModel):
    """Actionable follow-up emitted by an agent report."""

    title: Annotated[str, Field(min_length=1, max_length=160)]
    owner_role: AgentSessionRole | None = None
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    detail: Annotated[str, Field(max_length=500)] | None = None


class AgentReportHeader(_StrictModel):
    """Common metadata for every role report attempt.

    The ``agent_principal_id`` field is a v0.3-v0.5 placeholder mirroring
    :attr:`eawf.kernel.store.kinds.event.EventPayload.actor_principal_id`
    and :attr:`eawf.kernel.state.models.AgentSession.agent_principal_id`:
    headers MAY carry the :class:`~eawf.kernel.state.models.Principal` id
    of the agent that produced the report when known, but the load-
    bearing identity for backward compatibility remains
    :attr:`session_id` + :attr:`runtime`. Default ``None`` keeps the
    field replay-safe / additive — existing on-disk envelopes continue
    to validate without backfill. The canonical agent-report writer
    copies the value from the session when present.
    """

    report_id: Annotated[str, Field(min_length=1)]
    role: AgentSessionRole
    session_id: Annotated[str, Field(min_length=1)]
    scope_id: Annotated[str, Field(min_length=1)]
    base_id: Annotated[str, Field(min_length=1)]
    attempt: Annotated[int, Field(ge=1)]
    runtime: Annotated[str, Field(min_length=1)]
    generated_at: UtcDatetime
    summary: Annotated[str, Field(min_length=1, max_length=500)]
    artifact_ids: list[str] = Field(default_factory=list)
    blob_refs: list[str] = Field(default_factory=list)
    agent_principal_id: str | None = None

    @field_validator("artifact_ids", "blob_refs")
    @classmethod
    def _dedupe_preserve_order(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))


class AgentReportCommonBody(_StrictModel):
    """Fields every role report body must carry."""

    verdict: AgentReportVerdict
    confidence: Confidence
    summary: Annotated[str, Field(min_length=1, max_length=4000)]
    evidence_refs: list[AgentReportEvidenceRef] = Field(default_factory=list)
    followups: list[AgentReportFollowup] = Field(default_factory=list)


class PlannedWaveSummary(_StrictModel):
    """Planner summary for one proposed wave."""

    wave_id: Annotated[str, Field(min_length=1)]
    title: Annotated[str, Field(min_length=1, max_length=160)]
    depends_on: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)


class CriterionVerdict(_StrictModel):
    """Auditor verdict for one success criterion."""

    criterion: Annotated[str, Field(min_length=1, max_length=500)]
    passed: bool
    evidence_refs: list[AgentReportEvidenceRef] = Field(default_factory=list)


class ReviewFinding(_StrictModel):
    """Reviewer finding with severity and evidence."""

    severity: Literal["blocker", "must-fix", "should-fix", "nit"]
    message: Annotated[str, Field(min_length=1, max_length=500)]
    evidence_refs: list[AgentReportEvidenceRef] = Field(default_factory=list)


class PolishChange(_StrictModel):
    """Polisher change/defer row."""

    category: Literal["naming", "docstring", "logging", "error", "dead-code", "format"]
    summary: Annotated[str, Field(min_length=1, max_length=300)]
    files: list[str] = Field(default_factory=list)


class ResearcherReportBody(AgentReportCommonBody):
    """Report body emitted by a researcher."""

    role: Literal["researcher"] = "researcher"
    question: Annotated[str, Field(min_length=1, max_length=500)]
    findings: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    recommendation: Annotated[str, Field(min_length=1, max_length=1000)]


class PlannerReportBody(AgentReportCommonBody):
    """Report body emitted by a planner."""

    role: Literal["planner"] = "planner"
    objective: Annotated[str, Field(min_length=1, max_length=500)]
    waves: list[PlannedWaveSummary] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class ExecutorReportBody(AgentReportCommonBody):
    """Report body emitted by an executor."""

    role: Literal["executor"] = "executor"
    wave_id: Annotated[str, Field(min_length=1)]
    files_changed: list[str] = Field(default_factory=list)
    tests_run: list[str] = Field(default_factory=list)
    commit_sha: Annotated[str, Field(min_length=7)] | None = None
    outcome: Annotated[str, Field(min_length=1, max_length=1000)]


class AuditorReportBody(AgentReportCommonBody):
    """Report body emitted by an auditor."""

    role: Literal["auditor"] = "auditor"
    target_id: Annotated[str, Field(min_length=1)]
    criteria: list[CriterionVerdict] = Field(default_factory=list)
    refutations: list[str] = Field(default_factory=list)


class ReviewerReportBody(AgentReportCommonBody):
    """Report body emitted by a reviewer."""

    role: Literal["reviewer"] = "reviewer"
    target_id: Annotated[str, Field(min_length=1)]
    findings: list[ReviewFinding] = Field(default_factory=list)
    coverage_refs: list[AgentReportEvidenceRef] = Field(default_factory=list)


class PolisherReportBody(AgentReportCommonBody):
    """Report body emitted by a polisher."""

    role: Literal["polisher"] = "polisher"
    scope_id: Annotated[str, Field(min_length=1)]
    changes: list[PolishChange] = Field(default_factory=list)
    deferred_items: list[str] = Field(default_factory=list)


class OperatorReportBody(AgentReportCommonBody):
    """Report body emitted by an operator."""

    role: Literal["operator"] = "operator"
    phase_id: Annotated[str, Field(min_length=1)]
    completed_wave_ids: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


class DomainSpecialistReportBody(AgentReportCommonBody):
    """Report body emitted by a domain specialist."""

    role: Literal["domain-specialist"] = "domain-specialist"
    domain: Annotated[str, Field(min_length=1, max_length=120)]
    assessment: Annotated[str, Field(min_length=1, max_length=2000)]
    recommendations: list[str] = Field(default_factory=list)


type AgentReportBody = Annotated[
    ResearcherReportBody
    | PlannerReportBody
    | ExecutorReportBody
    | AuditorReportBody
    | ReviewerReportBody
    | PolisherReportBody
    | OperatorReportBody
    | DomainSpecialistReportBody,
    Field(discriminator="role"),
]


class AgentReportPayload(_StrictModel):
    """Store payload wrapper for a typed agent report."""

    header: AgentReportHeader
    body: AgentReportBody

    @model_validator(mode="after")
    def _body_role_matches_header(self) -> Self:
        if self.body.role != self.header.role.value:
            raise ValueError(
                f"body role {self.body.role!r} does not match header role "
                f"{self.header.role.value!r}"
            )
        if (
            self.header.role is AgentSessionRole.RESEARCHER
            and self.header.report_id
            == report_record_id(
                role=self.header.role,
                base_id=self.header.base_id,
                attempt=self.header.attempt,
            )
            and isinstance(self.body, ResearcherReportBody)
            and self.body.verdict is AgentReportVerdict.PASS
            and not self.body.evidence_refs
        ):
            raise ValueError("researcher pass requires non-empty evidence_refs")
        return self


def report_record_id(*, role: AgentSessionRole, base_id: str, attempt: int) -> str:
    """Return a stable record id for one role/base/attempt tuple.

    A caller that passes a ``base_id`` already carrying the role token (an
    operator-supplied ``"auditor-P30-..."``) is normalized so the role
    prefix appears exactly once: the returned id always matches
    ``^AR-<role_token>-(?!<role_token>-)``. Without this the mint doubled
    the prefix into ``AR-auditor-auditor-...``.

    Raises:
        ValueError: When ``base_id`` is empty or ``attempt`` is less than one.
    """
    if not base_id.strip():
        raise ValueError("base_id must be non-empty")
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    role_token = role.value.replace("-", "_")
    base_token = _REPORT_ID_RE.sub("-", base_id.strip()).strip("-")
    # Drop a redundant leading role token so a role-prefixed base_id does
    # not double the prefix in the minted id.
    role_prefix = f"{role_token}-"
    if base_token.startswith(role_prefix):
        base_token = base_token[len(role_prefix) :].strip("-")
    return f"AR-{role_token}-{base_token}-{attempt:02d}"


def store_kind_for_role(role: AgentSessionRole) -> StoreKind:
    """Return the role-specific store kind for *role*."""
    return _ROLE_TO_STORE_KIND[role]


def role_for_store_kind(store_kind: StoreKind) -> AgentSessionRole:
    """Return the agent role associated with a role-report store kind.

    Raises:
        ValueError: When *store_kind* is not an agent-report store kind.
    """
    try:
        return _STORE_KIND_TO_ROLE[store_kind]
    except KeyError as exc:
        raise ValueError(f"store kind is not an agent report kind: {store_kind.value!r}") from exc


def report_store_urn(*, scope_id: str, role: AgentSessionRole, report_id: str) -> str:
    """Return the canonical store URN for a role report."""
    store_kind = store_kind_for_role(role)
    return build_urn("store", owner=scope_id, id=f"{store_kind.value}/{report_id}")


_ROLE_BODY_CLASSES: dict[AgentSessionRole, type[AgentReportCommonBody]] = {
    AgentSessionRole.RESEARCHER: ResearcherReportBody,
    AgentSessionRole.PLANNER: PlannerReportBody,
    AgentSessionRole.EXECUTOR: ExecutorReportBody,
    AgentSessionRole.AUDITOR: AuditorReportBody,
    AgentSessionRole.REVIEWER: ReviewerReportBody,
    AgentSessionRole.POLISHER: PolisherReportBody,
    AgentSessionRole.OPERATOR: OperatorReportBody,
    AgentSessionRole.DOMAIN_SPECIALIST: DomainSpecialistReportBody,
}


def body_class_for_role(role: AgentSessionRole) -> type[AgentReportCommonBody]:
    """Return the typed report-body class for *role*.

    The dispatch runner uses this to construct the right-typed body for
    a non-executor session role on completion. Executor remains the rich
    path (commit_sha + wave_id + files_changed + tests_run + outcome); the
    other seven roles build a minimal completion body keyed by the role's
    required fields. Future waves under I02/I03 wire the full per-role
    validators (e.g. criteria for the auditor, coverage_refs for the
    reviewer); this seam keeps the kind routing typed today without
    pre-building those validators.

    Args:
        role: The agent session role whose body class to return.

    Returns:
        The :class:`AgentReportCommonBody` subclass mapped to *role*.

    Raises:
        KeyError: When *role* has no registered body class (cannot happen
            for a valid :class:`AgentSessionRole`).
    """
    try:
        return _ROLE_BODY_CLASSES[role]
    except KeyError as exc:
        raise KeyError(f"no report body class registered for role: {role.value!r}") from exc


__all__ = [
    "AgentReportBody",
    "AgentReportCommonBody",
    "AgentReportEvidenceRef",
    "AgentReportFollowup",
    "AgentReportHeader",
    "AgentReportPayload",
    "AuditorReportBody",
    "CriterionVerdict",
    "DomainSpecialistReportBody",
    "ExecutorReportBody",
    "OperatorReportBody",
    "PlannedWaveSummary",
    "PlannerReportBody",
    "PolishChange",
    "PolisherReportBody",
    "ResearcherReportBody",
    "ReviewFinding",
    "ReviewerReportBody",
    "body_class_for_role",
    "report_record_id",
    "report_store_urn",
    "role_for_store_kind",
    "store_kind_for_role",
]
