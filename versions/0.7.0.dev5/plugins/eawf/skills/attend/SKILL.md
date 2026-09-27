---
name: attend
description: "Work the Attention queue: pending actions, open questions and open pauses."
argument-hint: "[<list|prepare|resolve|defer|watch>] [<item-ref>] [--kind <action|question|pause>] [--urgency <watch|normal|high|urgent>] [--option <id>] [--answer <text>] [--until <datetime>] [--limit <N>] [--scope <urn>] [--dry-run]"
user-invocable: true
disable-model-invocation: true
---

# /attend

Work the Attention queue: pending actions, open questions and open pauses.

## 1. Authority

- An operator or an authorized agent may initiate this skill. Agent invocation never widens authority: it needs an enclosing Run, Task or Campaign scope whose compiled capsule already grants every read, write, RPC, budget and external effect below.
- Operator-only actions: `resolve`. An agent that reaches one prepares a PendingAction and stops; it never chooses the recommended option itself.
- Effects: Attention read and preparation plus the protected resolution RPC when explicitly selected.
- Allowed RPCs: `read_entity`, `query_evidence`, `operations.pending_action.resolve`, `operations.pending_action.hold`, `operations.pending_action.cancel`, `research.question.answer`, `research.question.drop`, `operations.pause.hold`, `operations.pause.resume`, `operations.pause.cancel`. Any other RPC is denied before it reaches a handler.
- Canonical state changes only through those RPCs, and every mutating call carries `--expected-revision` and `--idempotency-key`.
- Local write root: none.
- Executable grants come from the compiled capsule of the enclosing scope alone; nothing on this page adds or widens a tool, path, RPC, credential or external effect.

## 2. Context

The Attention queue of the scope named by `--scope`, or of the current project when it is omitted, and the item named by `<item-ref>` when one is given.

Resolve the subject before acting. Name every entity with its identifier and its exact current revision so staleness is detectable; a fact without a revision is a summary, not context.

## 3. Task

You triage the Attention queue for one scope and prepare each item for the operator. You resolve nothing that requires their authority.

```text
/attend [<list|prepare|resolve|defer|watch>] [<item-ref>] [--kind <action|question|pause>] [--urgency <watch|normal|high|urgent>] [--option <id>] [--answer <text>] [--until <datetime>] [--limit <N>] [--scope <urn>] [--dry-run]
```

Select exactly one action: `list`, `prepare`, `resolve`, `defer`, `watch`. An option the selected action does not declare is refused before you start.

## 4. Method

1. Read the queue. Three kinds live here and they are not interchangeable: a PendingAction is a request for an effect; an OpenQuestion is a knowledge gap; an OpenPause is an operational condition the daemon observed.
2. For each item, establish what the operator needs in order to answer: the exact subject and revision, the options with their consequences, the evidence behind each, and what happens if they do nothing.
3. Order by urgency and deadline, not by arrival. Interrupt-now first, then decisions that are due, then invalidated proofs, then recovery, then watch.
4. Where an item is answerable from evidence rather than authority — a question whose answer already exists in the claim ledger — answer it and record the evidence rather than spending operator attention.
5. Present what remains. Each option carries a rendering of what choosing it produces, every term expanded, one recommendation, and the recommendation is the durable choice rather than the cheap one.

## 4b. Applicable rules

The obligations the effective rule graph holds for activities `operate`. They bind what you do; they grant no capability.

- must: **Record each conduct deviation as a typed local row.** Record every conduct deviation as a typed row bound to the run that produced it and the obligation it breached, with how it was detected, its severity, an evidence reference and its disposition, in the machine-local store and never in the committed surface.
- must: **Record the goal before the first mutating action.** Before the first mutating action, record the task as a typed goal bound to the declared success criteria of the entity it serves.
- must: **Record a runtime cap only as a measured value.** Record a runtime's declared cap as a measured value with its measurement provenance, never as a figure chosen for headroom; report a cap not re-measured against a certified runtime version as stale.
- must: **Record a missed practice as a conduct deviation.** Record a practice whose trigger fired and whose obligation was not met as a conduct deviation, so recall failure is counted per rule and per runtime.
- must: **Record an open question when running unattended.** In an unattended run, record a typed open question against the entity and continue on the independent remainder; never halt to await an operator who is not present.

## 5. Constraints

- You never resolve a protected action, and you never let silence stand as consent to an irreversible effect.
- You never merge the three ledgers. A question rendered as an action, or a pause rendered as a question, loses the distinction the operator needs.
- If an item has no consequence either way, close it rather than asking.
- Stopping is a valid outcome, not a failure: when the answer needs an operator or a precondition fails, return `needs_operator` or `blocked` with the reason rather than guessing.

## 6. Output

A prepared attention set: per item, its kind, subject, exact revision, options with consequences, evidence, recommendation, and what happens on no answer.

The report validates against `AttentionSkillReport`, and its terminal outcome is exactly one of `listed`, `prepared`, `resolved`, `deferred`, `watching`, `needs_operator`, `blocked`. Prose in the report is explanation, never the result.
