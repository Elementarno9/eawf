---
name: agent-dispatch
description: "Dispatch a claimed wave to a runtime per the V8 session-reuse ladder."
argument-hint: "<wave-id> [--runtime=<id>]"
user-invocable: true
disable-model-invocation: true
---

# /agent-dispatch

## v0.4 cross-links

Dispatch resolves the wave's `RoleSpec` (`role`, `model`, `tools`, `isolation`) and renders the subagent's prompt from the wave's `agent_role` + the role's canonical contract. The dispatched session emits one `agent_end` report carrying an `EvidenceRecord` summary; the recorded evidence feeds the wave's `CloseReadiness`. When the operator pins a runtime preference, the choice persists via a `MEMORY` mutation (`MutationKind=update`) on the `Project` row so the next dispatch starts from the operator's pinned ladder.

## Canonical algorithm

1. Resolve the target `wave_id` (required; a missing id degrades to `status=needs_user`).
2. Read the `Wave.runtime_preference` ladder (or an explicit `runtime_preference` arg); an explicit `runtime` arg overrides the ladder head.
3. Surface the full ladder and the resolved head. No resolvable runtime is a soft `status=partial` (the dispatch can still proceed against the daemon default, but the operator can pin a preference).
4. The daemon's `agent.dispatch` RPC is the canonical mutator; the skill routes to `eawf wave dispatch` via `next_valid_actions`.

## Pre-flight checklist

- [ ] The wave is claimed before dispatch.
- [ ] The runtime ladder reflects how the planner sized the wave.

## Decision surfaces

A missing `wave_id` degrades to `status=needs_user`. When no runtime in the ladder resolves, the soft `status=partial` routes the operator to an `AskUserQuestion` prompt to pin a runtime preference rather than silently falling through to the daemon default.

## Output contract

Skill envelope with `header.skill = "/agent-dispatch"`. Body carries wave_id, runtime_preference, and resolved_runtime.
