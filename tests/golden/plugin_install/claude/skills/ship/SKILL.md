---
name: ship
description: Close out a phase by running the full local CI surface, opening the phase PR, and (after merge) advancing state.
argument-hint: "<phase-id> [--dry-run]"
user-invocable: true
disable-model-invocation: true
---

# /ship

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
