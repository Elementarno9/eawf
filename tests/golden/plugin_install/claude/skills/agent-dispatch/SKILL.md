---
name: agent-dispatch
description: Dispatch a claimed wave to a runtime per the V8 session-reuse ladder.
argument-hint: "<wave-id> [--runtime=<id>]"
user-invocable: true
disable-model-invocation: true
---

# /agent-dispatch

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

## Output contract

Skill envelope with `header.skill = "/agent-dispatch"`. Body carries
wave_id, runtime_preference, and resolved_runtime.
