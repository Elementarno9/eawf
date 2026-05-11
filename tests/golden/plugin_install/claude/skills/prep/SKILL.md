---
name: prep
description: Open the next phase or wave by writing a work plan and dispatching subagents for execution.
argument-hint: "<phase-id> [wave-id] [--auto-plan]"
user-invocable: true
disable-model-invocation: true
---

# /prep

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
