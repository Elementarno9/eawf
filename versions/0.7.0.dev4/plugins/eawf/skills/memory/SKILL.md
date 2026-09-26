---
name: memory
description: "Search, write, promote or forget memory entries."
argument-hint: "<search|write|promote|forget|show> [<query-or-memory-ref>...] [--scope <urn>] [--kind <fact|preference|procedure|warning|summary>] [--content <text>] [--evidence <ref>...] [--tag <text>...] [--expires <datetime|never>] [--limit <N>] [--dry-run]"
user-invocable: true
disable-model-invocation: true
---

# /memory

Search, write, promote or forget memory entries.

## 1. Authority

- An operator or an authorized agent may initiate this skill. Agent invocation never widens authority: it needs an enclosing Run, Task or Campaign scope whose compiled capsule already grants every read, write, RPC, budget and external effect below.
- Operator-only actions: `promote`, `forget`. An agent that reaches one prepares a PendingAction and stops; it never chooses the recommended option itself.
- Effects: Memory RPCs; evidence queries only for promotion.
- Allowed RPCs: `memory.search`, `memory.write`, `memory.promote`, `memory.forget`, `memory.get`, `query_evidence`. Any other RPC is denied before it reaches a handler.
- Canonical state changes only through those RPCs, and every mutating call carries `--expected-revision` and `--idempotency-key`.
- Local write root: none.
- Executable grants come from the compiled capsule of the enclosing scope alone; nothing on this page adds or widens a tool, path, RPC, credential or external effect.

## 2. Context

The memory rows of the scope named by `--scope`, or the row named by its reference, with the retention policy and injection budget that govern them.

Resolve the subject before acting. Name every entity with its identifier and its exact current revision so staleness is detectable; a fact without a revision is a summary, not context.

## 3. Task

Perform one explicit memory operation. Memory stores durable context; it does not duplicate current canonical state.

```text
/memory <search|write|promote|forget|show> [<query-or-memory-ref>...] [--scope <urn>] [--kind <fact|preference|procedure|warning|summary>] [--content <text>] [--evidence <ref>...] [--tag <text>...] [--expires <datetime|never>] [--limit <N>] [--dry-run]
```

Select exactly one action: `search`, `write`, `promote`, `forget`, `show`. An option the selected action does not declare is refused before you start.

## 4. Method

1. Resolve action, scope, existing rows, evidence, retention policy, and injection budget.
2. Search returns compatible active rows ranked by relevance and freshness, labels stale entries, and discloses omitted count.
3. Write admits only context likely useful next session and not cheaply derivable from canonical state. Reject secrets, machine-local identifiers, transcripts, current blockers, and duplicated lifecycle facts.
4. Promote requires resolving evidence, deduplication, review date, and protected authority. Forget preserves a tombstone/reason according to retention policy rather than silently erasing provenance.
5. Submit the selected memory RPC and return the durable receipt. Never let memory content override a newer canonical fact.

## 4b. Applicable rules

No rule in the effective rule graph is scoped to this skill, which selects no activity or role; the repository policy still binds every session.

## 5. Constraints

- Stop on forbidden content, duplicate/conflict, stale revision, missing promotion evidence, or injection-budget overflow.
- Stopping is a valid outcome, not a failure: when the answer needs an operator or a precondition fails, return `blocked` with the reason rather than guessing.

## 6. Output

Output one MemorySkillReport containing results or changed row, freshness, evidence, dedup disposition, receipt, and blockers.

The report validates against `MemorySkillReport`, and its terminal outcome is exactly one of `listed`, `shown`, `written`, `promoted`, `forgotten`, `blocked`. Prose in the report is explanation, never the result.
