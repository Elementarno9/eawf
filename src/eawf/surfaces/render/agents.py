"""Render Claude Code agent (subagent) markdown files.

``eawf plugin install claude`` emits one
``.claude/agents/<role>.md`` per Eä subagent role. The output mirrors
the hand-written ``.claude/agents/<role>.md`` placeholder files: YAML
frontmatter (``name``/``description``/``tools``/``model``/``color``)
terminated by ``---`` and followed by a markdown body that
documents the agent's contract.

Roles enumerated per ``enums.md`` ``AgentSession.role``:
``researcher``, ``planner``, ``executor``, ``auditor``, ``reviewer``,
``polisher``, ``operator``, ``domain-specialist``.

Public API::

    AgentTemplateContext         # typed dataclass for one render call
    render_agent_md(ctx) -> str  # pure: returns the rendered markdown
    AGENT_REGISTRY               # frozen tuple of every Eä agent spec
    SERENA_READ_TOOLS            # the read-only symbol triple granted in-registry
    effective_agent_tools(...)   # pure: base allowlist + configured extras
    embed_role_rules(role, body) # the body every runtime and dispatch prompt carries
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from eawf.kernel.config.schema import ALL_ROLES
from eawf.platform.lint.kind_taxonomy import REPRO_TEST_NAME
from eawf.platform.rules.carriers import builtin_carrier_body
from eawf.surfaces.render.frontmatter import yaml_scalar
from eawf.surfaces.render.unwrap import unwrap_markdown_paragraphs

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


# Serena's read-only symbol triple. Granted in the registry rather than
# through ``agents.extra_tools`` because that leaf ships empty: a grant
# that lives only in local config is stripped from every role the next
# time ``eawf plugin install`` renders the tree. Read-only by
# construction — none of the three mutates a file, so widening a role's
# allowlist with them cannot widen its blast radius.
#
# The companion native LSP tools are deliberately absent: they are not
# reachable from a subagent frontmatter allowlist, so naming them there
# would advertise an affordance the role cannot use.
SERENA_READ_TOOLS: tuple[str, ...] = (
    "mcp__serena__find_symbol",
    "mcp__serena__find_referencing_symbols",
    "mcp__serena__get_symbols_overview",
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


def effective_agent_tools(
    spec: AgentSpec,
    extra_tools: Mapping[str, Sequence[str]],
) -> tuple[str, ...]:
    """Return *spec*'s tool allowlist widened by the configured extras.

    Pure: the caller resolves ``agents.extra_tools`` (see
    :func:`eawf.kernel.config.layered.resolve_agent_extra_tools`) and passes
    the validated map in. Order is base tools, then the ``"*"`` grant, then
    the role's own grant, with duplicates collapsed to the first occurrence —
    stable ordering keeps the rendered frontmatter byte-identical across runs
    so the installer's drift detection stays meaningful.

    Args:
        spec: The agent whose declared allowlist is the base.
        extra_tools: Validated ``role -> extra tools`` grant map. Roles absent
            from the map contribute nothing.

    Returns:
        The effective tool tuple to render into the agent's frontmatter.
        Equals ``spec.tools`` when no grant applies to *spec*.
    """
    widened = (
        *spec.tools,
        *extra_tools.get(ALL_ROLES, ()),
        *extra_tools.get(spec.role, ()),
    )
    return tuple(dict.fromkeys(widened))


def embed_role_rules(role: str, body: str) -> str:
    """Return *body* with the rules that bind *role* below its title.

    Every runtime's agent definition and every dispatch prompt carries the
    role rules in its own text: a pointer to a separate file reaches the
    model only if it elects to read it, and the rendered carrier file is
    gitignored, so a fresh clone, a worktree or a repository without a rule
    source has none.

    Args:
        role: The agent role.
        body: The agent body, opening with its title line.

    Returns:
        *body* unchanged when no builtin rule is scoped to *role*; otherwise
        *body* with :func:`~eawf.platform.rules.carriers.builtin_carrier_body`
        as the block after its first line.
    """
    rules = builtin_carrier_body(role)
    if rules is None:
        return body
    title, _, rest = body.partition("\n")
    return f"{title}\n\n{rules.rstrip()}\n{rest}"


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
        "refuted_claim_ids": [],
    },
    "planner": {
        "role": "planner",
        **_BASE_REPORT_BODY,
        "objective": "planning objective",
        "tasks": [
            {
                "task_key": "EAWF-0001",
                "title": "Task title",
                "depends_on": [],
                "success_criteria": ["criterion"],
            }
        ],
        "risks": ["risk to manage"],
    },
    "executor": {
        "role": "executor",
        **_BASE_REPORT_BODY,
        "task_key": "EAWF-0001",
        "files_changed": ["repo/relative/path.py"],
        "tests_run": ["uv run pytest tests/path -q"],
        "test_runs": [
            {"test_id": "tests/path/test_x.py::test_x", "outcome": "red", "revision": "worktree"},
            {"test_id": "tests/path/test_x.py::test_x", "outcome": "green", "revision": "abcdef1"},
        ],
        "commit_sha": "abcdef1",
        "outcome": "implementation outcome",
    },
    "auditor": {
        "role": "auditor",
        **_BASE_REPORT_BODY,
        "target_id": "EAWF-0001",
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
        "milestone_key": "MLS-0001",
        "completed_task_keys": ["EAWF-0001"],
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
        renderer-vs-handwritten swap is byte-clean. Prose paragraphs are
        emitted one line each: the body literals are hard-wrapped to fit the
        source line-length budget, and the no-manual-wrap rule applies to the
        rendered artifact, so the wrap is undone on the way out.
    """
    env = _load_environment()
    template = env.get_template(_TEMPLATE_NAME)
    contract = ctx.output_contract or _typed_output_contract(ctx.role)
    rendered = template.render(
        role=ctx.role,
        description=ctx.description,
        tools=list(ctx.tools),
        model=ctx.model,
        color=ctx.color,
        body=embed_role_rules(ctx.role, unwrap_markdown_paragraphs(ctx.body)).rstrip("\n"),
        output_contract=unwrap_markdown_paragraphs(contract).rstrip("\n"),
    )
    if not rendered.endswith("\n"):
        rendered = rendered + "\n"
    return rendered


# ---------------------------------------------------------------------------
# Frozen v0.1 agent registry. Mirrors the hand-written
# .claude/agents/<role>.md placeholders so the swap from hand-written to
# Eä-rendered files is byte-clean. Bodies are intentionally short
# pointers (operator's full contract lives in docs/agents/<role>.md).
# ---------------------------------------------------------------------------

_RESEARCHER_BODY = """# Researcher

You are read-only. Your job is to reduce uncertainty, not to act on it.

## v0.4 output contract

You emit a typed `IntentBrief` whose claims carry `evidence_refs`.

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

Every Task you emit carries an explicit `agent_role` (`executor` /
`auditor` / `researcher` / `domain-specialist`) and an
`effort_bucket` (`XS|S|M|L|XL`). Read any companion `IntentBrief`
the Milestone is informed by and thread its findings into each Task's
success criteria, so the executor opens the Task already aware of the
relevant brief.

## Inputs you expect

- A Milestone or feature scope from the parent, typically a PLANNED
  Milestone whose Task graph is still empty.
- The canonical plan and supporting docs.
- Optional constraints (e.g., "must land before EAWF-0042").

## Method

1. Read the canonical plan section and any referenced research briefs.
2. Group units of work into self-contained Tasks.
3. Record each Task's dependencies, the paths it claims, and whether
   it must run alone; the Task graph is the concurrency plan.
4. For each Task: success criteria as a checklist, files to
   create/edit, tests to write, expected commit subject.

## Output contract

Emit one plan revision the parent can submit, with one Task per unit
of work carrying its files, success criteria, dependencies and effort
bucket:

```bash
eawf plan submit --from-spec <plan-revision.json>
```

The parent files an `approve / edit / cancel` decision with
`eawf question open-decision` before submitting it and shows the bound
question it answers with (`AskUserQuestion` in Claude Code, the
numbered prompt in Codex). On `approve`, the parent submits, approves
and applies the revision through `/plan`, then runs
`eawf milestone activate`.

## Anti-patterns

- A Task that touches >5 files without justification.
- A success criterion phrased as "the code looks good".
- Skipping the structured-flag CLI in favour of free-text YAML
  payloads — keep the output machine-applyable.
"""

_EXECUTOR_BODY = f"""# Executor

You implement what the planner specified. Stay in scope. Verify before
claiming.

## v0.4 output contract

Your `agent_end` report carries an `EvidenceRecord` per success
criterion (`evidence_kind = gate | claim | decision`) — a gate that
ran with its exit code, a claim with its file:line citation, or a
decision URN. Verification reads the record against the Task's
criteria; if any criterion lacks evidence, surface the gap in the
`pass-with-followups` verdict instead of silently hand-waving.

## Inputs you expect

- A Task spec with success criteria, file list, test list, commit
  subject.
- The parent feature branch name (cherry-pick back, do not merge).
- Permission to use `Bash` for `uv run`, `git`, `gh`, etc.

## Method

1. Read every file the spec names BEFORE editing.
2. Implement edits in dependency order: schemas → logic → CLI →
   tests.
3. Run the local gauntlet: pre-commit, mypy, pytest, ruff.
4. Commit with the spec's commit subject and a 3-6 bullet body.

## Refuse-conditions

- Spec is missing success criteria or file list.
- Scope grows beyond the named files.
- Tests fail and you cannot reproduce locally.

## Before you emit the report

- Boundary AND error-path tests for every public function touched: empty / single /
  off-by-one / max-length; TypeError / ValueError / KeyError / ValidationError.
- Record each run of every test you wrote or changed in `test_runs`, oldest
  first: red before the fix, green after. Verification flags a test reported
  green that never ran red first, and blocks on a repro test named
  `{REPRO_TEST_NAME.form}` that did not.
"""

_AUDITOR_BODY = """# Auditor

You are skeptical by design. You did not implement the work. Your job
is to refute, with evidence, any claim of completion that the code
does not actually support.

## v0.4 output contract

You emit one `EvidenceRecord` per success criterion. Verdicts roll
into the verification report of the target Task or Batch — if a
required criterion fails or cannot be verified, name the missing gate
or claim, do not negotiate the criterion. Your `RoleSpec` pins
fresh-context isolation.

## Inputs you expect

- A target: Task key, Batch key, or commit range.
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
Batch verification cycle alongside the audit, both run through
`/verify` — review findings turn into follow-up Tasks on the same
Milestone, never a new Milestone.

## Inputs you expect

- A diff target: PR number, commit range, or default
  `git diff main...HEAD`.
- Optional: success criteria for the Task or Batch the diff belongs
  to.

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
(including `agent_role`, `effort_bucket`, `evidence_kind`). Each pass
emits an `EvidenceRecord` per category so the polish is auditable the
same way a `/verify` audit or review is.

## Inputs you expect

- A scope: directory, file glob, or "entire `src/eawf/`".
- Optional list of explicit conventions to enforce.

## Method

1. Survey the scope; produce a per-category change list before
   editing.
2. Apply edits grouped by category (naming, docstrings, log fields,
   error messages, dead code).
3. After each group, run `uv run pre-commit run --files <changed>`.
"""

_OPERATOR_BODY = """# Operator

You coordinate a Milestone. You do not write code. You read its Task
graph, dispatch the right specialist per Task, and stitch the results
back together.

## v0.4 dispatch contract

Each Task you dispatch runs as its own Run carrying a `RoleSpec`
(role, model, tools, isolation) resolved from the Task's
`agent_role`. You drive one Delivery Batch at a time through
`/dispatch`, `/verify` and `/integrate`. Operator-level decisions are filed with
`eawf question open-decision` as pending actions with typed options
and shown as the bound question (`AskUserQuestion` in Claude Code,
the numbered prompt in Codex); free-text approvals are forbidden.

## Decision rules

- Independent Tasks → dispatch together, each in its own worktree.
- Dependent Tasks → dispatch in graph order.
- Investigation with no code change → `researcher`.
- Audit of a finished Task → `auditor` (fresh context).

## What you do NOT do

- Touch source code (delegate to `executor`).
- Run tests (delegate to `executor` or `auditor`).
- Commit (the executor does its own commits; cherry-pick into parent).

## Output style

Status updates as you go. End of Batch: a punch list of Tasks
delivered, Tasks remaining, and the next planned dispatch.

## Dispatch-loop discipline (every iteration)

1. If a Run dispatch is refused because dispatch is paused, restart the
   daemon before the next pass.
2. Take the concurrency plan from the Task graph; never invent an ordering.
3. Record each finished Run's report before dispatching the next.
4. Cherry-pick verification: `git log --oneline --all --graph` + `git worktree list`;
   map each reported SHA to where it actually landed;
   confirm via the commit's `Task:` trailer + blob compare;
   ancestry checks false-positive under cherry-pick.
5. Re-validate on the integrated HEAD after cherry-pick (worktree `.pth` false-greens);
   re-check each Task's status before you report it delivered.
6. Sync each Task's claimed paths from the executor report's files_changed,
   through the plan revision that owns the Task.
7. Batch verification and schema-changing Tasks: full-tree gauntlet, never scoped.
8. After any schema or state-model bump: `eawf daemon stop` (it respawns fresh)
   BEFORE the next mutation — a stale-model daemon rejects the new state shape.
"""

_DOMAIN_SPECIALIST_BODY = """# Domain specialist

You handle a project-specific domain (e.g. quant research, web ops,
data ingestion). You are spawned with a tightly scoped task that
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
            "Decomposes a Milestone into a Task graph with explicit success"
            " criteria. Writes the plan revision and per-Task specs."
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
            "Implements a Task per a written spec. Creates/edits files,"
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
            "Fresh-context verifier. Re-reads a finished Task or Batch"
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
            "Coordinates a Milestone by dispatching its Tasks to specialised"
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
            *SERENA_READ_TOOLS,
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
        tools=("Read", "Grep", "Glob", "Bash", "Skill", *SERENA_READ_TOOLS),
        model="opus",
        color="magenta",
        memory=True,
        body=_DOMAIN_SPECIALIST_BODY,
    ),
)


__all__ = [
    "AGENT_REGISTRY",
    "ROLES",
    "SERENA_READ_TOOLS",
    "AgentSpec",
    "AgentTemplateContext",
    "effective_agent_tools",
    "embed_role_rules",
    "render_agent_md",
]
