---
name: roadmap
description: Plan / revise / apply / drop / show PLANNED-scope phases on the eawf roadmap queue. Mutates state.json via the lifecycle transitions; one phase at a time.
argument-hint: "propose|revise|apply|drop|show <phase-id> [flags]"
user-invocable: true
disable-model-invocation: true
---

# /roadmap

## Canonical algorithm

1. **`propose`** stages a new PLANNED phase + its `P##-I01` iter on
   the queue without any waves yet. Emits a `needs_user` envelope
   with the rendered plan text — the active runtime (Claude
   plan-mode, Codex text-prompt) surfaces it for operator approval.
2. **`revise`** edits the PLANNED scope via structured flags:
   `--add-wave`, `--remove-wave`, `--set-deps`, `--retitle`.
   Wave-level mutations route through the P19-W01 PENDING-only
   transitions.
3. **`apply`** is the post-propose confirmation step. It validates
   that the phase is PLANNED with at least one wave and emits an
   `ok` envelope; the actual planning is already persisted (propose
   does the state mutation). Use it as the handoff into `/prep`.
4. **`drop`** archives a PLANNED phase (PLANNED → ARCHIVED) when
   the operator rejects the proposed plan.
5. **`show`** renders the queue: text table (default), markdown
   (`--md`), or JSON envelope (`--json`).

## Pre-flight checklist

- [ ] State CLI is the only mutator; `state.json` writes happen
      inside `state_transaction` so the sibling lock is held.
- [ ] Brief ids passed via `--from-briefs` should be promoted
      research artefacts (RES-YYYY-MM-DD-NNN).
- [ ] One phase at a time. Bulk-propose is deferred.

## Decision surfaces

`roadmap propose` is the single decision surface — its envelope
status is `needs_user`. The runtime adapter (Claude / Codex /
OpenCode) maps the envelope's `decision_kind=approve_plan` body to
its native confirm UI. `revise`, `apply`, and `drop` emit `ok`
envelopes — operator has already approved via propose (or is
walking back via drop).

## Output contract

`status=needs_user` envelope for `propose` (carries `plan_text` +
`options`). `status=ok` envelope for `revise`, `apply`, `drop`,
`show` — body shape varies per command. JSON envelope is the
machine surface; the default text render is for terminal use.
