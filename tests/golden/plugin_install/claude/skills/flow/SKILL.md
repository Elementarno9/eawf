---
name: flow
description: Run /research → /prep → /audit → /ship → /review → /polish sequentially; short-circuit on any non-ok status.
argument-hint: "<task-slug>"
user-invocable: true
disable-model-invocation: true
---

# /flow

## Canonical algorithm

1. Run `/research` → `/prep` → `/audit` → `/ship` → `/review` →
   `/polish` sequentially.
2. On any non-`ok` status, short-circuit with the failing step's
   repair commands.

## Pre-flight checklist

- [ ] All upstream skills are installed.

## Output contract

Skill envelope whose body accumulates per-step envelopes; status is
`ok` when every step passed, otherwise the first non-`ok` step's
status is propagated.
