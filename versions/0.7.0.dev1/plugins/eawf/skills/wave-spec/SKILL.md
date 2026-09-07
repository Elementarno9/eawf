---
name: wave-spec
description: "Scaffold or validate a WaveSpec deliverable for a claimed wave."
argument-hint: "init|validate <wave-id> [--mockup-waiver-reason=<text>]"
user-invocable: true
disable-model-invocation: true
---

# /wave-spec

## Canonical algorithm

1. Resolve the verb (`init` default / `validate`) and the target `wave_id` (required; a missing id degrades to `status=needs_user`).
2. Thread the optional `mockup_waiver_reason` through so a downstream scaffold can carry it onto the WaveSpec without forcing an ASCII mockup for non-UI waves.
3. Append a single append-only `EVENT` describing the operation intent; the daemon owns spec scaffolding + cache mutation, so the skill routes to the `eawf spec` writer via `next_valid_actions`.

## Pre-flight checklist

- [ ] The wave exists before `init` / `validate`.
- [ ] A Mockup-waiver reason is supplied for non-UI waves that skip the ASCII mockup.

## Decision surfaces

A missing `wave_id` degrades to `status=needs_user`, which routes the operator to an `AskUserQuestion` prompt for the target wave rather than scaffolding against an unresolved id.

## Output contract

Skill envelope with `header.skill = "/wave-spec"`. Body carries verb, wave_id, and mockup_waiver_reason.
