"""Golden tests for typed agent report markdown rendering."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.render.agent_report import render_agent_report
from eawf.state.enums import AgentReportVerdict, AgentSessionRole, Confidence
from eawf.store.kinds.agent_report import (
    AgentReportBody,
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
)

GOLDEN_DIR = Path(__file__).resolve().parent.parent / "golden" / "agent_report"


def _header(role: AgentSessionRole) -> AgentReportHeader:
    token = role.value.replace("-", "_")
    return AgentReportHeader(
        report_id=f"AR-{token}-P18-01",
        role=role,
        session_id="SES-001",
        scope_id="P18-I01-W07",
        base_id="P18-I01-W07",
        attempt=1,
        runtime="codex",
        generated_at=datetime(2026, 5, 14, tzinfo=UTC),
        summary=f"{role.value} summary",
    )


def _report(role: AgentSessionRole) -> AgentReportPayload:
    body: AgentReportBody
    match role:
        case AgentSessionRole.RESEARCHER:
            body = ResearcherReportBody(
                role="researcher",
                verdict=AgentReportVerdict.PASS,
                confidence=Confidence.HIGH,
                summary=f"{role.value} summary",
                question="What should ship?",
                findings=["Store reports as JSONL"],
                alternatives=["Markdown only"],
                recommendation="Use typed reports",
            )
        case AgentSessionRole.PLANNER:
            body = PlannerReportBody(
                role="planner",
                verdict=AgentReportVerdict.PASS,
                confidence=Confidence.HIGH,
                summary=f"{role.value} summary",
                objective="Plan report rendering",
                waves=[PlannedWaveSummary(wave_id="P18-I01-W07", title="Renderers")],
                risks=["golden drift"],
            )
        case AgentSessionRole.EXECUTOR:
            body = ExecutorReportBody(
                role="executor",
                verdict=AgentReportVerdict.PASS,
                confidence=Confidence.HIGH,
                summary=f"{role.value} summary",
                wave_id="P18-I01-W07",
                files_changed=["src/eawf/render/agent_report.py"],
                tests_run=["uv run pytest tests/unit/test_render_agent_report.py -q"],
                commit_sha="commit1",
                outcome="renderer added",
            )
        case AgentSessionRole.AUDITOR:
            body = AuditorReportBody(
                role="auditor",
                verdict=AgentReportVerdict.PASS,
                confidence=Confidence.HIGH,
                summary=f"{role.value} summary",
                target_id="P18-I01-W07",
                criteria=[CriterionVerdict(criterion="goldens pass", passed=True)],
            )
        case AgentSessionRole.REVIEWER:
            body = ReviewerReportBody(
                role="reviewer",
                verdict=AgentReportVerdict.PASS,
                confidence=Confidence.HIGH,
                summary=f"{role.value} summary",
                target_id="HEAD",
                findings=[ReviewFinding(severity="nit", message="tighten wording")],
            )
        case AgentSessionRole.POLISHER:
            body = PolisherReportBody(
                role="polisher",
                verdict=AgentReportVerdict.PASS,
                confidence=Confidence.HIGH,
                summary=f"{role.value} summary",
                scope="src/eawf",
                changes=[PolishChange(category="naming", summary="aligned names")],
            )
        case AgentSessionRole.OPERATOR:
            body = OperatorReportBody(
                role="operator",
                verdict=AgentReportVerdict.PASS,
                confidence=Confidence.HIGH,
                summary=f"{role.value} summary",
                phase_id="P18",
                completed_wave_ids=["P18-I01-W07"],
                decisions=["continue"],
                next_actions=["run W08"],
            )
        case AgentSessionRole.DOMAIN_SPECIALIST:
            body = DomainSpecialistReportBody(
                role="domain-specialist",
                verdict=AgentReportVerdict.PASS,
                confidence=Confidence.HIGH,
                summary=f"{role.value} summary",
                domain="workflow",
                assessment="report flow is coherent",
                recommendations=["keep markdown derived"],
            )
        case _:
            raise AssertionError(f"unknown role: {role.value!r}")
    return AgentReportPayload(header=_header(role), body=body)


@pytest.mark.parametrize("role", list(AgentSessionRole))
def test_render_agent_report_matches_golden(role: AgentSessionRole) -> None:
    rendered = render_agent_report(_report(role))
    golden = GOLDEN_DIR / f"{role.value}.md"
    assert rendered == golden.read_text(encoding="utf-8")


def test_render_agent_report_rejects_unknown_body_type() -> None:
    report = _report(AgentSessionRole.EXECUTOR)
    object.__setattr__(report, "body", object())
    with pytest.raises(TypeError, match="unsupported agent report body"):
        render_agent_report(report)
