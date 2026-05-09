---
name: prep
description: Open the next phase or wave by writing a work plan and dispatching subagents for execution.
argument-hint: "<phase-id> [wave-id]"
user-invocable: true
disable-model-invocation: true
---

# /prep

## Canonical algorithm

1. Resolve `<phase-id>` against the plan / state.
2. Enumerate waves; mark each `parallel | sequential` per the plan.
3. For each parallel wave, dispatch a worktree subagent.
4. For each sequential wave, run inline; cherry-pick parallel-wave
   commits in between as they finish.
5. Update plan checkboxes / state via `eawf state phase advance`.

## Pre-flight checklist

- [ ] Confirm current branch is the long-running phase branch.
- [ ] Confirm `git status` is clean.
- [ ] Confirm worktree subagents branch from the parent HEAD.

## Output contract

Skill envelope describing the dispatched waves and the expected
cherry-pick order.
