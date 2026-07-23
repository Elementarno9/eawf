"""Shared close-audit acceptance checks for lifecycle transitions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from eawf.kernel.state.enums import AuditKind, AuditStatus, AuditVerdict
from eawf.kernel.state.models import Audit, State

AUDIT_MINOR_BACKLOG_TRIAGE: Final[str] = "audit_minor_backlog_triage"

_ALLOWED_VERDICTS: Final[frozenset[AuditVerdict]] = frozenset(
    {AuditVerdict.PASS, AuditVerdict.MINOR}
)
_PASS_STATUSES: Final[frozenset[str]] = frozenset({"pass", "passed", "ok", "success"})
_RESULT_STATUSES: Final[frozenset[str]] = frozenset({"fail", "failed", *_PASS_STATUSES})
_LEGACY_STUB_CHECK_IDS: Final[frozenset[str]] = frozenset({"stub"})


class AuditAcceptanceIssue(StrEnum):
    """Reason a close audit fails its configured acceptance policy."""

    REQUIRED = "required"
    NOT_FOUND = "not_found"
    SCOPE_MISMATCH = "scope_mismatch"
    KIND_INVALID = "kind_invalid"
    NOT_COMPLETE = "not_complete"
    VERDICT_REJECTED = "verdict_rejected"
    FUTURE_TIMESTAMP = "future_timestamp"
    EVIDENCE_MISSING = "evidence_missing"


@dataclass(frozen=True)
class AuditAcceptance:
    """Read-only assessment of one audit proposed for lifecycle close."""

    audit: Audit | None
    issue: AuditAcceptanceIssue | None
    warnings: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        """Return whether the audit clears every requested check."""
        return self.audit is not None and self.issue is None


def _audit_row_get(row: object, key: str) -> object:
    """Read one audit check field from dict-like or model-like rows."""
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def _audit_check_id(row: object) -> str | None:
    """Return the first supported identifier from an audit check row."""
    raw_id = (
        _audit_row_get(row, "id")
        or _audit_row_get(row, "name")
        or _audit_row_get(row, "check_id")
        or _audit_row_get(row, "gate_id")
    )
    return raw_id if isinstance(raw_id, str) and raw_id else None


def _audit_row_has_result(row: object, *, require_passing: bool) -> bool:
    """Return whether one non-stub check carries an accepted explicit result."""
    raw_id = _audit_check_id(row)
    if raw_id is None:
        return False
    raw_details = _audit_row_get(row, "details")
    if raw_id in _LEGACY_STUB_CHECK_IDS and raw_details == "Phase 2 stub":
        return False
    passed = _audit_row_get(row, "passed")
    if isinstance(passed, bool):
        return passed if require_passing else True
    raw_status = _audit_row_get(row, "status") or _audit_row_get(row, "conclusion")
    if not isinstance(raw_status, str):
        return False
    accepted_statuses = _PASS_STATUSES if require_passing else _RESULT_STATUSES
    return raw_status.lower() in accepted_statuses


def audit_has_real_close_evidence(
    state: State,
    *,
    audit: Audit,
    require_passing_check: bool,
) -> bool:
    """Return whether *audit* has a real artifact or non-stub check result."""
    report_artifact_id = audit.report_artifact_id
    if report_artifact_id:
        return report_artifact_id in state.artifacts
    return any(
        _audit_row_has_result(row, require_passing=require_passing_check)
        for row in audit.check_results
    )


def assess_close_audit(
    state: State,
    *,
    audit_id: str | None,
    allowed_scope_ids: frozenset[str],
    required_kind: AuditKind,
    check_order: tuple[AuditAcceptanceIssue, ...],
    require_passing_check: bool,
    now: datetime | None = None,
) -> AuditAcceptance:
    """Assess one close audit without mutating state or inspecting Git."""
    if audit_id is None:
        return AuditAcceptance(audit=None, issue=AuditAcceptanceIssue.REQUIRED)
    audit = (state.audits or {}).get(audit_id)
    if audit is None or audit.id != audit_id:
        return AuditAcceptance(audit=None, issue=AuditAcceptanceIssue.NOT_FOUND)

    reference_time = now or datetime.now(UTC)
    for check in check_order:
        if check is AuditAcceptanceIssue.SCOPE_MISMATCH and audit.scope_id not in allowed_scope_ids:
            return AuditAcceptance(audit=audit, issue=check)
        if check is AuditAcceptanceIssue.KIND_INVALID and audit.kind is not required_kind:
            return AuditAcceptance(audit=audit, issue=check)
        if check is AuditAcceptanceIssue.NOT_COMPLETE and audit.status is not AuditStatus.COMPLETE:
            return AuditAcceptance(audit=audit, issue=check)
        if (
            check is AuditAcceptanceIssue.VERDICT_REJECTED
            and audit.verdict not in _ALLOWED_VERDICTS
        ):
            return AuditAcceptance(audit=audit, issue=check)
        if check is AuditAcceptanceIssue.FUTURE_TIMESTAMP and audit.created_at > reference_time:
            return AuditAcceptance(audit=audit, issue=check)
        if check is AuditAcceptanceIssue.EVIDENCE_MISSING and not audit_has_real_close_evidence(
            state,
            audit=audit,
            require_passing_check=require_passing_check,
        ):
            return AuditAcceptance(audit=audit, issue=check)

    warnings = (AUDIT_MINOR_BACKLOG_TRIAGE,) if audit.verdict is AuditVerdict.MINOR else ()
    return AuditAcceptance(audit=audit, issue=None, warnings=warnings)


__all__ = [
    "AUDIT_MINOR_BACKLOG_TRIAGE",
    "AuditAcceptance",
    "AuditAcceptanceIssue",
    "assess_close_audit",
    "audit_has_real_close_evidence",
]
