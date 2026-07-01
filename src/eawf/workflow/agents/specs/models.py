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
in :mod:`eawf.workflow.dispatch.renderer` (it needs the wave → iter → phase →
scope walk); the model layer stays pure data + formatting.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RoleTierBudgetError(ValueError):
    """A role-tier dispatch block exceeds its token budget.

    Raised at the role-block injection point (FLEET-6 / P30-I06-W06) when a
    per-role "Zone 3" block body's
    :func:`~eawf.platform.lint.tools.agents_md_budget.count_tokens` weight
    exceeds the configured role-tier cap. The role zone honours a budget the
    same way the AGENTS.md tier-0 zone does — the block is rejected by RAISING,
    never silently truncated, so an over-cap block fails fast at render time
    rather than shipping a clipped system prompt.

    Subclasses :class:`ValueError` so callers that already catch the
    renderer's :class:`ValueError` surface (e.g. an unknown runtime) treat a
    budget breach the same way.
    """


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


class SpecEstimate(_SpecModel):
    """Estimate hints surfaced in a :class:`SubagentSpec`.

    Attributes:
        effort_bucket: The wave's effort bucket, or ``None`` when
            unclassified.
        expected_eu: Latest expected effort-unit estimate, or ``None``
            when no estimate has been recorded yet.
        expected_minutes: Latest expected minutes estimate, or ``None``
            when no estimate has been recorded yet.
        token_budget: Token budget attached to the wave, or ``None``
            when unset.
        parallel_siblings: Other active waves in the same iter. Empty
            renders as ``none`` so subagents can distinguish solo waves
            from sibling-dispatch waves without reading state.
    """

    effort_bucket: str | None = None
    expected_eu: float | None = None
    expected_minutes: float | None = None
    token_budget: int | None = None
    parallel_siblings: list[str] = Field(default_factory=list)

    def render(self) -> str:
        """Return the ``## Estimate`` section."""
        siblings = ", ".join(self.parallel_siblings) if self.parallel_siblings else "none"
        return (
            "## Estimate\n"
            "\n"
            f"- bucket: {_display_value(self.effort_bucket)}\n"
            f"- expected_eu: {_display_value(self.expected_eu)}\n"
            f"- expected_minutes: {_display_value(self.expected_minutes)}\n"
            f"- token_budget: {_display_value(self.token_budget)}\n"
            f"- parallel_siblings: {siblings}"
        )


class RoleContract(_SpecModel):
    """Typed projection of role-level invariants for one dispatch.

    A :class:`RoleContract` is the keystone projection that lets every
    per-role plugin surface (Claude / Codex / OpenCode / dispatch
    :class:`SubagentSpec`) share one canonical source of role data
    (P28-I01-W12). The dispatch
    :func:`~eawf.workflow.dispatch.renderer.build_role_contract`
    helper reads a :class:`~eawf.workflow.agents.specs.roles.RoleSpec`
    and emits a :class:`RoleContract`; :class:`SubagentSpec` carries the
    projection so the role's ``system_prompt`` (and tool / model wiring)
    are driven by the registry rather than by hardcoded constants.

    The contract is a *projection* — it does not re-author role data; it
    copies the canonical fields off :class:`RoleSpec` so downstream
    surfaces consume one typed shape instead of re-walking the registry.

    Attributes:
        role: The role identifier (e.g. ``"executor"``). Bare ``role``
            here because the surrounding :class:`RoleContract` type
            already disambiguates per AGENTS naming rule 17.
        summary: One-sentence role description (mirrors
            :attr:`RoleSpec.summary`).
        system_prompt: The role contract Markdown — method, output
            contract, anti-patterns — rendered as the
            ``## Role contract`` section of the dispatch prompt.
        allowed_tools: Tools the role may invoke at dispatch time. The
            dispatcher feeds this into per-runtime tool grants
            (``settings.json`` for claude-code, ``allowed_tools`` on the
            SDK envelope, etc.).
        denied_tools: Tools the role MUST NOT invoke. Wave-scoped
            sandbox policies intersect into this list at envelope
            projection time via
            :func:`~eawf.runtime.sandbox.policy.resolve_denied_tools`.
        model: Preferred model identifier, or ``None`` to inherit the
            dispatcher's default.
        memory: Whether the role retains memory across invocations.
        report_schema_ref: Typed-report store-kind reference for the
            role's ``agent_end`` reports.
        stop_conditions: Role-specific stop conditions surfaced under
            the ``## Stop conditions`` section. Empty when the role has
            no role-specific stop conditions beyond the defaults.
    """

    role: str
    summary: str
    system_prompt: str
    allowed_tools: list[str] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=list)
    model: str | None = None
    memory: bool = False
    report_schema_ref: str = Field(min_length=1)
    stop_conditions: list[str] = Field(default_factory=list)


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
        estimate: Typed estimate hints rendered after ``## Out of
            scope``. Defaults to unknown values so all prompts carry the
            section even when no estimate has been seeded.
        role_contract: Typed projection of the dispatched wave's role
            (P28-I01-W12). When set, the spec renders a
            ``## Role contract`` section carrying the role's
            ``system_prompt`` plus role-driven tool / model wiring; a
            ``## Stop conditions`` section follows when the role lists
            any. ``None`` keeps the dispatch byte-equivalent to the
            pre-W12 ad-hoc renderer so callers that have not yet
            plumbed the role registry through stay unchanged.
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
    estimate: SpecEstimate = Field(default_factory=SpecEstimate)
    role_contract: RoleContract | None = None

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

    def _render_workflow(self, *, headless: bool = False) -> str:
        commit_prefix = _commit_prefix_for_wave(self.wave_id, self.iter_id)
        # The headless (daemon live-spawn) path closes the wave on the agent's
        # behalf: the daemon reads the emitted report body and drives the close
        # (DL-5). A sandboxed agent cannot self-close anyway — its jailed
        # `uv run eawf` resolves whatever eawf is installed, not the daemon's,
        # and a self-close attempt only risks a spurious blocked verdict. So the
        # headless render tells the agent to emit its report and stop, while the
        # interactive render keeps the operator-run self-close step.
        if headless:
            close_step = (
                "5. Do **not** run `eawf wave close` yourself. Emit your final "
                "report as your last message; the daemon binds it and closes "
                "this wave on your behalf once the report is recorded."
            )
        else:
            close_step = (
                "5. Close the wave through the CLI with the final token tally:\n"
                "   - `uv run eawf wave close "
                + self.wave_id
                + ' --outcome "<summary>" --tokens-consumed <tokens>`'
            )
        return (
            "## Workflow\n"
            "\n"
            "1. cd into the wave's worktree (see `## Working tree` above).\n"
            "2. Implement edits in dependency order: schemas → logic → CLI → tests.\n"
            "3. Run the local gauntlet:\n"
            "   - `uv run pre-commit run --all-files`\n"
            "   - `uv run mypy src/`\n"
            "   - `uv run pytest tests/ -q`\n"
            f"4. Commit with prefix `{commit_prefix} <type>: <summary>` "
            "(3-6 bullet body) and the recognized Claude or Codex "
            "`Co-Authored-By` trailer.\n" + close_step
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

    def _render_role_contract(self) -> str | None:
        """Return the ``## Role contract`` section, or ``None`` when absent.

        Renders the role's identity, model, tool grants, memory setting,
        report schema reference, and ``system_prompt`` body when
        :attr:`role_contract` is set. ``None`` (the default) omits the
        section, keeping the dispatch prompt byte-equivalent to the
        pre-W12 renderer for callers that have not yet plumbed the role
        registry through.
        """
        if self.role_contract is None:
            return None
        contract = self.role_contract
        body = _unwrap_markdown_soft_wraps(self.role_contract.system_prompt).rstrip("\n")
        allowed_tools = _display_list(contract.allowed_tools)
        denied_tools = _display_list(contract.denied_tools)
        model = _display_value(contract.model)
        memory = "true" if contract.memory else "false"
        return (
            "## Role contract\n"
            "\n"
            f"- role: {contract.role}\n"
            f"- summary: {contract.summary}\n"
            f"- model: {model}\n"
            f"- memory: {memory}\n"
            f"- report_schema_ref: {contract.report_schema_ref}\n"
            f"- allowed_tools: {allowed_tools}\n"
            f"- denied_tools: {denied_tools}\n"
            "\n"
            "### System prompt\n"
            "\n"
            f"{body}"
        )

    def _render_report_output(self) -> str | None:
        """Return the headless ``## Report output`` section, or ``None``.

        Only the HEADLESS executor dispatch path emits this section. The
        live-spawn daemon path reads the spawned model's final message as a
        JSON ``ExecutorReportBody`` (``json.loads(spawn.text)`` then schema
        validation), so a model that answers in prose fails validation. The
        section pins the exact JSON schema -- field names, enum values, and
        the dispatched wave id -- plus a strict "output ONLY this JSON"
        instruction so the model emits a valid body on the first try.

        Returns ``None`` for any non-executor role since only the executor
        report body drives the live-spawn parse; the caller already gates
        the whole section on the ``headless`` flag, so the interactive
        render stays byte-equivalent.
        """
        if self.agent_role != "executor":
            return None
        schema = (
            '{"role": "executor", '
            '"verdict": "pass|pass-with-followups|fail|blocked", '
            '"confidence": "high|medium|low", '
            '"summary": "<=4000 chars", '
            f'"wave_id": "{self.wave_id}", '
            '"outcome": "1-1000 chars", '
            '"files_changed": [], '
            '"tests_run": [], '
            '"commit_sha": null, '
            '"evidence_refs": [], '
            '"followups": []}'
        )
        return (
            "## Report output\n"
            "\n"
            "Emit your final message as a single JSON object matching this "
            "schema and nothing else -- no prose, no markdown, no code "
            "fences:\n"
            "\n"
            f"{schema}\n"
            "\n"
            f"Set `wave_id` to `{self.wave_id}` exactly. `verdict` is one of "
            "`pass`, `pass-with-followups`, `fail`, `blocked`; `confidence` "
            "is one of `high`, `medium`, `low`. `summary` and `outcome` are "
            "required non-empty strings. `files_changed` and `tests_run` are "
            "arrays of plain strings. Leave `evidence_refs` and `followups` as "
            "empty arrays `[]` -- do not add entries to them."
        )

    def _render_stop_conditions(self) -> str | None:
        """Return the ``## Stop conditions`` section, or ``None`` when absent.

        Renders one bullet per :attr:`RoleContract.stop_conditions`
        entry. ``None`` keeps the prompt byte-equivalent when the role
        has no role-specific stop conditions (the wave's default
        ``## Out of scope`` block already names the dispatch-wide stop
        rules).
        """
        if self.role_contract is None or not self.role_contract.stop_conditions:
            return None
        lines = ["## Stop conditions", ""]
        lines.extend(f"- {condition}" for condition in self.role_contract.stop_conditions)
        return "\n".join(lines)

    def render(self, *, headless: bool = False) -> str:
        """Return the full Markdown wave prompt.

        Sections render in the canonical order — header, description
        (omitted when unset), wave tags, scope, dependencies, decisions,
        hypotheses, recent audits, references (omitted when empty), working
        tree, workflow, out of scope, estimate, stop conditions (omitted
        when empty) — joined by a blank line. When *headless* and the wave's
        ``agent_role`` is ``executor``, a trailing ``## Report output``
        section pins the ``ExecutorReportBody`` JSON schema so the spawned
        model emits a parseable report body (the live-spawn path
        ``json.loads``-es the final message and schema-validates it). The
        ``## Workflow`` close step also differs: the headless render tells the
        agent NOT to self-close (the daemon closes the wave on its behalf once
        the report binds), while the interactive render keeps the operator-run
        ``uv run eawf wave close`` step.

        Args:
            headless: ``True`` for the live-spawn (daemon) dispatch path,
                whose downstream reads the spawned model's final message as a
                JSON report body. ``False`` (default) is the interactive
                render an operator-facing Claude Code session sees; it stays
                byte-equivalent to the pre-W43 prompt.

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
        role_contract = self._render_role_contract()
        if role_contract is not None:
            sections.append(role_contract)
        sections.append(self._render_workflow(headless=headless))
        sections.append(self._render_out_of_scope())
        sections.append(self.estimate.render())
        stop_conditions = self._render_stop_conditions()
        if stop_conditions is not None:
            sections.append(stop_conditions)
        if headless:
            report_output = self._render_report_output()
            if report_output is not None:
                sections.append(report_output)
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


def _commit_prefix_for_wave(wave_id: str, iter_id: str) -> str:
    """Return the commit prefix bracket for *wave_id* under *iter_id*.

    Implements the AGENTS ``commit-prefix`` rule: iter ``I01`` drops the
    iter segment to read ``[P<NN>-W<NN>]``; iter ``I02`` and later carry
    the iter segment as ``[P<NN>-I<NN>-W<NN>]`` so the bracket attribution
    is unambiguous once a phase opens its second iter.

    The helper reads :attr:`SubagentSpec.iter_id` rather than re-splitting
    the wave id because the wave id alone cannot disambiguate
    ``P10-I02-W03`` from a (hand-crafted) ``P10-X-W03`` whose iter is
    actually I04 — the iter id is the canonical source.

    Args:
        wave_id: The wave id. A malformed id (fewer than three
            ``-``-joined parts) falls back to the bare wave id.
        iter_id: The wave's parent iter id (``P<NN>-I<NN>``). A
            malformed id falls back to ``I01`` semantics so the prefix
            stays valid against the lint.

    Returns:
        The bracketed commit prefix, e.g. ``"[P28-W57]"`` for an
        I01 wave or ``"[P28-I03-W57]"`` for an I03 wave.
    """
    phase_segment, wave_segment = _phase_wave_segments(wave_id)
    iter_segment = _iter_segment(iter_id)
    if iter_segment is None or iter_segment == "I01":
        return f"[{phase_segment}-{wave_segment}]"
    return f"[{phase_segment}-{iter_segment}-{wave_segment}]"


def _iter_segment(iter_id: str) -> str | None:
    """Return the ``I<NN>`` segment of *iter_id*, or ``None`` when malformed."""
    parts = iter_id.split("-")
    if len(parts) < 2:
        return None
    return parts[1]


def _display_value(value: object | None) -> str:
    """Render absent estimate fields with the stable ``unknown`` sentinel."""
    if value is None:
        return "unknown"
    return str(value)


def _display_list(values: list[str]) -> str:
    """Render an empty string list with the stable ``none`` sentinel."""
    if not values:
        return "none"
    return ", ".join(values)


def _unwrap_markdown_soft_wraps(markdown: str) -> str:
    """Collapse soft-wrapped Markdown lines while preserving block boundaries."""
    rendered: list[str] = []
    paragraph: str | None = None
    in_fenced_block = False

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph is not None:
            rendered.append(paragraph.rstrip())
            paragraph = None

    for line in markdown.rstrip("\n").splitlines():
        stripped = line.strip()
        starts_fence = stripped.startswith("```") or stripped.startswith("~~~")

        if in_fenced_block:
            rendered.append(line.rstrip())
            if starts_fence:
                in_fenced_block = False
            continue

        if starts_fence:
            flush_paragraph()
            rendered.append(line.rstrip())
            in_fenced_block = True
            continue

        if not stripped:
            flush_paragraph()
            rendered.append("")
            continue

        if paragraph is None:
            paragraph = line.rstrip()
            continue

        if _starts_markdown_block(line):
            flush_paragraph()
            paragraph = line.rstrip()
            continue

        paragraph = f"{paragraph.rstrip()} {stripped}"

    flush_paragraph()
    return "\n".join(rendered)


def _starts_markdown_block(line: str) -> bool:
    """Return ``True`` when ``line`` starts a Markdown block."""
    stripped = line.lstrip()
    if line.startswith("    "):
        return True
    if stripped.startswith(("#", "> ", "- ", "* ", "+ ", "|")):
        return True
    if stripped in {"---", "***", "___"}:
        return True
    prefix, separator, _ = stripped.partition(". ")
    return bool(separator) and prefix.isdecimal()


__all__ = [
    "RoleContract",
    "RoleTierBudgetError",
    "SpecAudit",
    "SpecDecision",
    "SpecDependency",
    "SpecEstimate",
    "SpecHypothesis",
    "SpecWorktree",
    "SubagentSpec",
]
