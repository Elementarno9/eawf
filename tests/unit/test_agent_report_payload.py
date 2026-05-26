"""Unit tests for typed agent report payload primitives."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import AgentReportVerdict, AgentSessionRole, Confidence
from eawf.kernel.store.kinds.agent_report import (
    AgentReportEvidenceRef,
    AgentReportFollowup,
    AgentReportHeader,
    AgentReportPayload,
    AuditorReportBody,
    CriterionVerdict,
    DomainSpecialistReportBody,
    ExecutorReportBody,
    OperatorReportBody,
    PlannedWaveSummary,
    PlannerReportBody,
    PolishChange,
    PolisherReportBody,
    ResearcherReportBody,
    ReviewerReportBody,
    ReviewFinding,
    report_record_id,
)


def _header() -> AgentReportHeader:
    return AgentReportHeader(
        report_id="AR-executor-P18-I01-W01-01",
        role=AgentSessionRole.EXECUTOR,
        session_id="SES-001",
        scope_id="P18-I01-W01",
        base_id="P18-I01-W01",
        attempt=1,
        runtime="codex",
        generated_at=datetime(2026, 5, 14, tzinfo=UTC),
        summary="executor report",
        artifact_ids=["ART-1", "ART-1"],
    )


def _body() -> ExecutorReportBody:
    return ExecutorReportBody(
        verdict=AgentReportVerdict.PASS,
        confidence=Confidence.HIGH,
        summary="implemented shared primitives",
        evidence_refs=[
            AgentReportEvidenceRef(
                kind="artifact",
                ref="src/eawf/store/kinds/agent_report.py:1",
                note="payload models",
            )
        ],
        followups=[
            AgentReportFollowup(
                title="wire payload into store registry",
                owner_role=AgentSessionRole.EXECUTOR,
                priority="P2",
            )
        ],
        wave_id="P18-I01-W01",
        files_changed=["src/eawf/store/kinds/agent_report.py"],
        tests_run=["uv run pytest tests/unit/test_agent_report_payload.py -q"],
        commit_sha="commit1",
        outcome="implemented shared primitives",
    )


def test_agent_report_payload_valid_round_trip() -> None:
    payload = AgentReportPayload(header=_header(), body=_body())
    loaded = AgentReportPayload.model_validate_json(payload.model_dump_json())
    assert loaded.header.role is AgentSessionRole.EXECUTOR
    assert loaded.body.verdict is AgentReportVerdict.PASS
    assert loaded.body.confidence is Confidence.HIGH
    assert loaded.header.artifact_ids == ["ART-1"]


def test_agent_report_payload_rejects_extra_fields() -> None:
    raw = {
        "header": _header().model_dump(mode="json"),
        "body": _body().model_dump(mode="json"),
        "unexpected": True,
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AgentReportPayload.model_validate(raw)


def test_agent_report_payload_rejects_role_mismatch() -> None:
    raw = {
        "header": _header().model_dump(mode="json") | {"role": "reviewer"},
        "body": _body().model_dump(mode="json"),
    }
    with pytest.raises(ValidationError, match="does not match header role"):
        AgentReportPayload.model_validate(raw)


def test_agent_report_header_rejects_zero_attempt() -> None:
    raw = _header().model_dump(mode="json")
    raw["attempt"] = 0
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        AgentReportHeader.model_validate(raw)


def test_agent_report_verdict_values() -> None:
    expected = {"pass", "pass-with-followups", "fail", "blocked"}
    actual = {member.value for member in AgentReportVerdict}
    assert actual == expected
    with pytest.raises(ValueError):
        AgentReportVerdict("maybe")


def test_report_record_id_normalises_role_and_base_id() -> None:
    report_id = report_record_id(
        role=AgentSessionRole.DOMAIN_SPECIALIST,
        base_id="P18/I01 W01",
        attempt=2,
    )
    assert report_id == "AR-domain_specialist-P18-I01-W01-02"


def test_report_record_id_rejects_empty_base() -> None:
    with pytest.raises(ValueError, match="base_id must be non-empty"):
        report_record_id(role=AgentSessionRole.REVIEWER, base_id=" ", attempt=1)


def test_all_role_bodies_validate_through_payload() -> None:
    bodies = [
        ResearcherReportBody(
            role="researcher",
            verdict=AgentReportVerdict.PASS,
            confidence=Confidence.HIGH,
            summary="researched",
            question="which path?",
            findings=["current store is JSONL"],
            recommendation="add typed report payloads",
        ),
        PlannerReportBody(
            role="planner",
            verdict=AgentReportVerdict.PASS,
            confidence=Confidence.MEDIUM,
            summary="planned",
            objective="split work",
            waves=[
                PlannedWaveSummary(
                    wave_id="P18-I01-W01",
                    title="Shared primitives",
                    success_criteria=["tests pass"],
                )
            ],
        ),
        _body(),
        AuditorReportBody(
            role="auditor",
            verdict=AgentReportVerdict.PASS_WITH_FOLLOWUPS,
            confidence=Confidence.MEDIUM,
            summary="audited",
            target_id="P18-I01-W01",
            criteria=[
                CriterionVerdict(
                    criterion="strict models exist",
                    passed=True,
                    evidence_refs=[
                        AgentReportEvidenceRef(kind="artifact", ref="tests/unit/x.py:1")
                    ],
                )
            ],
        ),
        ReviewerReportBody(
            role="reviewer",
            verdict=AgentReportVerdict.FAIL,
            confidence=Confidence.HIGH,
            summary="reviewed",
            target_id="HEAD",
            findings=[
                ReviewFinding(
                    severity="must-fix",
                    message="missing test",
                    evidence_refs=[AgentReportEvidenceRef(kind="artifact", ref="abcdef0")],
                )
            ],
        ),
        PolisherReportBody(
            role="polisher",
            verdict=AgentReportVerdict.PASS,
            confidence=Confidence.LOW,
            summary="polished",
            scope_id="src/eawf",
            changes=[PolishChange(category="naming", summary="aligned field names")],
        ),
        OperatorReportBody(
            role="operator",
            verdict=AgentReportVerdict.BLOCKED,
            confidence=Confidence.MEDIUM,
            summary="operated",
            phase_id="P18",
            completed_wave_ids=["P18-I01-W01"],
            next_actions=["continue W02"],
        ),
        DomainSpecialistReportBody(
            role="domain-specialist",
            verdict=AgentReportVerdict.PASS,
            confidence=Confidence.MEDIUM,
            summary="domain checked",
            domain="workflow",
            assessment="report model fits append-only workflow evidence",
            recommendations=["keep markdown renderer separate"],
        ),
    ]
    for body in bodies:
        header = _header().model_copy(update={"role": AgentSessionRole(body.role)})
        payload = AgentReportPayload(header=header, body=body)
        loaded = AgentReportPayload.model_validate_json(payload.model_dump_json())
        assert loaded.body.role == body.role


def test_role_body_rejects_unknown_field() -> None:
    raw = _body().model_dump(mode="json")
    raw["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ExecutorReportBody.model_validate(raw)
