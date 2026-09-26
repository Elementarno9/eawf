---
name: why
description: "Explain the provenance of an entity; read-only."
argument-hint: "<subject-ref> [--at <revision-or-time>] [--depth <summary|chain|full>] [--include <decisions|events|evidence|receipts>...] [--format <text|graph|json>] [--verify-links]"
user-invocable: true
disable-model-invocation: false
---

# /why

Explain the provenance of an entity; read-only.

## 1. Authority

- An operator or an authorized agent may initiate this skill. Agent invocation never widens authority: it needs an enclosing Run, Task or Campaign scope whose compiled capsule already grants every read, write, RPC, budget and external effect below.
- Effects: Read-only provenance queries.
- Allowed RPCs: `read_entity`, `query_evidence`, `semantic.result.read`, `run.events.read`, `operation.status`. Any other RPC is denied before it reaches a handler.
- Canonical state: never mutated by this skill.
- Local write root: none.
- Executable grants come from the compiled capsule of the enclosing scope alone; nothing on this page adds or widens a tool, path, RPC, credential or external effect.

## 2. Context

One canonical fact, action, or state, named by `<subject-ref>`, at the revision or time `--at` names, or at its current revision when `--at` is omitted.

Resolve the subject before acting. Name every entity with its identifier and its exact current revision so staleness is detectable; a fact without a revision is a summary, not context.

## 3. Task

Explain why one canonical fact, action, or state exists. This skill is read-only.

```text
/why <subject-ref> [--at <revision-or-time>] [--depth <summary|chain|full>] [--include <decisions|events|evidence|receipts>...] [--format <text|graph|json>] [--verify-links]
```

## 4. Method

1. Bind the exact subject and requested revision or time.
2. Walk receipts, Decisions, Plans, claims, evidence, events, and supersession links backward within the requested depth and node cap.
3. Separate recorded fact, cited claim, and inference. Prefer active rationale while showing superseded history when requested.
4. Detect cycles and broken references. Never infer intent from timestamps or prose when durable links are absent; name the gap.
5. Stop at the root cause, requested depth, unresolved reference, cycle, or cap.

## 4b. Applicable rules

No rule in the effective rule graph is scoped to this skill, which selects no activity or role; the repository policy still binds every session.

## 5. Constraints

- Stopping is a valid outcome, not a failure: when the answer needs an operator or a precondition fails, return `blocked` with the reason rather than guessing.

## 6. Output

Output one WhyReport containing answer, subject revision, provenance path, nodes, active-versus-historical distinctions, gaps, references, coverage, and stop reason.

The report validates against `WhyReport`, and its terminal outcome is exactly one of `explained`, `partial`, `not_found`, `blocked`. Prose in the report is explanation, never the result.
