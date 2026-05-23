---
name: memory
description: Save, list, or forget curated durable memory entries.
argument-hint: "save|list|forget [<name>] [--tier=working|archival|retrieval]"
user-invocable: true
disable-model-invocation: true
---

# /memory

## Canonical algorithm

1. Resolve the verb (`save` default / `list` / `forget`) and the target
   tier (`working` default / `archival` / `retrieval`).
2. A named verb (`save` / `forget`) without a `name` degrades to
   `status=needs_user`.
3. Append a single append-only `EVENT` describing the operation intent;
   the daemon is the sole canonical writer of the memory JSONL store, so
   the skill routes the operator to the `eawf memory` writer via
   `next_valid_actions` rather than mutating the store itself.

## Pre-flight checklist

- [ ] The skill records intent only — the daemon owns the store write.
- [ ] `save` / `forget` carry a memory entry name.

## Decision surfaces

A named verb (`save` / `forget`) without a `name` degrades to
`status=needs_user`, which routes the operator to an `AskUserQuestion`
prompt for the missing entry name rather than inventing one.

## Output contract

Skill envelope with `header.skill = "/memory"`. Body carries verb, name,
and tier (or a reason on the needs_user path).
