---
name: polish
description: Repo-wide consistency sweep. Aligns naming, docstring style, log fields, error message phrasing, and removes dead code.
argument-hint: "[--scope=<dir|file>]"
user-invocable: true
disable-model-invocation: true
---

# /polish

## Canonical algorithm

1. Resolve scope: default = entire `src/eawf/`; `--scope=<dir|file>` narrows.
2. Sweep checks: naming, docstrings, log fields, error message phrasing, dead code, citation density, draft sentinels, scrub status.
3. Apply fixes inline. If a change touches public API, stop and ask.

## Pre-flight checklist

- [ ] Scope is declared and bounded.
- [ ] No public API rename without explicit user confirmation.
- [ ] The target iter is NOT yet closed. Iter close is gated on `audit + polish + ship CI + PR review pass` per the `iter-phase-close-timing` rule in AGENTS.md; `/polish` runs after `/audit` and before that close.

## Decision surfaces

Public-API renames, dead-code deletions, and anything matching `polish.deletion_policy` MUST be raised via `AskUserQuestion` (options: `apply` / `defer-to-backlog` / `skip`) instead of asking in free text. `polish.auto_apply_safe=true` bypasses the prompt for the small "safe" subset only (formatting, comment phrasing).

## Output contract

Skill envelope with a change list grouped by category and a deferred- items list for changes needing user OK.
