---
name: audit
description: Fresh-context verification of a phase deliverable or wave outcome. Spawns a fresh auditor subagent that re-reads the diff against the success criteria.
argument-hint: "<phase-id|wave-id|commit-range>"
user-invocable: true
disable-model-invocation: false
---

# /audit

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
