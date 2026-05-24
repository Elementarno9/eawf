"""Frozen :data:`SKILL_REGISTRY` data for the Eä skill surface.

Per Phase 4 W05 acceptance §1/§5, ``eawf plugin install claude`` emits
one ``.claude/skills/<name>/SKILL.md`` per skill. The output mirrors the
hand-written placeholders that already live under ``.claude/skills/``:
YAML frontmatter (``name``/``description``/``argument-hint``/
``user-invocable``/``disable-model-invocation``) terminated by ``---``
and followed by a markdown body documenting the canonical algorithm,
the pre-flight checklist, and the output contract.

This module holds only the data: the per-skill body strings and the
frozen :data:`SKILL_REGISTRY` tuple. The typed render context / spec
dataclasses and the Jinja2-backed ``render_skill_md`` helpers live in the
sibling :mod:`eawf.render.skills.render`; the package ``__init__``
re-exports both halves so every historical
``from eawf.render.skills import ...`` keeps resolving unchanged.

The ``SKILL_REGISTRY`` carries the operator-facing workflow skills (six
core + four meta + the C04b skill-surfaces) plus a tail of model-only
code-quality playbooks (``user_invocable=False`` — hidden from the slash
menu but reachable by the model). It is consumed by
:mod:`eawf.runtime.runtimes.claude.plugin_install` to produce the deterministic
plugin tree the golden test pins.
"""

from __future__ import annotations

import logging

from eawf.render.skills.render import SkillSpec

logger = logging.getLogger(__name__)


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
5. If `--final`: persist a research brief with `references` and render
   it through `eawf research show --md`.

## Spike convention

A *spike* — a short read-only investigation done before claiming a
real wave — is run via `/research` and produces a brief under
`.ea/local/<YYYY-MM-DD>-<slug>.md` (or the conventional
`.ea/local/research/` sub-directory). The filename follows the
`<date>-<slug>.md` stem so it sorts chronologically and slug-matches
the wave, iter, or phase it informs. Briefs stay local-only —
`.ea/local/` is gitignored — and are promoted to `.ea/artifacts/`
only when they inform a decision recorded in `state.json` (the
artifact-chassis rule then applies). See `spike-workflow` in
AGENTS.md for the full convention.

## Pre-flight checklist

- [ ] No state mutations — read-only.
- [ ] Cite sources as dense `[N]` references backed by `Citation` rows.
- [ ] Keep promoted artifact prose scrub-clean and repo-relative.
- [ ] Distinguish "what the code does" from "what the doc claims".
- [ ] If this run is a spike, name the brief
      `<YYYY-MM-DD>-<slug>.md` and place it under `.ea/local/` (or
      `.ea/local/research/`) so the dispatch renderer can surface it
      to the next wave's executor.

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

P19-W07 turns `/prep` into an activator. The flow now branches on the
phase's PLANNED-queue state:

1. Resolve `<phase-id>` against `state.phases`.
2. Branch on phase status + wave plan:

   - **Case A — PLANNED phase with at least one PENDING wave.**
     Render the plan via `eawf roadmap show --phase <id> --md`.
     Enter Claude Code plan mode (`EnterPlanMode`) with the rendered
     DAG, then surface an `AskUserQuestion` with the options
     `use-as-is`, `revise`, `replace`, `cancel`. On `use-as-is`,
     call `eawf phase activate <id>` (which runs the V11 hard
     gate: ≥1 wave + deps phases CLOSED). On `revise`, hand back to
     `/roadmap revise`. On `replace`, hand back to `/roadmap drop`
     + `/roadmap propose`.
   - **Case B — PLANNED phase with empty wave DAG.** Dispatch the
     `planner` agent (`build/eawf-plugin/agents/planner.md`). The
     planner returns a sequence of `eawf roadmap revise --add-wave`
     commands (or a YAML payload). **Apply the planner's commands
     first** through the state CLI — waves land as PENDING on the
     still-PLANNED iter — then render the resulting DAG via
     `eawf roadmap show --phase <id> --md` and enter Claude Code
     plan mode (`EnterPlanMode`) with that rendering. Surface
     `AskUserQuestion` with `approve`, `edit`, `cancel`. The
     operator reviews the rendered roadmap, not the planner's raw
     commands. Edits during plan mode are `/roadmap revise` calls
     (PLANNED scope is mutable). On `approve`, call
     `eawf phase activate <id>` (V11 hard gate).
   - **Case C — no PLANNED phase by that id.** Reject with exit 4
     and hint `Run \\`eawf roadmap propose --phase <id> --title ...\\`
     first.` for the operator.

3. **Optional spike first.** Before claiming a wave whose success
   criteria are not yet writable, run `/research <topic>` as a *spike*
   (read-only) per the `spike-workflow` rule in AGENTS.md. The spike
   produces a brief under `.ea/local/<YYYY-MM-DD>-<slug>.md` (or the
   conventional `.ea/local/research/` sub-directory). When a matching
   spike brief exists, the plan-mode proposal in case A MUST reference
   it by repo-relative path so the operator and the dispatched executor
   read the same source-of-truth artifact — the wave dispatch renderer
   surfaces matching briefs under a `## References` section
   automatically.
4. For each parallel wave under the activated iter, dispatch a
   worktree subagent.
5. For each sequential wave, run inline; cherry-pick parallel-wave
   commits in between as they finish.
6. Validate the rendered plan with `eawf plan show --md`; wave tags
   and bucket roll-ups must match state.

## Pre-flight checklist

- [ ] Confirm current branch is the long-running phase branch.
- [ ] Confirm `git status` is clean.
- [ ] Confirm worktree subagents branch from the parent HEAD.
- [ ] Every wave has success criteria, agent role, effort bucket, and
      file scope.
- [ ] The target phase exists in `state.phases` with status `planned`
      (otherwise hand back to `/roadmap propose`).
- [ ] If a spike preceded the claim, its brief path is cited in the
      plan-mode proposal (case A) so the dispatched subagent reads it.

## Decision surfaces

`AskUserQuestion` is the canonical surface for the case-A
`use-as-is/revise/replace/cancel` pick and the case-B
`approve/edit/cancel` pick. Free-text prompts are forbidden per the
project-wide approval policy.

## Output contract

Skill envelope describing the activated phase + dispatched waves and
the expected cherry-pick order. The envelope's
`body.plan_mode_approval` records the approval source
(`use-as-is`, `revise`, `replace`, `planner-approve`).
"""

_AUDIT_BODY = """# /audit

## Canonical algorithm

1. Resolve target: phase id, wave id, or commit range.
2. Identify success criteria from the plan / phase spec and cite evidence
   with dense `[N]` references.
3. Dispatch the auditor subagent with paths, line numbers, criteria.
4. Parse the verdict; convert refutations into TODOs or new waves.
5. Render audit evidence through `eawf audit show --md`.

## Pre-flight checklist

- [ ] The auditor must NOT have access to the parent conversation.
- [ ] Every quantitative claim must include source evidence and dense
      citation refs.
- [ ] The target iter is NOT yet closed. Iter close is gated on
      `audit + polish + ship CI + PR review pass` per the
      `iter-phase-close-timing` rule in AGENTS.md; `/audit` runs
      before that close.

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
3. Validate artifact markdown and PR prose against the chassis/scrub
   rules.
4. Push the long-running feature branch.
5. Open the phase PR via `gh pr create`.
6. **PR-review pass.** Read remote review comments via
   `gh pr view <PR> --comments` (or the inline equivalent). For each
   actionable finding, append a follow-up wave to the current iter
   via `eawf roadmap revise --add-wave` (not a new iter — per the
   `iter-phase-close-timing` rule). Implement, re-push, wait for
   green CI, re-request review until clean.
7. **Bundle close in the final pre-merge commit.** Once CI is green
   and the review-passed branch is on the remote, emit a single
   `[P<NN>] state: close iter + phase (audit=<id>)` commit
   (the legacy `[P<NN>-CORE] state: ...` form remains valid per the
   `commit-prefix` block in AGENTS.md) that bundles
   `eawf iter close P<NN>-I<MM>` + `eawf phase close P<NN>` (no
   other touched files). The operator merges that commit to end the
   phase.

## Pre-flight checklist

- [ ] All waves under `<phase-id>` are complete.
- [ ] Cherry-picks from worktree subagents have all landed.
- [ ] `eawf artifact validate` passes for promoted markdown.
- [ ] CI on the latest push is green.
- [ ] `/audit` and `/polish` have already run on the iter — phase
      close is gated on both per `iter-phase-close-timing`.

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
5. Check artifact chassis and dense references when reviewing docs or
   promoted artifacts.

## Pre-flight checklist

- [ ] Read the success criteria for the phase/wave the diff belongs to.
- [ ] Verify any quantitative claim against `Read`/`grep`.
- [ ] Verify markdown artifacts keep `Summary`, `References`,
      `Provenance`, and `Scrub` sections.

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
   phrasing, dead code, citation density, draft sentinels, scrub status.
3. Apply fixes inline. If a change touches public API, stop and ask.

## Pre-flight checklist

- [ ] Scope is declared and bounded.
- [ ] No public API rename without explicit user confirmation.
- [ ] The target iter is NOT yet closed. Iter close is gated on
      `audit + polish + ship CI + PR review pass` per the
      `iter-phase-close-timing` rule in AGENTS.md; `/polish` runs
      after `/audit` and before that close.

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

## Decision surfaces

When the wizard pauses on an unanswered question, the `status=needs_user`
envelope routes the operator to an `AskUserQuestion` prompt for the
missing field rather than guessing a default.

## Output contract

Skill envelope wrapping the wizard outcome. `status=needs_user` when
the wizard pauses on an unanswered question.
"""

_ROADMAP_BODY = """# /roadmap

## Canonical algorithm

1. **`propose`** stages a new PLANNED phase + its `P##-I01` iter on
   the queue without any waves yet. Emits a `needs_user` envelope
   with the rendered plan text — the active runtime (Claude
   plan-mode, Codex text-prompt) surfaces it for operator approval.
2. **`revise`** edits the PLANNED scope via structured flags:
   `--add-wave`, `--remove-wave`, `--set-deps`, `--retitle`.
   Wave-level mutations route through the P19-W01 PENDING-only
   transitions.
3. **`apply`** is the post-propose confirmation step. It validates
   that the phase is PLANNED with at least one wave and emits an
   `ok` envelope; the actual planning is already persisted (propose
   does the state mutation). Use it as the handoff into `/prep`.
4. **`drop`** archives a PLANNED phase (PLANNED → ARCHIVED) when
   the operator rejects the proposed plan.
5. **`show`** renders the queue: text table (default), markdown
   (`--md`), or JSON envelope (`--json`).

## Pre-flight checklist

- [ ] State CLI is the only mutator; `state.json` writes happen
      inside `state_transaction` so the sibling lock is held.
- [ ] Brief ids passed via `--from-briefs` should be promoted
      research artefacts (RES-YYYY-MM-DD-NNN).
- [ ] One phase at a time. Bulk-propose is deferred.

## Decision surfaces

`roadmap propose` is the single decision surface — its envelope
status is `needs_user`. The runtime adapter (Claude / Codex /
OpenCode) maps the envelope's `decision_kind=approve_plan` body to
its native confirm UI. `revise`, `apply`, and `drop` emit `ok`
envelopes — operator has already approved via propose (or is
walking back via drop).

## Output contract

`status=needs_user` envelope for `propose` (carries `plan_text` +
`options`). `status=ok` envelope for `revise`, `apply`, `drop`,
`show` — body shape varies per command. JSON envelope is the
machine surface; the default text render is for terminal use.
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

1. Run `/research` → `/prep` → `/audit` → `/polish` → `/ship`
   sequentially. The PR-review pass is folded into `/ship` (it reads
   the remote review comments, addresses feedback by appending
   waves to the current iter, then bundles iter + phase close in
   the final pre-merge commit per the `iter-phase-close-timing`
   rule in AGENTS.md).
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

_COAUTHOR_BODY = """# /coauthor

## Canonical algorithm

1. Read the `vcs.coauthor` config block (`mode` ∈
   `runtime|project|disabled`) and the caller-supplied `mode` /
   `runtime` / `message_text` args.
2. Run the canonical resolver (`resolve_coauthor_trailer`) to turn the
   config + the explicit runtime opt-in into a `Co-Authored-By:` trailer
   line (or `None` when trailers are disabled).
3. On `mode=runtime` with no resolvable runtime (no explicit opt-in and
   no usable default identity), degrade to `status=needs_user` so the
   runtime adapter surfaces a `coauthor resolve` prompt rather than
   guessing.

This skill is a thin surface over the shipped co-author policy
machinery; it does not reimplement trailer resolution.

## Pre-flight checklist

- [ ] Never invent an author identity — `disabled` mode rejects an
      existing trailer in `message_text`.
- [ ] No PII beyond the canonical author block.

## Decision surfaces

`status=needs_user` routes to a `coauthor resolve` `AskUserQuestion`
when the runtime cannot be resolved.

## Output contract

Skill envelope with `header.skill = "/coauthor"`. Body carries mode,
runtime, and the resolved trailer (or a reason on the needs_user path).
"""

_MEMORY_BODY = """# /memory

## Canonical algorithm

1. Resolve the verb (`save` default / `list` / `forget`) and the target
   tier (`working` default / `archival` / `retrieval`).
2. A named verb (`save` / `forget`) without a `name` degrades to
   `status=needs_user`.
3. Append a single append-only `EVENT` describing the operation intent;
   the daemon is the sole canonical writer of the memory JSONL store, so
   the skill routes the operator to the `eawf memory` writer via
   `next_valid_actions` rather than mutating the store itself.

## Pre-flight checklist

- [ ] The skill records intent only — the daemon owns the store write.
- [ ] `save` / `forget` carry a memory entry name.

## Decision surfaces

A named verb (`save` / `forget`) without a `name` degrades to
`status=needs_user`, which routes the operator to an `AskUserQuestion`
prompt for the missing entry name rather than inventing one.

## Output contract

Skill envelope with `header.skill = "/memory"`. Body carries verb, name,
and tier (or a reason on the needs_user path).
"""

_AGENT_DISPATCH_BODY = """# /agent-dispatch

## Canonical algorithm

1. Resolve the target `wave_id` (required; a missing id degrades to
   `status=needs_user`).
2. Read the `Wave.runtime_preference` ladder (or an explicit
   `runtime_preference` arg); an explicit `runtime` arg overrides the
   ladder head.
3. Surface the full ladder and the resolved head. No resolvable runtime
   is a soft `status=partial` (the dispatch can still proceed against the
   daemon default, but the operator can pin a preference).
4. The daemon's `agent.dispatch` RPC is the canonical mutator; the skill
   routes to `eawf wave dispatch` via `next_valid_actions`.

## Pre-flight checklist

- [ ] The wave is claimed before dispatch.
- [ ] The runtime ladder reflects how the planner sized the wave.

## Decision surfaces

A missing `wave_id` degrades to `status=needs_user`. When no runtime in
the ladder resolves, the soft `status=partial` routes the operator to an
`AskUserQuestion` prompt to pin a runtime preference rather than silently
falling through to the daemon default.

## Output contract

Skill envelope with `header.skill = "/agent-dispatch"`. Body carries
wave_id, runtime_preference, and resolved_runtime.
"""

_COMPRESS_BODY = """# /compress

## Canonical algorithm

1. Read `tokens_before` (required; missing/zero degrades to
   `status=needs_user`) and `tokens_after` (defaults to a no-op when
   omitted; clamped so a pass can only shrink the context).
2. Build the per-runtime compression directive (cache-control wiring) for
   the target `runtime` (defaults to `claude-code`, the only runtime with
   a caller-side cache-control marker). An unknown runtime degrades to
   `status=needs_user`.
3. Append the canonical `compression_emitted` event carrying the token
   deltas and the realised ratio so the telemetry projector can chart
   context pressure over a session.

The model summarisation fan-out lives behind the runtime adapter's
cache-control hook; the skill records the requested compression.

## Pre-flight checklist

- [ ] `tokens_before` is present and > 0.
- [ ] The target runtime is a known runtime id.

## Decision surfaces

A missing/zero `tokens_before` or an unknown `runtime` degrades to
`status=needs_user`, which routes the operator to an `AskUserQuestion`
prompt for the missing input rather than emitting a compression event
against an unresolved runtime.

## Output contract

Skill envelope with `header.skill = "/compress"`. Body carries
tokens_before, tokens_after, ratio, runtime, and cache_control_applied.
"""

_WAVE_SPEC_BODY = """# /wave-spec

## Canonical algorithm

1. Resolve the verb (`init` default / `validate`) and the target
   `wave_id` (required; a missing id degrades to `status=needs_user`).
2. Thread the optional `mockup_waiver_reason` (C03 D11) through so a
   downstream scaffold can carry it onto the WaveSpec without forcing an
   ASCII mockup for non-UI waves.
3. Append a single append-only `EVENT` describing the operation intent;
   the daemon owns spec scaffolding + cache mutation, so the skill routes
   to the `eawf spec` writer via `next_valid_actions`.

## Pre-flight checklist

- [ ] The wave exists before `init` / `validate`.
- [ ] A Mockup-waiver reason is supplied for non-UI waves that skip the
      ASCII mockup.

## Decision surfaces

A missing `wave_id` degrades to `status=needs_user`, which routes the
operator to an `AskUserQuestion` prompt for the target wave rather than
scaffolding against an unresolved id.

## Output contract

Skill envelope with `header.skill = "/wave-spec"`. Body carries verb,
wave_id, and mockup_waiver_reason.
"""

_SECURITY_REVIEW_BODY = """# /security-review

## Canonical algorithm

1. Load the caller-supplied audit spec (a YAML file of declarative
   checks) via the audit-check DSL; a missing/unreadable `spec_path`
   degrades to `status=needs_user`.
2. Dispatch every check through the DSL runner against the target `cwd`
   (defaults to the process working tree).
3. Fold the pass/fail tally into the body. The terminal status is `ok`
   when every check passes and `failed` when any check fails (failing
   check names surface as repair commands).

When the active profile is `security` (a C08 contribution), this skill is
a required gate for `phase close`.

## Pre-flight checklist

- [ ] `spec_path` points at a readable declarative audit spec.
- [ ] The scope under review is closed.

## Decision surfaces

A missing or unreadable `spec_path` degrades to `status=needs_user`,
which routes the operator to an `AskUserQuestion` prompt for the audit
spec rather than running an empty check set.

## Output contract

Skill envelope with `header.skill = "/security-review"`. Body carries
scope_id, spec_path, checks_run, and the per-check findings.
"""

# ---------------------------------------------------------------------------
# Model-only code-quality skills. These carry no executable skill body in
# `eawf.workflow.skills.registry`; they are prompt-only playbooks the model invokes
# while editing source. Each renders with `user-invocable: false` so it is
# hidden from the slash menu, and `disable-model-invocation: false` so the
# model may still reach for it. Bodies document the canonical refactoring
# procedure rather than a CLI output contract.
# ---------------------------------------------------------------------------

_REFACTOR_GOD_CLASS_BODY = """# refactor-god-class

A model-only refactoring playbook. There is no slash command and no CLI
verb; the model invokes this while editing a module whose single class
has accreted too many responsibilities.

## When to reach for it

- One class owns parsing, validation, persistence, and presentation.
- The class exceeds ~300 lines or has more than ~7 public methods that
  cluster into distinct concerns.
- Tests for the class need elaborate setup because one method depends on
  state another method mutates.

## Canonical procedure

1. Map responsibilities. List each public method and tag it with the one
   concern it serves (parse, validate, compute, persist, render).
2. Find the seams. Group methods sharing the same tag and the same
   private state; each group is a candidate collaborator.
3. Extract the lowest-coupling group first into its own class with a
   constructor that takes only the state it needs (no back-reference to
   the god class).
4. Replace the in-class call sites with delegation to the new
   collaborator; keep the god class's public API stable for one step so
   callers do not churn.
5. Repeat per concern. When the god class is a thin coordinator, decide
   whether it survives as a facade or dissolves into its callers.

## Guardrails

- One concern per extraction commit; never move two seams at once.
- Preserve behaviour: run the existing tests after each extraction before
   moving the next group.
- Honour the project conventions — `extra="forbid"` on any new Pydantic
   model, single-responsibility per the AGENTS.md engineering rules, and
   no speculative abstraction (YAGNI).
"""

_WRITE_ADR_BODY = """# write-adr

A model-only playbook for drafting an architecture decision record. There
is no slash command; the model invokes this when a design choice needs a
durable, reviewable rationale. In an Eä-managed repo the decision itself
is a typed row in `state.json` — the ADR markdown is the human-readable
companion, never the source of truth.

## When to reach for it

- Two or more designs were weighed and one was picked.
- The choice constrains future work (a dependency, a schema shape, a
   protocol boundary) and a later reader will ask "why this?".
- An audit or spike produced a verdict that should outlive the session.

## Canonical structure

1. **Context** — the forces in tension, in two or three sentences. State
   the constraint, not the solution.
2. **Options** — each candidate as a bullet with its concrete trade-off.
   Name the option the way the codebase will refer to it.
3. **Decision** — the chosen option, stated as a present-tense assertion.
4. **Consequences** — what becomes easy, what becomes hard, what is now
   forbidden.

## Guardrails

- Reference the audit or spike that justifies the decision so the evidence
   chain is reconstructible (research-workflow rule).
- Keep prose scrub-clean: no machine paths, hostnames, or PII; references
   stay repo-relative, an external URL, or an Eä URN.
- The ADR records WHY; provenance ids (audit, roundtable) live in the
   decision row and the commit body, not in source comments (rule 25).
"""

_ADD_PROPERTY_TEST_BODY = """# add-property-test

A model-only playbook for adding a property-based test. There is no slash
command; the model invokes this when example-based tests leave a function's
invariants under-specified.

## When to reach for it

- The function has an algebraic law (round-trip, idempotence,
   commutativity, monotonicity) that examples only sample.
- The input space is large and edge cases keep slipping through hand-
   written cases.
- A parser/serialiser pair should satisfy `decode(encode(x)) == x`.

## Canonical procedure

1. State the invariant in one sentence ("encoding then decoding returns
   the input").
2. Pick the narrowest strategy that generates valid inputs; constrain it
   so generated values are in-domain rather than filtering after the fact.
3. Assert the law inside the test body; let the framework shrink failures
   to a minimal counter-example.
4. Keep one or two example-based tests alongside for the named boundary
   cases (empty, single, max-length) the contract calls out.
5. When a property fails, treat the shrunk counter-example as a new
   regression fixture before fixing the code.

## Guardrails

- Property tests complement, never replace, the boundary-case AND
   error-path tests every public function owes (test-discipline rule).
- Use `pytest.approx` for float laws and `numpy.testing.assert_allclose`
   for array laws.
- Name the test `test_<func>_<invariant>` so the law is legible from the
   test report.
"""

_EXTRACT_FUNCTION_BODY = """# extract-function

A model-only refactoring playbook. There is no slash command; the model
invokes this to pull a coherent block out of a long function into a named
helper.

## When to reach for it

- A function spans more than one screen and reads as several phases.
- A comment introduces a block ("# now normalise the rows") — the comment
   is begging to become a function name.
- The same computation appears in two places (DRY).

## Canonical procedure

1. Identify the block and the minimal set of variables it reads (inputs)
   and the single value it produces (output). If it produces two, that is
   two extractions.
2. Name the helper for WHAT it returns, not how — `normalised_rows`, not
   `do_step_two`.
3. Lift the block into a pure function where viable: explicit args in,
   explicit value out, no hidden state mutation.
4. Replace the original block with a call; keep the surrounding function's
   shape so the diff is reviewable.
5. Give the new helper full type hints, a docstring with a `Raises:` block
   if it can raise, and boundary + error-path tests if it is public.

## Guardrails

- Stop and reconsider if the helper needs four or more parameters — that
   often signals a missing value object, not a missing function.
- Three similar lines are fine; do not extract a one-line helper used once
   (YAGNI / KISS).
- Use named arguments once arity reaches three (explicit-over-implicit).
"""

_EXTRACT_MODULE_BODY = """# extract-module

A model-only refactoring playbook. There is no slash command; the model
invokes this when a single file has grown to host several unrelated
concerns that deserve their own module.

## When to reach for it

- One file mixes layers — schema models, business logic, and CLI glue —
   that the architecture keeps separate elsewhere.
- The file's imports fan out across many subpackages, signalling it does
   too much.
- Two clusters of functions never call each other and share no state.

## Canonical procedure

1. Draw the dependency graph inside the file: which functions call which,
   which share module-level state. Disjoint clusters are extraction
   candidates.
2. Choose the cluster with the fewest inbound edges from the rest of the
   file — it moves with the least churn.
3. Create the new module in the layer it belongs to (schema, logic, CLI)
   per the CLI-is-dispatch / separation-of-concerns rules.
4. Move the cluster, then fix imports; keep public names stable so callers
   outside the file change only their import path.
5. Re-export from the original module only if a published API depended on
   the old location; otherwise update call sites directly.

## Guardrails

- Respect the layering: CLI imports logic, logic imports schema, never the
   reverse.
- Avoid circular imports — if the split would create a cycle, the seam is
   wrong; find a different cluster.
- Each module keeps `from __future__ import annotations` and its own
   `logger = logging.getLogger(__name__)`.
"""

_GRADUATE_RESEARCH_CODE_BODY = """# graduate-research-code

A model-only playbook for promoting throwaway research/spike code into a
maintained module. There is no slash command; the model invokes this when
a notebook or scratch script has proven its idea and must now meet
production conventions.

## When to reach for it

- A spike under `.ea/local/` (or a notebook) demonstrated a result that a
   real feature now depends on.
- The exploratory code lacks types, tests, and error handling but the
   algorithm is settled.
- A research artifact is being promoted to `.ea/artifacts/` and its code
   needs to move out of scratch.

## Canonical procedure

1. Separate the kept algorithm from the exploratory scaffolding (plotting,
   ad-hoc prints, hard-coded paths). Only the algorithm graduates.
2. Re-home it into the proper package layer with `from __future__ import
   annotations`, full type hints, and module-level `logger`.
3. Replace inline constants and machine paths with parameters or config;
   scrub any PII or local paths before the code is committed.
4. Add the test debt the spike skipped: boundary cases, error paths, and
   `pytest.approx` / `assert_allclose` for any numerics.
5. Back every quantitative claim the graduated code makes with an audit-
   recorded artifact so the verify-before-claim rule holds.

## Guardrails

- A spike brief that ratifies a decision promotes from
   `.ea/local/research/` to `.ea/artifacts/research/` in the same commit
   that lands the decision (spike-workflow rule).
- Do not graduate code whose verdict is still open — promote the brief and
   the decision first.
- The graduated module obeys the same lint, type, and coverage gates as
   any other source file; no exceptions for "it was research".
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
        disable_model_invocation=False,
        body=_RESEARCH_BODY,
    ),
    SkillSpec(
        skill_name="prep",
        description=(
            "Activate the next PLANNED phase: surface its DAG for operator"
            " approval, then run the activate_phase hard gate and dispatch"
            " subagents per wave."
        ),
        argument_hint="<phase-id>",
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
        disable_model_invocation=False,
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
        disable_model_invocation=False,
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
            "Plan / revise / apply / drop / show PLANNED-scope phases on"
            " the eawf roadmap queue. Mutates state.json via the lifecycle"
            " transitions; one phase at a time."
        ),
        argument_hint="propose|revise|apply|drop|show <phase-id> [flags]",
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
        disable_model_invocation=False,
        body=_DIFFERENTIATE_BODY,
    ),
    SkillSpec(
        skill_name="flow",
        description=(
            "Run /research → /prep → /audit → /polish → /ship sequentially;"
            " review folds into /ship as the PR-review pass. Short-circuit"
            " on any non-ok status."
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
        disable_model_invocation=False,
        body=_BLITZ_BODY,
    ),
    SkillSpec(
        skill_name="coauthor",
        description="Resolve the Co-Authored-By trailer policy for the active repo.",
        argument_hint="[--mode=runtime|project|disabled]",
        user_invocable=True,
        disable_model_invocation=False,
        body=_COAUTHOR_BODY,
    ),
    SkillSpec(
        skill_name="memory",
        description="Save, list, or forget curated durable memory entries.",
        argument_hint="save|list|forget [<name>] [--tier=working|archival|retrieval]",
        user_invocable=True,
        disable_model_invocation=True,
        body=_MEMORY_BODY,
    ),
    SkillSpec(
        skill_name="agent-dispatch",
        description="Dispatch a claimed wave to a runtime per the V8 session-reuse ladder.",
        argument_hint="<wave-id> [--runtime=<id>]",
        user_invocable=True,
        disable_model_invocation=True,
        body=_AGENT_DISPATCH_BODY,
    ),
    SkillSpec(
        skill_name="compress",
        description="Compress the session conversation when context approaches the limit.",
        argument_hint="[--tokens-before=<n>] [--tokens-after=<n>] [--runtime=<id>]",
        user_invocable=True,
        disable_model_invocation=False,
        body=_COMPRESS_BODY,
    ),
    SkillSpec(
        skill_name="wave-spec",
        description="Scaffold or validate a WaveSpec deliverable for a claimed wave.",
        argument_hint="init|validate <wave-id> [--mockup-waiver-reason=<text>]",
        user_invocable=True,
        disable_model_invocation=True,
        body=_WAVE_SPEC_BODY,
    ),
    SkillSpec(
        skill_name="security-review",
        description="Run the security-audit DSL against a closed scope and emit findings.",
        argument_hint="--spec=<path> [--cwd=<dir>]",
        user_invocable=True,
        disable_model_invocation=False,
        body=_SECURITY_REVIEW_BODY,
    ),
    SkillSpec(
        skill_name="refactor-god-class",
        description=(
            "Model-only playbook for splitting a multi-responsibility class"
            " into single-purpose collaborators."
        ),
        argument_hint="",
        user_invocable=False,
        disable_model_invocation=False,
        body=_REFACTOR_GOD_CLASS_BODY,
    ),
    SkillSpec(
        skill_name="write-adr",
        description=(
            "Model-only playbook for drafting an architecture decision record"
            " companion to a typed decision row."
        ),
        argument_hint="",
        user_invocable=False,
        disable_model_invocation=False,
        body=_WRITE_ADR_BODY,
    ),
    SkillSpec(
        skill_name="add-property-test",
        description=(
            "Model-only playbook for adding a property-based test that pins a function's invariant."
        ),
        argument_hint="",
        user_invocable=False,
        disable_model_invocation=False,
        body=_ADD_PROPERTY_TEST_BODY,
    ),
    SkillSpec(
        skill_name="extract-function",
        description=(
            "Model-only refactoring playbook for pulling a coherent block out"
            " of a long function into a named helper."
        ),
        argument_hint="",
        user_invocable=False,
        disable_model_invocation=False,
        body=_EXTRACT_FUNCTION_BODY,
    ),
    SkillSpec(
        skill_name="extract-module",
        description=(
            "Model-only refactoring playbook for splitting a multi-concern file"
            " into layered modules."
        ),
        argument_hint="",
        user_invocable=False,
        disable_model_invocation=False,
        body=_EXTRACT_MODULE_BODY,
    ),
    SkillSpec(
        skill_name="graduate-research-code",
        description=(
            "Model-only playbook for promoting spike/research code into a"
            " typed, tested, maintained module."
        ),
        argument_hint="",
        user_invocable=False,
        disable_model_invocation=False,
        body=_GRADUATE_RESEARCH_CODE_BODY,
    ),
)


__all__ = [
    "SKILL_REGISTRY",
    "SkillSpec",
]
