---
name: polish
description: Repo-wide consistency sweep. Aligns naming, docstring style, log fields, error message phrasing, and removes dead code.
argument-hint: "[--scope=<dir|file>]"
user-invocable: true
disable-model-invocation: true
---

# /polish

## Canonical algorithm

1. Resolve scope: default = entire `src/eawf/`; `--scope=<dir|file>`
   narrows.
2. Sweep checks: naming, docstrings, log fields, error message
   phrasing, dead code.
3. Apply fixes inline. If a change touches public API, stop and ask.

## Pre-flight checklist

- [ ] Scope is declared and bounded.
- [ ] No public API rename without explicit user confirmation.

## Output contract

Skill envelope with a change list grouped by category and a deferred-
items list for changes needing user OK.
