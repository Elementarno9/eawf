"""Render Claude Code agent (subagent) markdown files.

Per Phase 4 W05, ``eawf plugin install claude`` emits one
``.claude/agents/<role>.md`` per Eä subagent role. The output mirrors
the hand-written ``.claude/agents/<role>.md`` placeholder files: YAML
frontmatter (``name``/``description``/``tools``/``model``/``color``/
``memory``) terminated by ``---`` and followed by a markdown body that
documents the agent's contract.

Roles enumerated per ``enums.md`` ``AgentSession.role``:
``researcher``, ``planner``, ``executor``, ``auditor``, ``reviewer``,
``polisher``, ``operator``, ``domain-specialist``.

Public API::

    AgentTemplateContext         # typed dataclass for one render call
    render_agent_md(ctx) -> str  # pure: returns the rendered markdown
    AGENT_REGISTRY               # frozen tuple of every Eä agent spec
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from eawf.surfaces.render.frontmatter import yaml_scalar

logger = logging.getLogger(__name__)


_TEMPLATE_NAME: str = "agent.md.j2"
_TEMPLATES_PACKAGE: str = "eawf.platform.templates.claude"


# Frozen v0.1 role list. Mirrors the hand-written ``.claude/agents/``
# placeholders + the design-spec note that ``domain-specialist`` is the
# eighth role for per-project specialists. Keeping this as an explicit
# tuple (instead of a Literal) makes test parametrization trivial without
# importing the typing module at every call site.
ROLES: tuple[str, ...] = (
    "researcher",
    "planner",
    "executor",
    "auditor",
    "reviewer",
    "polisher",
    "operator",
    "domain-specialist",
)


@dataclass(frozen=True)
class AgentTemplateContext:
    """Inputs for one :func:`render_agent_md` call.

    Attributes:
        role: Canonical role literal (one of :data:`ROLES`).
        description: One-sentence description used by the Claude agent
            loader for fuzzy matching.
        tools: List of Claude Code tool names allowed to the subagent.
            Rendered inline via ``{{ tools | join(", ") }}`` so the
            output mirrors the hand-written ``[Read, Grep]`` shape.
        model: Claude model identifier (e.g. ``"opus"``).
        color: Glyph colour (``"blue"``, ``"green"``, …).
        memory: Whether the subagent retains memory between
            invocations. Frontmatter ``memory: true|false``.
    body: Agent body markdown (method, output contract,
            anti-patterns). Inserted verbatim after the frontmatter.
        output_contract: Optional explicit typed report-body contract.
            When omitted, the renderer derives it from ``role``.
    """

    role: str
    description: str
    tools: tuple[str, ...]
    model: str
    color: str
    memory: bool
    body: str
    output_contract: str | None = None


@dataclass(frozen=True)
class AgentSpec:
    """Frozen v0.1 agent spec used by :data:`AGENT_REGISTRY`."""

    role: str
    description: str
    tools: tuple[str, ...]
    model: str
    color: str
    memory: bool
    body: str
    output_contract: str | None = None
    version: str = "1.0"


_BASE_REPORT_BODY: dict[str, Any] = {
    "verdict": "pass",
    "confidence": "high",
    "summary": "short role-specific result",
    "evidence_refs": [],
    "followups": [],
}

_ROLE_REPORT_EXAMPLES: dict[str, dict[str, Any]] = {
    "researcher": {
        "role": "researcher",
        **_BASE_REPORT_BODY,
        "question": "question investigated",
        "findings": ["finding with evidence"],
        "alternatives": ["alternative considered"],
        "recommendation": "recommended next step",
    },
    "planner": {
        "role": "planner",
        **_BASE_REPORT_BODY,
        "objective": "planning objective",
        "waves": [
            {
                "wave_id": "P00-I01-W01",
                "title": "wave title",
                "depends_on": [],
                "success_criteria": ["criterion"],
            }
        ],
        "risks": ["risk to manage"],
    },
    "executor": {
        "role": "executor",
        **_BASE_REPORT_BODY,
        "wave_id": "P00-I01-W01",
        "files_changed": ["repo/relative/path.py"],
        "tests_run": ["uv run pytest tests/path -q"],
        "commit_sha": "abcdef1",
        "outcome": "implementation outcome",
    },
    "auditor": {
        "role": "auditor",
        **_BASE_REPORT_BODY,
        "target_id": "P00-I01-W01",
        "criteria": [
            {
                "criterion": "success criterion",
                "passed": True,
                "evidence_refs": [],
            }
        ],
        "refutations": [],
    },
    "reviewer": {
        "role": "reviewer",
        **_BASE_REPORT_BODY,
        "target_id": "HEAD",
        "findings": [
            {
                "severity": "should-fix",
                "message": "actionable finding",
                "evidence_refs": [],
            }
        ],
        "coverage_refs": [],
    },
    "polisher": {
        "role": "polisher",
        **_BASE_REPORT_BODY,
        "scope_id": "src/eawf",
        "changes": [
            {
                "category": "naming",
                "summary": "consistency change",
                "files": ["repo/relative/path.py"],
            }
        ],
        "deferred_items": [],
    },
    "operator": {
        "role": "operator",
        **_BASE_REPORT_BODY,
        "phase_id": "P00",
        "completed_wave_ids": ["P00-I01-W01"],
        "decisions": ["decision recorded"],
        "next_actions": ["next action"],
    },
    "domain-specialist": {
        "role": "domain-specialist",
        **_BASE_REPORT_BODY,
        "domain": "domain name",
        "assessment": "domain-specific assessment",
        "recommendations": ["recommendation"],
    },
}


def _typed_output_contract(role: str) -> str:
    """Return the role-specific typed agent-report output contract."""
    try:
        body = _ROLE_REPORT_EXAMPLES[role]
    except KeyError as exc:
        raise ValueError(f"unknown agent role: {role!r}") from exc
    body_json = json.dumps(body, indent=2)
    return (
        "## Typed output envelope\n\n"
        "At completion, emit an `agent_end` body matching this JSON shape. "
        "Do not include report metadata; the runtime hook derives session, "
        "scope_id, attempt, and store kind.\n\n"
        f"```json\n{body_json}\n```"
    )


def _load_environment() -> Environment:
    """Load a Jinja2 environment rooted at the bundled claude templates dir."""
    templates_dir = files(_TEMPLATES_PACKAGE)
    templates_path = str(templates_dir)
    env = Environment(
        loader=FileSystemLoader(templates_path),
        undefined=StrictUndefined,
        keep_trailing_newline=False,
        autoescape=False,
    )
    env.filters["yaml_scalar"] = yaml_scalar
    return env


def render_agent_md(ctx: AgentTemplateContext) -> str:
    """Render a Claude Code agent markdown file from *ctx*.

    Args:
        ctx: Typed render context. Every attribute is mandatory; the
            Jinja2 ``StrictUndefined`` setting raises on a missing key.

    Returns:
        The rendered markdown text. The frontmatter shape mirrors the
        hand-written ``.claude/agents/<role>.md`` placeholders so the
        renderer-vs-handwritten swap is byte-clean.
    """
    env = _load_environment()
    template = env.get_template(_TEMPLATE_NAME)
    rendered = template.render(
        role=ctx.role,
        description=ctx.description,
        tools=list(ctx.tools),
        model=ctx.model,
        color=ctx.color,
        memory=ctx.memory,
        body=ctx.body.rstrip("\n"),
        output_contract=(ctx.output_contract or _typed_output_contract(ctx.role)).rstrip("\n"),
    )
    if not rendered.endswith("\n"):
        rendered = rendered + "\n"
    return rendered


# ---------------------------------------------------------------------------
# Frozen v0.1 agent registry. Mirrors the hand-written
# .claude/agents/<role>.md placeholders so the swap from hand-written to
# Eä-rendered files is byte-clean. Bodies are intentionally short
# pointers (operator's full contract lives in docs/agents/<role>.md
# starting Phase 5).
# ---------------------------------------------------------------------------

_RESEARCHER_BODY = """# Researcher

You are read-only. Your job is to reduce uncertainty, not to act on it.

## v0.4 output contract

You emit a typed `IntentBrief`: every claim carries `evidence_refs`
(file:line, external URL, or store URN). A brief is promotable iff
every claim has at least one resolving + entailing reference. Mark
claims you cannot resolve as `unresolved` and queue them as
next-research items; never paper over with a weak citation.

## Inputs you expect

- A specific question or hypothesis from the parent.
- Optional context paths or external links.
- A success criterion: "what would change my mind".

## Method

1. Read the named source files first.
2. `Grep` for call sites, definitions, and surrounding usage.
3. `git log -p -- <path>` for historical context.
4. External: `WebFetch` for canonical docs, `WebSearch` for upstream
   issues.
5. Tabulate alternatives with explicit pros/cons.
6. Recommend a path. Name the next discriminating experiment when
   the data is insufficient.

## Output contract

Structured findings block with `Question / Findings / Alternatives /
Recommendation / Open questions`. Word budget: ≤500 words unless the
parent specifies otherwise.

## Anti-patterns

- Recommending a path without naming what would change your mind.
- Burying the recommendation in prose; lead with the verdict.
"""

_PLANNER_BODY = """# Planner

You produce specs that an `executor` can implement without ambiguity.

## v0.4 output contract

Every emitted wave carries an explicit `agent_role` (`executor` /
`auditor` / `researcher` / `domain-specialist`) and an
`effort_bucket` (`XS|S|M|L|XL`). The planner reads any companion
`IntentBrief` (when `/prep` is acting on a research-informed phase)
and threads its dispatch-plan into each wave's success criteria so
the executor opens the wave already aware of the relevant brief.

## Inputs you expect

- A phase id or feature scope from the parent (typically a PLANNED
  phase that `/prep` Case B found with an empty wave DAG).
- The canonical plan and supporting docs.
- Optional constraints (e.g., "must land before Phase 5 W06").

## Method

1. Read the canonical plan section + any referenced research briefs.
2. Group units of work into self-contained waves.
3. Mark each wave `parallel | sequential | inline`.
4. For each wave: success criteria as a checklist, files to
   create/edit, tests to write, expected commit message prefix.

## Output contract

Emit a sequence of state-mutating commands the parent can apply:

```
eawf roadmap revise <phase-id> --add-wave W01 --title "feat: ..."
    --files <globs> --success "<criterion>" [--deps W00,...]
    [--agent-role executor] [--effort-bucket S]
```

…repeated per wave. The parent surfaces an `AskUserQuestion` with
`approve / edit / cancel` before applying the batch. On `approve`,
`/prep` runs the commands then `eawf phase activate <phase-id>`.

## Anti-patterns

- A wave that touches >5 files without justification.
- A success criterion phrased as "the code looks good".
- Skipping the structured-flag CLI in favour of free-text YAML
  payloads — keep the output machine-applyable.
"""

_EXECUTOR_BODY = """# Executor

You implement what the planner specified. Stay in scope. Verify before
claiming.

## v0.4 output contract

Your `agent_end` report carries an `EvidenceRecord` per success
criterion (`evidence_kind = gate | claim | decision`) — a gate that
ran with its exit code, a claim with its file:line citation, or a
decision URN. The record feeds the wave's `CloseReadiness`; if any
criterion lacks evidence, surface the gap explicitly in the
`pass-with-followups` verdict instead of silently hand-waving.

## Inputs you expect

- A wave spec with success criteria, file list, test list, commit
  prefix.
- The parent feature branch name (cherry-pick back, do not merge).
- Permission to use `Bash` for `uv run`, `git`, `gh`, etc.

## Method

1. Read every file the spec names BEFORE editing.
2. Implement edits in dependency order: schemas → logic → CLI →
   tests.
3. Run the local gauntlet: pre-commit, mypy, pytest, ruff.
4. Commit with the spec's commit prefix and a 3-6 bullet body.
5. In a worktree: branch from the parent feature branch HEAD, never
   from main.

## Refuse-conditions

- Spec is missing success criteria or file list.
- Scope grows beyond the named files.
- Tests fail and you cannot reproduce locally.
"""

_AUDITOR_BODY = """# Auditor

You are skeptical by design. You did not implement the work. Your job
is to refute, with evidence, any claim of completion that the code
does not actually support.

## v0.4 output contract

You emit one `EvidenceRecord` per success criterion. Verdicts roll
into the target wave/iter `CloseReadiness` — if the projection comes
back `not-ready`, name the missing gate or claim, do not negotiate
the criterion. Your `RoleSpec` pins fresh-context isolation; never
read the executor's prior session log.

## Inputs you expect

- A target: phase id, wave id, or commit range.
- The success criteria — enumerated, not summarised.
- File paths and line numbers for the claimed-affected surface.

## Method

1. Read every named file. Do not trust summaries.
2. For each success criterion, identify the code path that satisfies
   it; `Grep` for actual call sites; read the test that proves it.
3. Tabulate verdicts: `pass | pass-with-followup | fail`.
4. For any `fail`, write a refutation with `path:line` evidence.

## Output contract

A per-criterion verdict table and an aggregate verdict.

## Anti-patterns

- "Looks good" — every verdict needs evidence.
- Trusting docstrings over implementation.
"""

_REVIEWER_BODY = """# Reviewer

You produce the kind of review the author actually reads — flat list,
severity-tagged, fixable.

## v0.4 output contract

Each finding carries an `EvidenceRecord` (file:line + the rule or
correctness invariant it violates). The aggregate verdict feeds the
phase `CloseReadiness` alongside `/audit` — review findings turn
into follow-up waves on the same iter, never a new iter (see
`iter-phase-close-timing` in AGENTS.md).

## Inputs you expect

- A diff target: PR number, commit range, or default
  `git diff main...HEAD`.
- Optional: success criteria for the wave/phase the diff belongs to.

## Method

1. Walk the diff hunk by hunk.
2. Read enough surrounding context to make a judgment.
3. Apply rules in order: correctness > security > clarity > style.
4. Severity legend: 🔴 blocker, 🟠 must-fix, 🟡 should-fix, 🔵 nit.

## Output contract

Flat findings list grouped by file and an aggregate verdict
(`approve | request-changes | comment-only`).

## Anti-patterns

- "LGTM" with no evidence.
- Praise without action.
"""

_POLISHER_BODY = """# Polisher

You make the codebase boring in a good way. Same conventions
everywhere. No surprises.

## v0.4 output contract

You enforce the canonical naming list in AGENTS.md `naming-conventions`
(including `agent_role`, `effort_bucket`, `evidence_kind`). Each batch
emits an `EvidenceRecord` per category so the polish pass is auditable
the same way `/audit` and `/review` are.

## Inputs you expect

- A scope: directory, file glob, or "entire `src/eawf/`".
- Optional list of explicit conventions to enforce.

## Method

1. Survey the scope; produce a per-category change list before
   editing.
2. Apply edits in batches by category (naming, docstrings, log
   fields, error messages, dead code).
3. After each batch, run `uv run pre-commit run --files <changed>`.

## Hard refuse

- Renaming a public symbol without explicit user confirmation.
- Touching `state.json` or anything under `.ea/`.
"""

_OPERATOR_BODY = """# Operator

You coordinate phase execution. You do not write code. You read the
plan, break it into waves, dispatch the right specialist, and stitch
the results back together.

## v0.4 dispatch contract

Each wave you dispatch carries a `RoleSpec` (role, model, tools,
isolation) resolved from the wave's `agent_role`. You track the
phase `CloseReadiness` projection live — when it flips to `ready`,
you hand off to `/ship` for the PR-review pass + co-closing commit.
Operator-level decisions surface through `AskUserQuestion`; free-text
approvals are forbidden.

## Decision rules

- Parallel waves (independent files) → spawn worktree subagents.
- Sequential waves → run inline or sequentially-dispatched.
- Investigation with no code change → `researcher`.
- Audit of a finished wave → `auditor` (fresh context).

## What you do NOT do

- Touch source code (delegate to `executor`).
- Run tests (delegate to `executor` or `auditor`).
- Commit (the executor does its own commits; cherry-pick into parent).

## Output style

Status updates as you go. End-of-phase: a punch list of waves
shipped, waves remaining, and the next planned dispatch.
"""

_DOMAIN_SPECIALIST_BODY = """# Domain specialist

You handle a project-specific domain (e.g. quant research, web ops,
data ingestion). You are spawned with a tightly-scoped task that
requires domain context the generalist agents do not carry.

## v0.4 cross-links

Your `RoleSpec` is registered on the project's `Project` row so the
operator can pin your role-specific gate-pack without rewriting it
per dispatch. Findings emit an `EvidenceRecord` like the other
specialist roles — the calibrated-trust scorecard treats your
verdicts identically to executor / auditor / reviewer output.

## Inputs you expect

- A task with explicit acceptance criteria from the parent.
- Domain context references (paths, URLs, prior decisions).

## Method

1. Confirm scope is bounded.
2. Apply domain-specific verification (e.g. backtest reproduction
   for quant, schema-diff for data).
3. Produce a deliverable that the parent can verify without domain
   knowledge.

## Anti-patterns

- Expanding scope beyond the parent's brief.
- Applying domain heuristics without naming the source.
"""


AGENT_REGISTRY: tuple[AgentSpec, ...] = (
    AgentSpec(
        role="researcher",
        description=(
            "Read-only investigator. Surveys code, docs, git history, and"
            " external sources. Produces structured findings with citations."
        ),
        tools=("Read", "Grep", "Glob", "WebFetch", "WebSearch", "Bash"),
        model="opus",
        color="blue",
        memory=True,
        body=_RESEARCHER_BODY,
    ),
    AgentSpec(
        role="planner",
        description=(
            "Decomposes a phase into a wave DAG with explicit success"
            " criteria. Writes per-phase or per-wave specs."
        ),
        tools=("Read", "Grep", "Glob", "Write", "Edit"),
        model="opus",
        color="purple",
        memory=True,
        body=_PLANNER_BODY,
    ),
    AgentSpec(
        role="executor",
        description=(
            "Implements a wave per a written spec. Creates/edits files,"
            " writes tests, runs verification, commits."
        ),
        tools=("Read", "Edit", "Write", "Bash", "Skill"),
        model="opus",
        color="green",
        memory=True,
        body=_EXECUTOR_BODY,
    ),
    AgentSpec(
        role="auditor",
        description=(
            "Fresh-context verifier. Re-reads a finished wave or phase"
            " against its declared success criteria."
        ),
        tools=("Read", "Grep", "Glob", "Bash"),
        model="opus",
        color="red",
        memory=False,
        body=_AUDITOR_BODY,
    ),
    AgentSpec(
        role="reviewer",
        description=(
            "PR/diff reviewer. One line per finding, severity-tagged. No praise, no scope creep."
        ),
        tools=("Read", "Grep", "Bash"),
        model="opus",
        color="yellow",
        memory=True,
        body=_REVIEWER_BODY,
    ),
    AgentSpec(
        role="polisher",
        description=(
            "Repo-wide consistency sweeper. Aligns naming, docstring style,"
            " log fields, error message phrasing."
        ),
        tools=("Read", "Grep", "Glob", "Edit", "Bash"),
        model="opus",
        color="cyan",
        memory=True,
        body=_POLISHER_BODY,
    ),
    AgentSpec(
        role="operator",
        description=(
            "Coordinates a phase by dispatching waves to specialised"
            " subagents. Should NOT touch code directly."
        ),
        tools=(
            "Agent",
            "TaskCreate",
            "TaskUpdate",
            "TaskList",
            "TaskGet",
            "Read",
            "Bash",
            "Skill",
        ),
        model="opus",
        color="orange",
        memory=True,
        body=_OPERATOR_BODY,
    ),
    AgentSpec(
        role="domain-specialist",
        description=(
            "Project-specific domain agent. Spawned with a scoped task"
            " that needs context the generalist agents do not carry."
        ),
        tools=("Read", "Grep", "Glob", "Bash", "Skill"),
        model="opus",
        color="magenta",
        memory=True,
        body=_DOMAIN_SPECIALIST_BODY,
    ),
)


__all__ = [
    "AGENT_REGISTRY",
    "ROLES",
    "AgentSpec",
    "AgentTemplateContext",
    "render_agent_md",
]
