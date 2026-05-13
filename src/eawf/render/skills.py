"""Render Claude Code ``SKILL.md`` files for installed Eä skills.

Per Phase 4 W05 acceptance §1/§5, ``eawf plugin install claude`` emits
one ``.claude/skills/<name>/SKILL.md`` per skill. The output mirrors the
hand-written placeholders that already live under ``.claude/skills/``:
YAML frontmatter (``name``/``description``/``argument-hint``/
``user-invocable``/``disable-model-invocation``) terminated by ``---``
and followed by a markdown body documenting the canonical algorithm,
the pre-flight checklist, and the output contract.

Public API::

    SkillTemplateContext         # typed dataclass for one render call
    render_skill_md(ctx) -> str  # pure: returns the rendered markdown
    SKILL_REGISTRY               # frozen tuple of every Eä skill spec

The ``SKILL_REGISTRY`` carries the v0.1 surface (six core + four meta
workflow skills, mirroring :data:`~eawf.render.envelope.SkillName`) and
is consumed by :mod:`eawf.runtimes.claude.plugin_install` to produce
the deterministic plugin tree the golden test pins.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib.resources import files

from jinja2 import Environment, FileSystemLoader, StrictUndefined

logger = logging.getLogger(__name__)


_TEMPLATE_NAME: str = "SKILL.md.j2"
_TEMPLATES_PACKAGE: str = "eawf.templates.claude"


@dataclass(frozen=True)
class SkillTemplateContext:
    """Inputs for one :func:`render_skill_md` call.

    Attributes:
        skill_name: Canonical Eä skill name (without the leading slash).
            Frontmatter ``name`` field. Mirrors the ten skill names
            recorded in :data:`~eawf.render.envelope.SkillName`.
        description: One-sentence skill description used by the Claude
            Code skill loader for fuzzy matching.
        argument_hint: ``argument-hint`` string for slash invocation
            (e.g. ``"<topic-slug> [--final]"``).
        user_invocable: Whether the user can invoke the skill directly
            via the slash command.
        disable_model_invocation: Whether the model is barred from
            invoking the skill on its own.
        body: Skill body markdown (algorithm + checklist + output
            contract). Inserted verbatim after the frontmatter.
    """

    skill_name: str
    description: str
    argument_hint: str
    user_invocable: bool
    disable_model_invocation: bool
    body: str


@dataclass(frozen=True)
class SkillSpec:
    """Frozen v0.1 skill spec used by :data:`SKILL_REGISTRY`.

    Mirrors the hand-written ``.claude/skills/<name>/SKILL.md`` shape so
    the renderer-vs-handwritten swap stays byte-clean. ``body`` is the
    markdown body the renderer pastes after the frontmatter.
    """

    skill_name: str
    description: str
    argument_hint: str
    user_invocable: bool
    disable_model_invocation: bool
    body: str
    version: str = "1.0"
    requires: tuple[str, ...] = field(default_factory=tuple)


def _load_environment() -> Environment:
    """Load a Jinja2 environment rooted at the bundled claude templates dir.

    Mirrors :func:`eawf.render.agents_md._load_environment` so loader
    behaviour is consistent across renderers (StrictUndefined,
    ``keep_trailing_newline=False``, autoescape off).
    """
    templates_dir = files(_TEMPLATES_PACKAGE)
    templates_path = str(templates_dir)
    env = Environment(
        loader=FileSystemLoader(templates_path),
        undefined=StrictUndefined,
        keep_trailing_newline=False,
        autoescape=False,
    )
    return env


def render_skill_md(ctx: SkillTemplateContext) -> str:
    """Render a Claude Code ``SKILL.md`` from *ctx*.

    Args:
        ctx: Typed render context. Every attribute is mandatory — the
            Jinja2 ``StrictUndefined`` setting catches a missing key with
            an :class:`~jinja2.exceptions.UndefinedError` rather than
            silently emitting ``""``.

    Returns:
        The rendered markdown text. The frontmatter shape mirrors the
        hand-written placeholder files; the body block is *ctx.body*
        wrapped only by a leading blank line.
    """
    env = _load_environment()
    template = env.get_template(_TEMPLATE_NAME)
    rendered = template.render(
        skill_name=ctx.skill_name,
        description=ctx.description,
        argument_hint=ctx.argument_hint,
        user_invocable=ctx.user_invocable,
        disable_model_invocation=ctx.disable_model_invocation,
        body=ctx.body.rstrip("\n"),
    )
    if not rendered.endswith("\n"):
        rendered = rendered + "\n"
    return rendered


def render_skill_md_from_spec(spec: SkillSpec) -> str:
    """Render a SKILL.md from a :class:`SkillSpec`.

    Convenience wrapper that builds the :class:`SkillTemplateContext`
    from *spec* and forwards to :func:`render_skill_md`. Used by both
    :mod:`eawf.runtimes.claude.plugin_install` (file-on-disk render) and
    :func:`eawf.cli.commands.skill.render_cmd` (stdout dump) so the two
    code paths emit byte-identical bytes for the same registry entry.
    """
    return render_skill_md(
        SkillTemplateContext(
            skill_name=spec.skill_name,
            description=spec.description,
            argument_hint=spec.argument_hint,
            user_invocable=spec.user_invocable,
            disable_model_invocation=spec.disable_model_invocation,
            body=spec.body,
        )
    )


# ---------------------------------------------------------------------------
# Frozen v0.1 skill registry. Mirrors :data:`SkillName` literal — six core
# (research/prep/audit/ship/review/polish) + four meta (init/roadmap/
# differentiate/flow). Bodies are intentionally short pointers; full
# canonical algorithms live in docs/architecture/workflow.md.
# ---------------------------------------------------------------------------

_RESEARCH_BODY = """# /research

## Canonical algorithm

1. Define the question. State the hypothesis or unknown in one sentence.
2. Survey: read source, run `git log`, fetch external refs as needed.
3. Compare alternatives — bullet list of options with pros/cons.
4. Verdict: recommend one path, or recommend "stay open" with the next
   discriminating experiment.
5. If `--final`: persist a research brief via the `/research` skill body.

## Pre-flight checklist

- [ ] No state mutations — read-only.
- [ ] Cite sources by `path:line` or external URL.
- [ ] Distinguish "what the code does" from "what the doc claims".

## Decision surfaces

When the verdict reduces to a small set of named alternatives, surface
the choice through `AskUserQuestion` rather than free-text — the
operator can pick without retyping the option labels.

## Output contract

Eä-rendered skill envelope (`OutputEnvelope`) with `header.skill =
"/research"`. Body carries the structured findings; footer records any
persisted brief.
"""

_PREP_BODY = """# /prep

## Canonical algorithm

1. Resolve `<phase-id>` against the plan / state.
2. Enumerate waves; mark each `parallel | sequential` per the plan.
3. **Plan-mode proposal (default).** If `planning.auto_plan` is `false`
   (the default; check via `uv run eawf config get planning.auto_plan`)
   and `--auto-plan` was not passed, enter Claude Code plan mode
   (`EnterPlanMode`) and present the proposed wave DAG (IDs, deps,
   file scopes, success criteria, estimated EU). Exit via
   `ExitPlanMode` only after operator approval. When `auto_plan` is
   `true` or `--auto-plan` is set, skip the proposal and dispatch
   inline.
4. For each parallel wave, dispatch a worktree subagent.
5. For each sequential wave, run inline; cherry-pick parallel-wave
   commits in between as they finish.
6. Update plan checkboxes / state via `eawf state phase advance`.

## Pre-flight checklist

- [ ] Confirm current branch is the long-running phase branch.
- [ ] Confirm `git status` is clean.
- [ ] Confirm worktree subagents branch from the parent HEAD.
- [ ] Plan-mode proposal is the default; pass `--auto-plan` only when
      the wave DAG is trivial or pre-approved.

## Decision surfaces

When the algorithm reaches a discrete choice (e.g. "split or merge
waves W03+W04?", "use worktree per wave or run inline?"), surface
the options via `AskUserQuestion` rather than free-text prompts —
the operator's UI offers a faster confirm path and the answer is
machine-parsable.

## Output contract

Skill envelope describing the dispatched waves and the expected
cherry-pick order. When the operator approves a plan-mode proposal,
the envelope's `body.plan_mode_approval` records the approval source
(`config-auto-plan`, `arg-auto-plan`, or `operator-approved`).
"""

_AUDIT_BODY = """# /audit

## Canonical algorithm

1. Resolve target: phase id, wave id, or commit range.
2. Identify success criteria from the plan / phase spec.
3. Dispatch the auditor subagent with paths, line numbers, criteria.
4. Parse the verdict; convert refutations into TODOs or new waves.

## Pre-flight checklist

- [ ] The auditor must NOT have access to the parent conversation.
- [ ] Every quantitative claim must include `Read`/`Grep` evidence.

## Decision surfaces

On `pass-with-followups`: present the follow-up disposition (open
backlog, open wave, defer) through `AskUserQuestion`. On `fail`:
ask whether to halt the flow or open a remediation wave.

## Output contract

Skill envelope with a per-criterion verdict table and an aggregate
status (`pass | pass-with-followups | fail`).
"""

_SHIP_BODY = """# /ship

## Canonical algorithm

1. Resolve `<phase-id>`; verify all waves under it are complete.
2. Run the local verification gauntlet (pre-commit, mypy, pytest, ruff).
3. Push the long-running feature branch.
4. Open the phase PR via `gh pr create`.
5. After merge, advance state via `eawf state phase close <NN>`.

## Pre-flight checklist

- [ ] All waves under `<phase-id>` are complete.
- [ ] Cherry-picks from worktree subagents have all landed.
- [ ] CI on the latest push is green.

## Decision surfaces

`gh pr create`, `gh pr merge`, and any push to a protected branch are
irreversible/visible-to-others actions per AGENTS.md — surface the
final confirm through `AskUserQuestion` (options: `proceed` / `defer`
/ `abort`) unless `vcs.auto_push`, `vcs.pr_open`, and the merge
strategy are pre-resolved by config.

## Output contract

Skill envelope carrying the PR URL, the post-merge state mutation, and
any deferred follow-ups.
"""

_REVIEW_BODY = """# /review

## Canonical algorithm

1. Resolve target: PR number → `gh pr diff <PR>`; commit range →
   `git diff <range>`; default → `git diff main...HEAD`.
2. Walk the diff hunk by hunk. For each hunk, read enough surrounding
   context to make a judgment.
3. Apply rules in order: correctness > security > clarity > style.
4. Tag findings: 🔴 blocker, 🟠 must-fix, 🟡 should-fix, 🔵 nit.

## Pre-flight checklist

- [ ] Read the success criteria for the phase/wave the diff belongs to.
- [ ] Verify any quantitative claim against `Read`/`grep`.

## Decision surfaces

When the final verdict is ambiguous (e.g. one 🟠 finding the operator
might choose to defer), surface `approve | request-changes |
comment-only` through `AskUserQuestion` rather than picking silently.

## Output contract

Skill envelope with a flat findings list grouped by file and an
aggregate verdict (`approve | request-changes | comment-only`).
"""

_POLISH_BODY = """# /polish

## Canonical algorithm

1. Resolve scope: default = entire `src/eawf/`; `--scope=<dir|file>`
   narrows.
2. Sweep checks: naming, docstrings, log fields, error message
   phrasing, dead code.
3. Apply fixes inline. If a change touches public API, stop and ask.

## Pre-flight checklist

- [ ] Scope is declared and bounded.
- [ ] No public API rename without explicit user confirmation.

## Decision surfaces

Public-API renames, dead-code deletions, and anything matching
`polish.deletion_policy` MUST be raised via `AskUserQuestion`
(options: `apply` / `defer-to-backlog` / `skip`) instead of asking
in free text. `polish.auto_apply_safe=true` bypasses the prompt for
the small "safe" subset only (formatting, comment phrasing).

## Output contract

Skill envelope with a change list grouped by category and a deferred-
items list for changes needing user OK.
"""

_INIT_BODY = """# /init

## Canonical algorithm

1. Discover existing `.ea/` (if any) and load profile composition.
2. Render managed regions of `AGENTS.md`, `CLAUDE.md`, `.claude/`.
3. Persist `.ea/state.json` and `.ea/profile.yaml`.

## Pre-flight checklist

- [ ] Working tree is clean before the first init.
- [ ] Profile composition is declared.

## Output contract

Skill envelope wrapping the wizard outcome. `status=needs_user` when
the wizard pauses on an unanswered question.
"""

_ROADMAP_BODY = """# /roadmap

## Canonical algorithm

1. Read open hypotheses, audits, and decisions from `state.json`.
2. Cluster by phase / wave; surface stale items.
3. Recommend a next move per cluster.

## Pre-flight checklist

- [ ] Read-only — no state mutations.

## Output contract

Skill envelope with a structured roadmap body (clusters, stale items,
next-move recommendations).
"""

_DIFFERENTIATE_BODY = """# /differentiate

## Canonical algorithm

1. Take a candidate path and the alternatives under consideration.
2. Identify the discriminating signal (`what would change my mind?`).
3. Recommend the cheapest experiment that produces the signal.

## Pre-flight checklist

- [ ] Read-only — no state mutations.

## Output contract

Skill envelope listing the discriminating experiments and the
recommended next move.
"""

_FLOW_BODY = """# /flow

## Canonical algorithm

1. Run `/research` → `/prep` → `/audit` → `/ship` → `/review` →
   `/polish` sequentially.
2. **Inter-stage gate (default).** After each step returns
   `status=ok`, check `flow.auto_accept.<stage>` (via
   `uv run eawf config get flow.auto_accept.<stage>`). When `false`
   (the default) and the stage was not listed in `--auto-accept`,
   ask the operator via `AskUserQuestion` whether to proceed —
   options: `proceed` / `skip-next` / `stop`. When `true`, advance
   without a prompt.
3. On any non-`ok` status (`blocked`, `needs_user`, `failed`,
   `partial`), short-circuit with the failing step's repair commands.

## Pre-flight checklist

- [ ] All upstream skills are installed.
- [ ] Per-stage `flow.auto_accept` flags reflect the operator's
      intended cadence (review existing values; default is "ask each
      time" for every stage).

## Decision surfaces

`/flow` is a long-running pipeline. Every operator-facing decision
point — inter-stage gates, "abandon vs retry on `failed`",
"merge order on `needs_user`" — MUST be raised through
`AskUserQuestion` so the run stays unstuck without dropping the
operator into free-text. Per-step skills already follow this rule;
the flow merely propagates their `needs_user` envelopes verbatim.

## Output contract

Skill envelope whose body accumulates per-step envelopes plus the
inter-stage gate decisions. Status is `ok` when every step passed
(after any auto-accept or operator confirm), otherwise the first
non-`ok` step's status is propagated.
"""

_BLITZ_BODY = """# /blitz

## Canonical algorithm

1. Read residual unknown count and follow-up research args.
2. Increment the recursion guard (`EAWF_BLITZ_DEPTH_COUNTER`) against
   `EAWF_BLITZ_DEPTH` (default 8).
3. Return a follow-up `/research` action with `blitz=false` so the next
   research pass does not recurse indefinitely.

## Pre-flight checklist

- [ ] Confirm `/research` produced more than one residual unknown.
- [ ] Confirm recursion depth has not exceeded the configured cap.

## Output contract

Skill envelope with `header.skill = "/blitz"`. Body carries depth,
depth_cap, residual_unknowns, followup_research_args, and next_actions.
"""


SKILL_REGISTRY: tuple[SkillSpec, ...] = (
    SkillSpec(
        skill_name="research",
        description=(
            "Read-only investigation of an open question. Produces a research"
            " brief or surfaces findings inline; no code changes, no state"
            " mutations."
        ),
        argument_hint="<topic-slug> [--final]",
        user_invocable=True,
        disable_model_invocation=True,
        body=_RESEARCH_BODY,
    ),
    SkillSpec(
        skill_name="prep",
        description=(
            "Open the next phase or wave by writing a work plan and"
            " dispatching subagents for execution."
        ),
        argument_hint="<phase-id> [wave-id] [--auto-plan]",
        user_invocable=True,
        disable_model_invocation=True,
        body=_PREP_BODY,
    ),
    SkillSpec(
        skill_name="audit",
        description=(
            "Fresh-context verification of a phase deliverable or wave"
            " outcome. Spawns a fresh auditor subagent that re-reads the diff"
            " against the success criteria."
        ),
        argument_hint="<phase-id|wave-id|commit-range>",
        user_invocable=True,
        disable_model_invocation=True,
        body=_AUDIT_BODY,
    ),
    SkillSpec(
        skill_name="ship",
        description=(
            "Close out a phase by running the full local CI surface, opening"
            " the phase PR, and (after merge) advancing state."
        ),
        argument_hint="<phase-id> [--dry-run]",
        user_invocable=True,
        disable_model_invocation=True,
        body=_SHIP_BODY,
    ),
    SkillSpec(
        skill_name="review",
        description=(
            "Code review of an open PR or local diff. Surfaces issues with"
            " severity tags; no scope creep, no praise."
        ),
        argument_hint="[<PR# | commit-range>]",
        user_invocable=True,
        disable_model_invocation=True,
        body=_REVIEW_BODY,
    ),
    SkillSpec(
        skill_name="polish",
        description=(
            "Repo-wide consistency sweep. Aligns naming, docstring style, log"
            " fields, error message phrasing, and removes dead code."
        ),
        argument_hint="[--scope=<dir|file>]",
        user_invocable=True,
        disable_model_invocation=True,
        body=_POLISH_BODY,
    ),
    SkillSpec(
        skill_name="init",
        description=(
            "Initialise a new Eä Workflow workspace. Renders managed regions"
            " of AGENTS.md and the .claude/ plugin tree."
        ),
        argument_hint="[--profile=<id>]",
        user_invocable=True,
        disable_model_invocation=True,
        body=_INIT_BODY,
    ),
    SkillSpec(
        skill_name="roadmap",
        description=(
            "Surface open hypotheses, audits, and decisions; cluster by"
            " phase/wave and recommend next moves."
        ),
        argument_hint="",
        user_invocable=True,
        disable_model_invocation=True,
        body=_ROADMAP_BODY,
    ),
    SkillSpec(
        skill_name="differentiate",
        description=(
            "Recommend the cheapest experiment that discriminates between two"
            " or more candidate paths."
        ),
        argument_hint="<candidate-id>",
        user_invocable=True,
        disable_model_invocation=True,
        body=_DIFFERENTIATE_BODY,
    ),
    SkillSpec(
        skill_name="flow",
        description=(
            "Run /research → /prep → /audit → /ship → /review → /polish"
            " sequentially; short-circuit on any non-ok status."
        ),
        argument_hint="<task-slug> [--auto-accept=<stage>[,<stage>...]]",
        user_invocable=True,
        disable_model_invocation=True,
        body=_FLOW_BODY,
    ),
    SkillSpec(
        skill_name="blitz",
        description=(
            "Auto-chained research follow-up skill with recursion guard for residual unknowns."
        ),
        argument_hint="[--residual-unknowns=<n>]",
        user_invocable=True,
        disable_model_invocation=True,
        body=_BLITZ_BODY,
    ),
)


__all__ = [
    "SKILL_REGISTRY",
    "SkillSpec",
    "SkillTemplateContext",
    "render_skill_md",
    "render_skill_md_from_spec",
]
