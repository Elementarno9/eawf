"""AuditPayload — payload model for StoreKind.AUDIT records."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from eawf.kernel.state.enums import AuditKind, AuditVerdict


class CheckResult(BaseModel):
    """A single check result within an audit."""

    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    details: str | None = None


class AuditPayload(BaseModel):
    """Payload for an audit store record."""

    model_config = ConfigDict(extra="forbid")

    audit_kind: AuditKind
    verdict: AuditVerdict | None = None
    check_results: list[CheckResult]
    report_artifact_id: str | None = None
