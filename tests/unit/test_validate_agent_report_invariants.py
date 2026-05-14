"""Unit tests for typed agent report cross-kind invariants."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.state.enums import AgentReportVerdict, AgentSessionRole, Confidence, StoreKind
from eawf.state.models import State
from eawf.store.envelope import Envelope
from eawf.store.kinds.agent_report import (
    AgentReportBody,
    AgentReportEvidenceRef,
    AgentReportHeader,
    AgentReportPayload,
    AuditorReportBody,
    CriterionVerdict,
    ExecutorReportBody,
    OperatorReportBody,
    ReviewerReportBody,
    store_kind_for_role,
)
from eawf.validate.invariants import Violation, check_agent_report_invariants

pytestmark = pytest.mark.unit

NOW = datetime(2026, 5, 14, tzinfo=UTC)


def _base_state_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-05-14T00:00:00Z",
        "project": {
            "code": "EAWF",
            "slug": "eawf",
            "title": "Eawf",
            "description": "",
            "domains": ["workflow"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:EAWF",
        },
        "current": {
            "project_code": "EAWF",
            "subproject_id": None,
            "phase_id": "P18",
            "iter_id": "P18-I01",
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P18": {
                "id": "P18",
                "scope_id": "EAWF",
                "title": "Typed reports",
                "status": "active",
                "iter_ids": ["P18-I01"],
                "outcome_ids": [],
                "opened_at": "2026-05-14T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            },
            "P19": {
                "id": "P19",
                "scope_id": "EAWF",
                "title": "Other phase",
                "status": "active",
                "iter_ids": ["P19-I01"],
                "outcome_ids": [],
                "opened_at": "2026-05-14T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            },
        },
        "iters": {
            "P18-I01": {
                "id": "P18-I01",
                "phase_id": "P18",
                "title": "Main iter",
                "status": "active",
                "wave_ids": ["P18-I01-W01"],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-05-14T00:00:00Z",
                "closed_at": None,
            },
            "P19-I01": {
                "id": "P19-I01",
                "phase_id": "P19",
                "title": "Other iter",
                "status": "active",
                "wave_ids": ["P19-I01-W01"],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-05-14T00:00:00Z",
                "closed_at": None,
            },
        },
        "waves": {
            "P18-I01-W01": {
                "id": "P18-I01-W01",
                "iter_id": "P18-I01",
                "title": "Executor wave",
                "status": "closed",
                "deps": [],
                "blocks": [],
                "file_scopes": [],
                "success_criteria": [],
                "agent_role": "executor",
                "effort_bucket": "S",
                "claim_session_id": "SES-executor",
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": "done",
                "opened_at": "2026-05-14T00:00:00Z",
                "closed_at": "2026-05-14T01:00:00Z",
            },
            "P19-I01-W01": {
                "id": "P19-I01-W01",
                "iter_id": "P19-I01",
                "title": "Other wave",
                "status": "closed",
                "deps": [],
                "blocks": [],
                "file_scopes": [],
                "success_criteria": [],
                "agent_role": "executor",
                "effort_bucket": "S",
                "claim_session_id": None,
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": "done",
                "opened_at": "2026-05-14T00:00:00Z",
                "closed_at": "2026-05-14T01:00:00Z",
            },
        },
        "artifacts": {},
        "agent_sessions": {
            "SES-executor": _session("SES-executor", "executor", "P18-I01-W01"),
            "SES-reviewer": _session("SES-reviewer", "reviewer", "P18-I01"),
            "SES-auditor": _session("SES-auditor", "auditor", "P18-I01-W01"),
            "SES-operator": _session("SES-operator", "operator", "P18"),
        },
        "plugins": {},
        "indexes": {},
    }


def _session(session_id: str, role: str, scope_id: str) -> dict[str, Any]:
    return {
        "id": session_id,
        "role": role,
        "runtime": "codex",
        "scope_id": scope_id,
        "status": "active",
        "claimed_wave_ids": [],
        "worktree_ids": [],
        "artifact_ids": [],
        "started_at": "2026-05-14T00:00:00Z",
        "ended_at": None,
        "summary": None,
    }


def _state() -> State:
    return State.model_validate(_base_state_payload())


def _executor_body(commit_sha: str | None = "abcdef1") -> ExecutorReportBody:
    return ExecutorReportBody(
        role="executor",
        verdict=AgentReportVerdict.PASS,
        confidence=Confidence.HIGH,
        summary="executor summary",
        wave_id="P18-I01-W01",
        files_changed=["src/eawf/validate/invariants.py"],
        tests_run=["uv run pytest tests/unit/test_validate_agent_report_invariants.py -q"],
        commit_sha=commit_sha,
        outcome="done",
    )


def _reviewer_body(*, coverage: bool = True) -> ReviewerReportBody:
    coverage_refs = (
        [AgentReportEvidenceRef(kind="repo", ref="src/eawf/validate/invariants.py:1")]
        if coverage
        else []
    )
    return ReviewerReportBody(
        role="reviewer",
        verdict=AgentReportVerdict.PASS,
        confidence=Confidence.HIGH,
        summary="reviewer summary",
        target_id="HEAD",
        findings=[],
        coverage_refs=coverage_refs,
    )


def _auditor_body(*, criteria: bool = True) -> AuditorReportBody:
    criteria_rows = [CriterionVerdict(criterion="criterion", passed=True)] if criteria else []
    return AuditorReportBody(
        role="auditor",
        verdict=AgentReportVerdict.PASS,
        confidence=Confidence.HIGH,
        summary="auditor summary",
        target_id="P18-I01-W01",
        criteria=criteria_rows,
        refutations=[],
    )


def _operator_body(completed_wave_ids: list[str] | None = None) -> OperatorReportBody:
    return OperatorReportBody(
        role="operator",
        verdict=AgentReportVerdict.PASS,
        confidence=Confidence.HIGH,
        summary="operator summary",
        phase_id="P18",
        completed_wave_ids=completed_wave_ids or ["P18-I01-W01"],
        decisions=[],
        next_actions=[],
    )


def _envelope(
    role: AgentSessionRole,
    body: AgentReportBody,
    *,
    session_id: str,
    scope_id: str,
    base_id: str,
    attempt: int = 1,
    report_id: str | None = None,
    kind: StoreKind | None = None,
    envelope_scope_id: str | None = None,
) -> Envelope:
    token = role.value.replace("-", "_")
    resolved_report_id = report_id or f"AR-{token}-{attempt:02d}"
    header = AgentReportHeader(
        report_id=resolved_report_id,
        role=role,
        session_id=session_id,
        scope_id=scope_id,
        base_id=base_id,
        attempt=attempt,
        runtime="codex",
        generated_at=NOW,
        summary=body.summary,
    )
    payload = AgentReportPayload(header=header, body=body)
    return Envelope(
        id=resolved_report_id,
        kind=kind or store_kind_for_role(role),
        scope_id=envelope_scope_id if envelope_scope_id is not None else scope_id,
        created_at=NOW,
        updated_at=None,
        summary=body.summary,
        payload=payload.model_dump(mode="json"),
    )


def _codes(violations: list[Violation]) -> set[str]:
    return {violation.code for violation in violations}


def test_check_agent_report_invariants_accepts_valid_report_set() -> None:
    reports = [
        _envelope(
            AgentSessionRole.EXECUTOR,
            _executor_body(),
            session_id="SES-executor",
            scope_id="P18-I01-W01",
            base_id="P18-I01-W01",
        ),
        _envelope(
            AgentSessionRole.REVIEWER,
            _reviewer_body(),
            session_id="SES-reviewer",
            scope_id="P18-I01",
            base_id="P18-I01",
        ),
        _envelope(
            AgentSessionRole.AUDITOR,
            _auditor_body(),
            session_id="SES-auditor",
            scope_id="P18-I01-W01",
            base_id="P18-I01-W01",
        ),
        _envelope(
            AgentSessionRole.OPERATOR,
            _operator_body(),
            session_id="SES-operator",
            scope_id="P18",
            base_id="P18",
        ),
    ]
    assert list(check_agent_report_invariants(_state(), reports)) == []


def test_check_agent_report_invariants_flags_store_kind_mismatch() -> None:
    report = _envelope(
        AgentSessionRole.EXECUTOR,
        _executor_body(),
        session_id="SES-executor",
        scope_id="P18-I01-W01",
        base_id="P18-I01-W01",
        kind=StoreKind.REVIEWER_REPORT,
    )
    violations = list(check_agent_report_invariants(_state(), [report]))
    assert "INV.AGENT_REPORT.STORE_KIND_MISMATCH" in _codes(violations)


def test_check_agent_report_invariants_flags_session_role_mismatch() -> None:
    report = _envelope(
        AgentSessionRole.EXECUTOR,
        _executor_body(),
        session_id="SES-reviewer",
        scope_id="P18-I01",
        base_id="P18-I01",
    )
    violations = list(check_agent_report_invariants(_state(), [report]))
    assert "INV.AGENT_REPORT.SESSION_ROLE_MISMATCH" in _codes(violations)


def test_check_agent_report_invariants_flags_missing_scope() -> None:
    report = _envelope(
        AgentSessionRole.EXECUTOR,
        _executor_body(),
        session_id="SES-executor",
        scope_id="P18-I01-W99",
        base_id="P18-I01-W99",
    )
    violations = list(check_agent_report_invariants(_state(), [report]))
    assert "INV.AGENT_REPORT.SCOPE_MISSING" in _codes(violations)


def test_check_agent_report_invariants_flags_attempt_gap() -> None:
    reports = [
        _envelope(
            AgentSessionRole.EXECUTOR,
            _executor_body(),
            session_id="SES-executor",
            scope_id="P18-I01-W01",
            base_id="P18-I01-W01",
            attempt=1,
            report_id="AR-executor-01",
        ),
        _envelope(
            AgentSessionRole.EXECUTOR,
            _executor_body(),
            session_id="SES-executor",
            scope_id="P18-I01-W01",
            base_id="P18-I01-W01",
            attempt=3,
            report_id="AR-executor-03",
        ),
    ]
    violations = list(check_agent_report_invariants(_state(), reports))
    assert "INV.AGENT_REPORT.ATTEMPT_GAP" in _codes(violations)


def test_check_agent_report_invariants_flags_executor_missing_commit() -> None:
    report = _envelope(
        AgentSessionRole.EXECUTOR,
        _executor_body(commit_sha=None),
        session_id="SES-executor",
        scope_id="P18-I01-W01",
        base_id="P18-I01-W01",
    )
    violations = list(check_agent_report_invariants(_state(), [report]))
    assert "INV.AGENT_REPORT.EXECUTOR_COMMIT_MISSING" in _codes(violations)


def test_check_agent_report_invariants_flags_reviewer_missing_coverage() -> None:
    report = _envelope(
        AgentSessionRole.REVIEWER,
        _reviewer_body(coverage=False),
        session_id="SES-reviewer",
        scope_id="P18-I01",
        base_id="P18-I01",
    )
    violations = list(check_agent_report_invariants(_state(), [report]))
    assert "INV.AGENT_REPORT.REVIEWER_COVERAGE_MISSING" in _codes(violations)


def test_check_agent_report_invariants_flags_auditor_missing_criteria() -> None:
    report = _envelope(
        AgentSessionRole.AUDITOR,
        _auditor_body(criteria=False),
        session_id="SES-auditor",
        scope_id="P18-I01-W01",
        base_id="P18-I01-W01",
    )
    violations = list(check_agent_report_invariants(_state(), [report]))
    assert "INV.AGENT_REPORT.AUDITOR_CRITERIA_MISSING" in _codes(violations)


def test_check_agent_report_invariants_flags_operator_phase_coverage_mismatch() -> None:
    report = _envelope(
        AgentSessionRole.OPERATOR,
        _operator_body(completed_wave_ids=["P19-I01-W01"]),
        session_id="SES-operator",
        scope_id="P18",
        base_id="P18",
    )
    violations = list(check_agent_report_invariants(_state(), [report]))
    assert "INV.AGENT_REPORT.OPERATOR_WAVE_PHASE_MISMATCH" in _codes(violations)
