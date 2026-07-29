"""Typed acknowledgement of an immutable legacy iter-audit anomaly."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

LegacyAuditIssue = Literal[
    "required",
    "not_found",
    "scope_mismatch",
    "kind_invalid",
    "not_complete",
    "verdict_rejected",
    "future_timestamp",
    "evidence_missing",
]


class LegacyAuditDisposition(BaseModel):
    """Historical annotation; never upgrades the referenced audit to a pass."""

    model_config = ConfigDict(extra="forbid")

    iter_id: str = Field(min_length=1)
    audit_id: str | None = None
    observed_issue: LegacyAuditIssue
    disposition: Literal["acknowledged_legacy_unverified"] = "acknowledged_legacy_unverified"
    source_state_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_audit_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")] | None = None
    source_refs: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=512)
    acknowledged_at: datetime
    operator_session: str | None = None


__all__ = ["LegacyAuditDisposition", "LegacyAuditIssue"]
