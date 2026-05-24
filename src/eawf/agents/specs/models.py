"""Typed subagent-spec model (P27-I03-W14).

A :class:`SubagentSpec` is the typed intermediate the dispatch renderer
builds from a validated :class:`~eawf.kernel.state.models.State` snapshot. Before
this wave the wave prompt was assembled by concatenating section strings
straight out of ``State``; now the renderer projects ``State`` into a
``SubagentSpec`` and the spec renders itself. The two-step seam means the
prompt is produced from a typed, individually-testable structure rather
than an ad-hoc string blob.

Every section is its own strict Pydantic model with a ``render() -> str``
method that returns the section's Markdown body (no trailing newline).
:meth:`SubagentSpec.render` joins the section bodies with a blank line in
the same order the legacy renderer used, so the rendered bytes are
unchanged. The builder that fills a ``SubagentSpec`` from ``State`` lives
in :mod:`eawf.dispatch.renderer` (it needs the wave → iter → phase →
scope walk); the model layer stays pure data + formatting.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _SpecModel(BaseModel):
    """Base spec model — forbids unknown keys (project rule 2)."""

    model_config = ConfigDict(extra="forbid")


class SpecDependency(_SpecModel):
    """One blocking dependency row in a :class:`SubagentSpec`.

    Attributes:
        wave_id: The dependency wave id.
        title: The dependency wave title, or ``None`` when the dep id is
            dangling (the wave references an id absent from ``State``).
        status: The dependency wave status string, or ``None`` when the
            dep is dangling. ``None`` renders as ``status=unknown`` so the
            prompt surfaces the broken edge without raising.
    """

    wave_id: str
    title: str | None = None
    status: str | None = None

    def render(self) -> str:
        """Return the single-line dependency row Markdown."""
        if self.title is None or self.status is None:
            return f"- {self.wave_id}: (missing from state) (status=unknown)"
        return f"- {self.wave_id}: {self.title} (status={self.status})"


class SpecDecision(_SpecModel):
    """One in-scope decision row in a :class:`SubagentSpec`.

    Attributes:
        decision_id: The decision id (e.g. ``"D01"``).
        title: The decision title.
        rationale: The decision rationale body (rendered verbatim, with
            trailing whitespace stripped).
    """

    decision_id: str
    title: str
    rationale: str

    def render(self) -> str:
        """Return the decision sub-block (heading + blank line + rationale)."""
        return f"### {self.decision_id}: {self.title}\n\n{self.rationale.rstrip()}"


class SpecHypothesis(_SpecModel):
    """One in-scope hypothesis row in a :class:`SubagentSpec`.

    Attributes:
        hypothesis_id: The hypothesis id (e.g. ``"H01-01"``).
        metric: The hypothesis metric name.
        confirm: The confirm threshold prose.
        reject: The reject threshold prose.
        verdict: The verdict string, or ``None`` (rendered as ``open``).
    """

    hypothesis_id: str
    metric: str
    confirm: str
    reject: str
    verdict: str | None = None

    def render(self) -> str:
        """Return the four-line hypothesis block."""
        verdict = self.verdict if self.verdict is not None else "open"
        return (
            f"- {self.hypothesis_id}: metric={self.metric!r}\n"
            f"    confirm: {self.confirm}\n"
            f"    reject:  {self.reject}\n"
            f"    verdict: {verdict}"
        )


class SpecAudit(_SpecModel):
    """One recent-audit row in a :class:`SubagentSpec`.

    Attributes:
        audit_id: The audit id (e.g. ``"A01"``).
        kind: The audit kind string (e.g. ``"evaluation"``).
        verdict: The verdict string, or ``None`` (rendered as ``pending``).
    """

    audit_id: str
    kind: str
    verdict: str | None = None

    def render(self) -> str:
        """Return the single-line audit row."""
        verdict = self.verdict if self.verdict is not None else "pending"
        return f"- {self.audit_id}: {self.kind} verdict={verdict}"


class SpecWorktree(_SpecModel):
    """Worktree wire-up for a :class:`SubagentSpec`.

    Attributes:
        branch: The worktree branch name.
        path: The worktree checkout path.
        base_branch: The branch the worktree was cut from.
    """

    branch: str
    path: str
    base_branch: str


class SubagentSpec(_SpecModel):
    """Typed dispatch spec for one wave.

    The renderer builds this from ``State`` and calls :meth:`render` to
    produce the wave prompt. The field set mirrors the sections the legacy
    ad-hoc renderer emitted, in the same order.

    Attributes:
        wave_id: The dispatched wave id.
        iter_id: The wave's parent iter id (rendered into the scope
            rationale verbatim, matching the legacy ``wave.iter_id``).
        title: The wave title (rendered into the ``# Wave ...`` header).
        description: The wave's optional long-form purpose (the ≤500-char
            field split off the bounded ≤72-char ``title``). Rendered as a
            ``## Description`` section between the header and ``## Wave
            tags`` when set; ``None`` omits the section.
        scope_id: The resolved scope id (wave → iter → phase → scope).
        agent_role: The wave's ``agent_role`` value, or ``None``
            (rendered as ``unspecified``).
        effort_bucket: The wave's ``effort_bucket`` value, or ``None``
            (rendered as ``unspecified``).
        success_criteria: The wave success-criteria list (may be empty).
        file_scopes: The wave file-scope globs (may be empty → ``(none)``).
        dependencies: Blocking dependency rows (may be empty → ``None.``).
        decisions: In-scope decision sub-blocks (may be empty → ``None.``).
        hypotheses: In-scope hypothesis blocks (may be empty → ``None.``).
        recent_audits: Recent-audit rows (may be empty → ``None.``).
        references: Repo-relative spike-brief paths surfaced under
            ``## References``. Empty list → the section is omitted (the
            legacy renderer skipped it entirely when no brief matched).
        worktree: Worktree wire-up, or ``None`` (renders the
            ``Worktree path: inline`` fallback).
    """

    wave_id: str
    iter_id: str
    title: str
    description: str | None = None
    scope_id: str
    agent_role: str | None = None
    effort_bucket: str | None = None
    success_criteria: list[str] = Field(default_factory=list)
    file_scopes: list[str] = Field(default_factory=list)
    dependencies: list[SpecDependency] = Field(default_factory=list)
    decisions: list[SpecDecision] = Field(default_factory=list)
    hypotheses: list[SpecHypothesis] = Field(default_factory=list)
    recent_audits: list[SpecAudit] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    worktree: SpecWorktree | None = None

    # ---- Section renderers --------------------------------------------------

    def _render_header(self) -> str:
        return f"# Wave {self.wave_id}: {self.title}"

    def _render_description(self) -> str | None:
        if self.description is None:
            return None
        return f"## Description\n\n{self.description.rstrip()}"

    def _render_wave_tags(self) -> str:
        role = self.agent_role if self.agent_role else "unspecified"
        bucket = self.effort_bucket if self.effort_bucket else "unspecified"
        lines = ["## Wave tags", "", f"- agent_role: {role}", f"- effort_bucket: {bucket}"]
        if self.success_criteria:
            lines.append("- success_criteria:")
            lines.extend(f"  - {criterion}" for criterion in self.success_criteria)
        else:
            lines.append("- success_criteria: none")
        return "\n".join(lines)

    def _render_scope(self) -> str:
        files = ", ".join(self.file_scopes) if self.file_scopes else "(none)"
        rationale = (
            f"Scope is anchored on iter {self.iter_id} under scope {self.scope_id}. "
            f"Stay inside the listed file_scopes — any change outside this list "
            f"is out of scope for this wave."
        )
        return f"## Scope\n\n{files}\n\n{rationale}"

    def _render_dependencies(self) -> str:
        lines = ["## Dependencies", ""]
        if not self.dependencies:
            lines.append("None.")
            return "\n".join(lines)
        lines.extend(dep.render() for dep in self.dependencies)
        return "\n".join(lines)

    def _render_decisions(self) -> str:
        if not self.decisions:
            return "## Decisions\n\nNone."
        parts = ["## Decisions"]
        for decision in self.decisions:
            parts.append("")
            parts.append(decision.render())
        return "\n".join(parts)

    def _render_hypotheses(self) -> str:
        if not self.hypotheses:
            return "## Hypotheses\n\nNone."
        lines = ["## Hypotheses", ""]
        lines.extend(hyp.render() for hyp in self.hypotheses)
        return "\n".join(lines)

    def _render_recent_audits(self) -> str:
        if not self.recent_audits:
            return "## Recent audits\n\nNone."
        lines = ["## Recent audits", ""]
        lines.extend(audit.render() for audit in self.recent_audits)
        return "\n".join(lines)

    def _render_references(self) -> str | None:
        if not self.references:
            return None
        lines = ["## References", ""]
        lines.append(
            "Spike briefs whose filename references this wave / iter / phase. "
            "Read these before starting work — they capture the read-only "
            "investigation that motivated the wave's success criteria."
        )
        lines.append("")
        lines.extend(f"- {rel_path}" for rel_path in self.references)
        return "\n".join(lines)

    def _render_working_tree(self) -> str:
        lines = ["## Working tree", ""]
        if self.worktree is not None:
            lines.append(f"Branch: {self.worktree.branch}")
            lines.append(f"Worktree path: {self.worktree.path}")
            lines.append(f"Base commit: {self.worktree.base_branch}")
        else:
            lines.append("Worktree path: inline")
        return "\n".join(lines)

    def _render_workflow(self) -> str:
        phase_segment, wave_segment = _phase_wave_segments(self.wave_id)
        commit_prefix = f"[{phase_segment}-{wave_segment}]"
        return (
            "## Workflow\n"
            "\n"
            "1. cd into the wave's worktree (see `## Working tree` above).\n"
            "2. Implement edits in dependency order: schemas → logic → CLI → tests.\n"
            "3. Run the local gauntlet:\n"
            "   - `uv run pre-commit run --all-files`\n"
            "   - `uv run mypy src/`\n"
            "   - `uv run pytest tests/ -q`\n"
            "4. Commit with prefix `"
            + commit_prefix
            + " <type>: <summary>` (3-6 bullet body) and the\n"
            "   recognized Claude or Codex `Co-Authored-By` trailer."
        )

    def _render_out_of_scope(self) -> str:
        return (
            "## Out of scope\n"
            "\n"
            "- Do **not** push the branch.\n"
            "- Do **not** open a PR.\n"
            "- Do **not** edit `.ea/state.json` or `.ea/store/event.jsonl` "
            "directly — every mutation goes through `uv run eawf state ...`.\n"
            "- Never `git commit --no-verify`; root-cause the hook instead."
        )

    def render(self) -> str:
        """Return the full Markdown wave prompt.

        Sections render in the canonical order — header, description
        (omitted when unset), wave tags, scope, dependencies, decisions,
        hypotheses, recent audits, references (omitted when empty), working
        tree, workflow, out of scope — joined by a blank line. The trailing
        newline mirrors the legacy renderer so emitting the prompt verbatim
        stays byte-clean.

        Returns:
            The wave prompt as a single string ending in ``"\\n"``.
        """
        sections: list[str] = [self._render_header()]
        description = self._render_description()
        if description is not None:
            sections.append(description)
        sections.extend(
            [
                self._render_wave_tags(),
                self._render_scope(),
                self._render_dependencies(),
                self._render_decisions(),
                self._render_hypotheses(),
                self._render_recent_audits(),
            ]
        )
        references = self._render_references()
        if references is not None:
            sections.append(references)
        sections.append(self._render_working_tree())
        sections.append(self._render_workflow())
        sections.append(self._render_out_of_scope())
        return "\n\n".join(sections).rstrip() + "\n"


def _phase_wave_segments(wave_id: str) -> tuple[str, str]:
    """Split ``Pxx-Iyy-Wzz`` into ``("Pxx", "Wzz")`` for the commit prefix.

    Args:
        wave_id: The wave id. The state model's ``WaveIdStr`` regex
            already enforces three segments; the split stays defensive so
            a hand-crafted spec never crashes the renderer.

    Returns:
        A ``(phase_segment, wave_segment)`` tuple. A malformed id (fewer
        than three ``-``-joined parts) yields ``(wave_id, "WXX")``.
    """
    parts = wave_id.split("-")
    if len(parts) < 3:
        return wave_id, "WXX"
    return parts[0], parts[2]


__all__ = [
    "SpecAudit",
    "SpecDecision",
    "SpecDependency",
    "SpecHypothesis",
    "SpecWorktree",
    "SubagentSpec",
]
