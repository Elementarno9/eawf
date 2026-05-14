"""Render a phase PR body (Markdown) from ``state.json``.

The output mirrors the convention captured in ``AGENTS.md`` (the project
PR template):

- ``## Summary`` — 3-6 bullets pulled from phase-scoped decisions and the
  titles of iters under the phase.
- ``## Phase deliverables`` — table of every wave under the phase with
  ``wave_id``, ``title``, the wave's commit short-SHA (first 7 chars), and
  ``outcome``.
- ``## Test plan`` — standard checklist anchored at the phase.

Public API: :func:`build_pr_body` returns the rendered Markdown string given
a validated :class:`State` and a phase id.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

from jinja2 import Environment, StrictUndefined
from pydantic import BaseModel, ConfigDict

from eawf.agent_report.rollup import AgentReportRow, iter_agent_reports, operator_rollup
from eawf.artifacts.references import Citation
from eawf.artifacts.validation import validate_text_surface
from eawf.profiles.models import ComposedProfile
from eawf.state.enums import AgentSessionRole
from eawf.state.ids import is_iter_id, is_phase_id, is_wave_id
from eawf.state.models import State
from eawf.store.kinds.agent_report import (
    AgentReportEvidenceRef,
    ExecutorReportBody,
    ResearcherReportBody,
    ReviewerReportBody,
)

logger = logging.getLogger(__name__)

PrBodyInputKind = Literal["operator_rollup", "executor_report", "reviewer_report", "docs_research"]
PrKind = Literal["phase", "iter", "docs-research", "incident-fix"]


class PrBodyNotFound(LookupError):  # noqa: N818 — pairs with cli.errors.NotFound naming
    """Raised when the requested phase id is not present in ``state.phases``."""


class PrBodyValidationError(ValueError):
    """Raised when rendered PR text fails outbound validation."""


class PrBodyInput(BaseModel):
    """Typed input artifact used to render a PR body section."""

    model_config = ConfigDict(extra="forbid")

    kind: PrBodyInputKind
    title: str
    summary: str
    bullets: list[str] = []
    artifact_ids: list[str] = []
    citations: list[Citation] = []


def infer_pr_kind(
    scope_id: str,
    *,
    source: str | None = None,
    incident_id: str | None = None,
) -> PrKind:
    """Infer the renderer kind for a PR surface."""
    if incident_id is not None or scope_id.startswith("INC-"):
        return "incident-fix"
    source_key = (source or "").strip().casefold().replace("_", "-")
    if source_key in {"docs-research", "docs", "research"}:
        return "docs-research"
    if is_iter_id(scope_id):
        return "iter"
    if is_phase_id(scope_id):
        return "phase"
    return "phase"


def resolve_pr_phase_id(state: State, scope_id: str) -> str:
    """Resolve a phase/iter/wave PR scope to its owning phase id.

    Raises:
        PrBodyNotFound: When *scope_id* does not resolve to a known phase.
    """
    if is_phase_id(scope_id):
        if scope_id not in state.phases:
            raise PrBodyNotFound(f"phase not found: {scope_id!r}")
        return scope_id
    if is_iter_id(scope_id):
        iter_record = state.iters.get(scope_id)
        if iter_record is None:
            raise PrBodyNotFound(f"iter not found: {scope_id!r}")
        return iter_record.phase_id
    if is_wave_id(scope_id):
        wave = state.waves.get(scope_id)
        if wave is None:
            raise PrBodyNotFound(f"wave not found: {scope_id!r}")
        iter_record = state.iters.get(wave.iter_id)
        if iter_record is None:
            raise PrBodyNotFound(f"iter not found: {wave.iter_id!r}")
        return iter_record.phase_id
    raise PrBodyNotFound(f"unsupported PR scope: {scope_id!r}")


def _short_sha(value: str | None) -> str:
    if not value:
        return "(pending)"
    return value[:7]


def _decisions_for_phase(state: State, phase_id: str) -> list[str]:
    """Return summaries of decisions whose ``scope_id`` mentions the phase.

    Decisions are EAWF-wide in v0.2; we surface only those whose summary
    contains the phase id (e.g. ``"P06 rescope: ..."``) so PR bodies are
    phase-relevant rather than dumping every cross-phase decision.
    """
    decisions = state.decisions or {}
    out: list[str] = []
    for d in sorted(decisions.values(), key=lambda r: r.id):
        if phase_id in d.summary:
            out.append(d.summary)
    return out


def _iters_for_phase(state: State, phase_id: str) -> list[tuple[str, str]]:
    """Return ``(iter_id, title)`` rows for iters under *phase_id* (id order)."""
    iters = state.iters or {}
    return [
        (it.id, it.title)
        for it in sorted(iters.values(), key=lambda r: r.id)
        if it.phase_id == phase_id
    ]


def _waves_for_phase(state: State, phase_id: str) -> list[tuple[str, str, str, str]]:
    """Return ``(wave_id, title, short_commit, outcome)`` rows for waves in the phase."""
    iter_ids = {iid for iid, _t in _iters_for_phase(state, phase_id)}
    waves = state.waves or {}
    rows: list[tuple[str, str, str, str]] = []
    for w in sorted(waves.values(), key=lambda r: r.id):
        if w.iter_id not in iter_ids:
            continue
        rows.append(
            (
                w.id,
                w.title,
                _short_sha(w.commit),
                w.outcome or "",
            )
        )
    return rows


def _escape_table_cell(value: str) -> str:
    return value.replace("|", "\\|")


def _render_source_rows(citations: list[Citation]) -> list[str]:
    if not citations:
        return []
    rows = [
        "Source rows:",
        "",
        "| Citation | Kind | Reference | Note |",
        "|---|---|---|---|",
    ]
    for citation in citations:
        note = citation.note or citation.title or ""
        rows.append(
            "| "
            f"[{citation.n}] | "
            f"{citation.kind} | "
            f"`{_escape_table_cell(citation.ref)}` | "
            f"{_escape_table_cell(note)} |"
        )
    return rows


def _render_input_section(input_model: PrBodyInput, heading: str) -> str:
    lines = [
        f"## {heading}",
        "",
        f"### {input_model.title}",
        "",
        input_model.summary,
    ]
    if input_model.bullets:
        lines.append("")
        lines.extend(f"- {bullet}" for bullet in input_model.bullets)
    if input_model.artifact_ids:
        lines.append("")
        artifact_list = ", ".join(f"`{artifact_id}`" for artifact_id in input_model.artifact_ids)
        lines.append(f"Artifacts: {artifact_list}")
    if input_model.citations:
        lines.append("")
        lines.extend(_render_source_rows(input_model.citations))
    return "\n".join(lines)


def render_operator_rollup(input_model: PrBodyInput) -> str:
    """Render the operator rollup section."""
    return _render_input_section(input_model, "Operator Rollup")


def render_executor_report(input_model: PrBodyInput) -> str:
    """Render the executor report section."""
    return _render_input_section(input_model, "Executor Report")


def render_reviewer_report(input_model: PrBodyInput) -> str:
    """Render the reviewer report section."""
    return _render_input_section(input_model, "Reviewer Report")


def render_docs_research_report(input_model: PrBodyInput) -> str:
    """Render the docs/research report section."""
    return _render_input_section(input_model, "Docs And Research Report")


_INPUT_RENDERERS: dict[PrBodyInputKind, Callable[[PrBodyInput], str]] = {
    "operator_rollup": render_operator_rollup,
    "executor_report": render_executor_report,
    "reviewer_report": render_reviewer_report,
    "docs_research": render_docs_research_report,
}


def _report_scope_matches(row: AgentReportRow, scope_id: str) -> bool:
    header = row.payload.header
    return (
        header.scope_id == scope_id
        or header.scope_id.startswith(f"{scope_id}-")
        or header.base_id == scope_id
        or header.base_id.startswith(f"{scope_id}-")
    )


def _citations_for_evidence_refs(refs: list[AgentReportEvidenceRef]) -> list[Citation]:
    citations: list[Citation] = []
    for item in refs:
        if item.kind == "commit":
            continue
        kind: Literal["repo", "url", "urn"] = item.kind
        citations.append(
            Citation(
                n=len(citations) + 1,
                ref=item.ref,
                kind=kind,
                title=item.note,
            )
        )
    return citations


def _summary_bullets(row: AgentReportRow) -> list[str]:
    payload = row.payload
    body = payload.body
    bullets = [
        (
            f"`{payload.header.report_id}` verdict={body.verdict.value} "
            f"confidence={body.confidence.value}"
        )
    ]
    if isinstance(body, ExecutorReportBody):
        bullets.append(f"wave `{body.wave_id}` commit `{body.commit_sha or '(none)'}`")
        bullets.extend(f"test `{test}`" for test in body.tests_run)
    elif isinstance(body, ReviewerReportBody):
        bullets.append(f"target `{body.target_id}` findings={len(body.findings)}")
    elif isinstance(body, ResearcherReportBody):
        bullets.append(f"question: {body.question}")
        bullets.append(f"recommendation: {body.recommendation}")
    return bullets


def _input_from_report(row: AgentReportRow, kind: PrBodyInputKind, title: str) -> PrBodyInput:
    body = row.payload.body
    return PrBodyInput(
        kind=kind,
        title=title,
        summary=body.summary,
        bullets=_summary_bullets(row),
        artifact_ids=row.payload.header.artifact_ids,
        citations=_citations_for_evidence_refs(body.evidence_refs),
    )


def collect_pr_report_inputs(
    state_path: Path,
    state: State,
    scope_id: str,
    *,
    kind: PrKind,
) -> list[PrBodyInput]:
    """Collect typed agent reports that should be included in a PR body."""
    del state
    inputs: list[PrBodyInput] = []
    if kind == "phase":
        rollup = operator_rollup(state_path, scope_id)
        report_count = cast(int, rollup["report_count"])
        if report_count:
            by_role = cast(dict[str, int], rollup["by_role"])
            bullets = [f"{role}: {count}" for role, count in sorted(by_role.items())]
            inputs.append(
                PrBodyInput(
                    kind="operator_rollup",
                    title=f"Agent report rollup for {scope_id}",
                    summary=f"{report_count} typed agent report(s) recorded for {scope_id}.",
                    bullets=bullets,
                )
            )
        return inputs

    if kind == "iter":
        role_inputs: tuple[tuple[AgentSessionRole, PrBodyInputKind], ...] = (
            (AgentSessionRole.EXECUTOR, "executor_report"),
            (AgentSessionRole.REVIEWER, "reviewer_report"),
        )
        for role, input_kind in role_inputs:
            rows = [
                row
                for row in iter_agent_reports(state_path, role=role)
                if _report_scope_matches(row, scope_id)
            ]
            inputs.extend(
                _input_from_report(row, input_kind, f"{role.value} report for {scope_id}")
                for row in rows
            )
        return inputs

    if kind == "docs-research":
        rows = [
            row
            for row in iter_agent_reports(state_path, role=AgentSessionRole.RESEARCHER)
            if _report_scope_matches(row, scope_id)
        ]
        inputs.extend(
            _input_from_report(row, "docs_research", f"Research report for {scope_id}")
            for row in rows
        )
    return inputs


def _render_profile_blocks(
    composed: ComposedProfile | None,
    *,
    kind: str,
    context: dict[str, object],
) -> list[str]:
    if composed is None:
        return []
    env = Environment(undefined=StrictUndefined, autoescape=False)
    rendered: list[str] = []
    target = f"pr.{kind}"
    for block in composed.render_blocks:
        if block.target != target:
            continue
        template = env.from_string(block.body_template)
        rendered.append(template.render(**context))
    return rendered


def _citation_rows_for_inputs(inputs: list[PrBodyInput] | None) -> list[Citation] | None:
    rows: list[Citation] = []
    for input_model in inputs or []:
        rows.extend(input_model.citations)
    return rows or None


def build_pr_body(
    state: State,
    phase_id: str,
    *,
    inputs: list[PrBodyInput] | None = None,
    composed_profile: ComposedProfile | None = None,
    kind: PrKind = "phase",
) -> str:
    """Render the Markdown PR body for *phase_id*.

    Args:
        state: Loaded, validated :class:`State`.
        phase_id: Target phase id (e.g. ``"P11"``).

    Returns:
        The rendered Markdown body as a single string.

    Raises:
        PrBodyNotFound: When *phase_id* is not in ``state.phases``.
    """
    phases = state.phases or {}
    phase = phases.get(phase_id)
    if phase is None:
        raise PrBodyNotFound(f"phase not found: {phase_id!r}")

    decisions = _decisions_for_phase(state, phase_id)
    iters = _iters_for_phase(state, phase_id)
    waves = _waves_for_phase(state, phase_id)

    audit_line = ""
    if phase.audit_id:
        audits = state.audits or {}
        audit = audits.get(phase.audit_id)
        if audit is not None and audit.verdict is not None:
            audit_line = f"Audit `{phase.audit_id}` verdict: **{audit.verdict.value}**."

    lines: list[str] = [
        f"# {phase_id}: {phase.title}",
        "",
        "## Summary",
        "",
    ]
    if audit_line:
        lines.append(f"- {audit_line}")
    for summary in decisions:
        lines.append(f"- {summary}")
    for _iter_id, title in iters:
        lines.append(f"- {title}")
    if not decisions and not iters and not audit_line:
        lines.append("- (no decisions or iters recorded for this phase)")
    lines.extend(["", "## Phase deliverables", ""])
    if waves:
        lines.append("| Wave | Title | Commit | Outcome |")
        lines.append("|------|-------|--------|---------|")
        for wave_id, title, short, outcome in waves:
            outcome_cell = outcome.replace("|", "\\|") if outcome else ""
            title_cell = title.replace("|", "\\|")
            lines.append(f"| `{wave_id}` | {title_cell} | `{short}` | {outcome_cell} |")
    else:
        lines.append("_(no waves recorded for this phase)_")
    for input_model in inputs or []:
        lines.extend(["", _INPUT_RENDERERS[input_model.kind](input_model)])

    context = {
        "phase_id": phase_id,
        "phase": phase,
        "decisions": decisions,
        "iters": iters,
        "waves": waves,
    }
    for block_body in _render_profile_blocks(composed_profile, kind=kind, context=context):
        lines.extend(["", block_body])

    lines.extend(
        [
            "",
            "## Test plan",
            "",
            "- [ ] `uv run pytest -q`",
            "- [ ] `uv run pre-commit run --all-files`",
            "- [ ] `uv run mypy src/eawf`",
        ]
    )
    body = "\n".join(lines) + "\n"
    report = validate_text_surface(
        body,
        surface="pr",
        references=_citation_rows_for_inputs(inputs),
    )
    if not report.ok:
        raise PrBodyValidationError("; ".join(report.errors))
    return body


__all__ = [
    "PrBodyInput",
    "PrBodyNotFound",
    "PrBodyValidationError",
    "PrKind",
    "build_pr_body",
    "collect_pr_report_inputs",
    "infer_pr_kind",
    "render_docs_research_report",
    "render_executor_report",
    "render_operator_rollup",
    "render_reviewer_report",
    "resolve_pr_phase_id",
]
