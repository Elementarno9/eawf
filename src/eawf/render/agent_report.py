"""Markdown renderers for typed agent reports."""

from __future__ import annotations

from collections.abc import Iterable

from eawf.store.kinds.agent_report import (
    AgentReportCommonBody,
    AgentReportEvidenceRef,
    AgentReportFollowup,
    AgentReportPayload,
    AuditorReportBody,
    DomainSpecialistReportBody,
    ExecutorReportBody,
    OperatorReportBody,
    PlannerReportBody,
    PolisherReportBody,
    ResearcherReportBody,
    ReviewerReportBody,
)


def _list_or_none(items: Iterable[str]) -> list[str]:
    rows = [f"- {item}" for item in items]
    return rows or ["- (none)"]


def _evidence_rows(items: list[AgentReportEvidenceRef]) -> list[str]:
    if not items:
        return ["- (none)"]
    rows: list[str] = []
    for item in items:
        note = f" - {item.note}" if item.note else ""
        rows.append(f"- {item.kind}: `{item.ref}`{note}")
    return rows


def _followup_rows(items: list[AgentReportFollowup]) -> list[str]:
    if not items:
        return ["- (none)"]
    rows: list[str] = []
    for item in items:
        owner = f" owner={item.owner_role.value}" if item.owner_role is not None else ""
        detail = f" - {item.detail}" if item.detail else ""
        rows.append(f"- [{item.priority}]{owner} {item.title}{detail}")
    return rows


def _body_or_raise(report: AgentReportPayload) -> AgentReportCommonBody:
    body = report.body
    if not isinstance(body, AgentReportCommonBody):
        raise TypeError(f"unsupported agent report body: {type(body).__name__}")
    return body


def _common_lines(report: AgentReportPayload, body: AgentReportCommonBody) -> list[str]:
    header = report.header
    return [
        f"# Agent Report: {header.role.value}",
        "",
        f"- report: `{header.report_id}`",
        f"- role: `{header.role.value}`",
        f"- scope_id: `{header.scope_id}`",
        f"- base: `{header.base_id}`",
        f"- attempt: `{header.attempt}`",
        f"- verdict: `{body.verdict.value}`",
        f"- confidence: `{body.confidence.value}`",
        f"- runtime: `{header.runtime}`",
        "",
        "## Summary",
        "",
        body.summary,
        "",
    ]


def _role_details(body: AgentReportCommonBody) -> list[str]:
    if isinstance(body, ResearcherReportBody):
        return [
            "## Research",
            "",
            f"- question: {body.question}",
            f"- recommendation: {body.recommendation}",
            "",
            "Findings:",
            *_list_or_none(body.findings),
            "",
            "Alternatives:",
            *_list_or_none(body.alternatives),
        ]
    if isinstance(body, PlannerReportBody):
        lines = ["## Plan", "", f"- objective: {body.objective}", "", "Waves:"]
        lines.extend(
            f"- `{wave.wave_id}` {wave.title} deps={wave.depends_on or []}" for wave in body.waves
        )
        if not body.waves:
            lines.append("- (none)")
        lines.extend(["", "Risks:", *_list_or_none(body.risks)])
        return lines
    if isinstance(body, ExecutorReportBody):
        lines = [
            "## Execution",
            "",
            f"- wave: `{body.wave_id}`",
            f"- outcome: {body.outcome}",
            f"- commit: `{body.commit_sha}`" if body.commit_sha else "- commit: (none)",
            "",
            "Files changed:",
            *_list_or_none(f"`{path}`" for path in body.files_changed),
            "",
            "Tests run:",
            *_list_or_none(f"`{test}`" for test in body.tests_run),
        ]
        return lines
    if isinstance(body, AuditorReportBody):
        lines = ["## Audit", "", f"- target: `{body.target_id}`", "", "Criteria:"]
        lines.extend(
            f"- [{'x' if criterion.passed else ' '}] {criterion.criterion}"
            for criterion in body.criteria
        )
        if not body.criteria:
            lines.append("- (none)")
        lines.extend(["", "Refutations:", *_list_or_none(body.refutations)])
        return lines
    if isinstance(body, ReviewerReportBody):
        lines = ["## Review", "", f"- target: `{body.target_id}`", "", "Findings:"]
        lines.extend(f"- {finding.severity}: {finding.message}" for finding in body.findings)
        if not body.findings:
            lines.append("- (none)")
        lines.extend(["", "Coverage:", *_evidence_rows(body.coverage_refs)])
        return lines
    if isinstance(body, PolisherReportBody):
        lines = ["## Polish", "", f"- scope_id: `{body.scope_id}`", "", "Changes:"]
        lines.extend(
            f"- {change.category}: {change.summary} files={change.files or []}"
            for change in body.changes
        )
        if not body.changes:
            lines.append("- (none)")
        lines.extend(["", "Deferred:", *_list_or_none(body.deferred_items)])
        return lines
    if isinstance(body, OperatorReportBody):
        return [
            "## Operator",
            "",
            f"- phase: `{body.phase_id}`",
            "",
            "Completed waves:",
            *_list_or_none(f"`{wave_id}`" for wave_id in body.completed_wave_ids),
            "",
            "Decisions:",
            *_list_or_none(body.decisions),
            "",
            "Next actions:",
            *_list_or_none(body.next_actions),
        ]
    if isinstance(body, DomainSpecialistReportBody):
        return [
            "## Domain",
            "",
            f"- domain: {body.domain}",
            f"- assessment: {body.assessment}",
            "",
            "Recommendations:",
            *_list_or_none(body.recommendations),
        ]
    raise TypeError(f"unsupported agent report body: {type(body).__name__}")


def render_agent_report(report: AgentReportPayload) -> str:
    """Render one typed agent report as deterministic Markdown."""
    body = _body_or_raise(report)
    lines = [
        *_common_lines(report, body),
        *_role_details(body),
        "",
        "## Evidence",
        "",
        *_evidence_rows(body.evidence_refs),
        "",
        "## Follow-ups",
        "",
        *_followup_rows(body.followups),
        "",
    ]
    return "\n".join(lines)
