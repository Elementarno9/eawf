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
from typing import Literal

from jinja2 import Environment, StrictUndefined
from pydantic import BaseModel, ConfigDict

from eawf.artifacts.references import Citation
from eawf.artifacts.validation import validate_text_surface
from eawf.profiles.models import ComposedProfile
from eawf.state.models import State

logger = logging.getLogger(__name__)

PrBodyInputKind = Literal["operator_rollup", "executor_report", "reviewer_report", "docs_research"]


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
        lines.append("References:")
        lines.extend(f"[{c.n}] {c.ref}" for c in input_model.citations)
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


def build_pr_body(
    state: State,
    phase_id: str,
    *,
    inputs: list[PrBodyInput] | None = None,
    composed_profile: ComposedProfile | None = None,
    kind: str = "phase",
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
    report = validate_text_surface(body, surface="pr")
    if not report.ok:
        raise PrBodyValidationError("; ".join(report.errors))
    return body


__all__ = [
    "PrBodyInput",
    "PrBodyNotFound",
    "PrBodyValidationError",
    "build_pr_body",
    "render_docs_research_report",
    "render_executor_report",
    "render_operator_rollup",
    "render_reviewer_report",
]
