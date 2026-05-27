---
name: blitz
description: Auto-chained research follow-up skill with recursion guard for residual unknowns.
argument-hint: "[--residual-unknowns=<n>]"
user-invocable: true
disable-model-invocation: false
---

# /blitz

## Canonical algorithm

1. Read residual unknown count and follow-up research args.
2. Increment the recursion guard (`EAWF_BLITZ_DEPTH_COUNTER`) against `EAWF_BLITZ_DEPTH` (default 8).
3. Return a follow-up `/research` action with `blitz=false` so the next research pass does not recurse indefinitely.

## Pre-flight checklist

- [ ] Confirm `/research` produced more than one residual unknown.
- [ ] Confirm recursion depth has not exceeded the configured cap.

## Output contract

Skill envelope with `header.skill = "/blitz"`. Body carries depth, depth_cap, residual_unknowns, followup_research_args, and next_actions.
