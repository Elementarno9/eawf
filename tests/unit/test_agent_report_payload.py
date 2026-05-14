"""Unit tests for typed agent report payload primitives."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.state.enums import AgentReportVerdict, AgentSessionRole, Confidence
from eawf.store.kinds.agent_report import (
    AgentReportBody,
    AgentReportEvidenceRef,
    AgentReportFollowup,
    AgentReportHeader,
    AgentReportPayload,
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


def _body() -> AgentReportBody:
    return AgentReportBody(
        verdict=AgentReportVerdict.PASS,
        confidence=Confidence.HIGH,
        summary="implemented shared primitives",
        evidence_refs=[
            AgentReportEvidenceRef(
                kind="repo",
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
