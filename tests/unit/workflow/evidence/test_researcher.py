"""Researcher agent-report evidence invariants."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import AgentReportVerdict, AgentSessionRole, Confidence
from eawf.kernel.store.kinds.agent_report import (
    AgentReportEvidenceRef,
    AgentReportHeader,
    AgentReportPayload,
    ResearcherReportBody,
)


def _researcher_body(
    *,
    verdict: AgentReportVerdict = AgentReportVerdict.PASS,
    evidence_refs: list[AgentReportEvidenceRef] | None = None,
) -> ResearcherReportBody:
    return ResearcherReportBody(
        role="researcher",
        verdict=verdict,
        confidence=Confidence.HIGH,
        summary="researched the question",
        evidence_refs=list(evidence_refs or []),
        question="which path?",
        recommendation="choose the cited path",
    )


def _payload(body: ResearcherReportBody) -> AgentReportPayload:
    return AgentReportPayload(
        header=AgentReportHeader(
            report_id="AR-researcher-P18-I01-W04-01",
            role=AgentSessionRole.RESEARCHER,
            session_id="SES-001",
            scope_id="P18-I01-W04",
            base_id="P18-I01-W04",
            attempt=1,
            runtime="codex",
            generated_at=datetime(2026, 5, 14, tzinfo=UTC),
            summary=body.summary,
        ),
        body=body,
    )


def test_researcher_pass_requires_non_empty_evidence_refs() -> None:
    with pytest.raises(ValidationError, match="researcher pass requires non-empty"):
        _payload(_researcher_body())


def test_researcher_pass_accepts_evidence_refs() -> None:
    body = _researcher_body(
        evidence_refs=[AgentReportEvidenceRef(kind="artifact", ref="docs/source.md")]
    )
    payload = _payload(body)
    assert payload.body.verdict is AgentReportVerdict.PASS
    assert len(payload.body.evidence_refs) == 1


def test_researcher_pass_with_followups_may_defer_evidence_refs() -> None:
    body = _researcher_body(verdict=AgentReportVerdict.PASS_WITH_FOLLOWUPS)
    payload = _payload(body)
    assert payload.body.verdict is AgentReportVerdict.PASS_WITH_FOLLOWUPS
    assert payload.body.evidence_refs == []
