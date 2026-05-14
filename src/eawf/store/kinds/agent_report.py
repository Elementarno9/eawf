"""Typed payload primitives for agent report store records."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from eawf.state.enums import AgentReportVerdict, AgentSessionRole, Confidence
from eawf.state.types import UtcDatetime

_REPORT_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class _StrictModel(BaseModel):
    """Base report model with closed schemas."""

    model_config = ConfigDict(extra="forbid")


class AgentReportEvidenceRef(_StrictModel):
    """Pointer to evidence supporting a report claim."""

    kind: Literal["repo", "urn", "url", "commit"]
    ref: Annotated[str, Field(min_length=1)]
    note: Annotated[str, Field(max_length=240)] | None = None


class AgentReportFollowup(_StrictModel):
    """Actionable follow-up emitted by an agent report."""

    title: Annotated[str, Field(min_length=1, max_length=160)]
    owner_role: AgentSessionRole | None = None
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    detail: Annotated[str, Field(max_length=500)] | None = None


class AgentReportHeader(_StrictModel):
    """Common metadata for every role report attempt."""

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

    @field_validator("artifact_ids", "blob_refs")
    @classmethod
    def _dedupe_preserve_order(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))


class AgentReportBody(_StrictModel):
    """Common report body shared by role-specific models."""

    verdict: AgentReportVerdict
    confidence: Confidence
    summary: Annotated[str, Field(min_length=1, max_length=4000)]
    evidence_refs: list[AgentReportEvidenceRef] = Field(default_factory=list)
    followups: list[AgentReportFollowup] = Field(default_factory=list)


class AgentReportPayload(_StrictModel):
    """Store payload wrapper for a typed agent report."""

    header: AgentReportHeader
    body: AgentReportBody


def report_record_id(*, role: AgentSessionRole, base_id: str, attempt: int) -> str:
    """Return a stable record id for one role/base/attempt tuple.

    Raises:
        ValueError: When ``base_id`` is empty or ``attempt`` is less than one.
    """
    if not base_id.strip():
        raise ValueError("base_id must be non-empty")
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    role_token = role.value.replace("-", "_")
    base_token = _REPORT_ID_RE.sub("-", base_id.strip()).strip("-")
    return f"AR-{role_token}-{base_token}-{attempt:02d}"


__all__ = [
    "AgentReportBody",
    "AgentReportEvidenceRef",
    "AgentReportFollowup",
    "AgentReportHeader",
    "AgentReportPayload",
    "report_record_id",
]
