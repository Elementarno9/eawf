---
name: test
description: "Design, add, repair or run a bounded test contract."
argument-hint: "<design|add|repair|run> <target...> [--kind <unit|property|integration|golden|conformance|e2e>] [--invariant <text>] [--case <text>...] [--command <command>...] [--seed <N>] [--examples <N>] [--mode <inspect|apply>] [--budget <spec>] [--dry-run]"
user-invocable: true
disable-model-invocation: false
---

# /test

Design, add, repair or run a bounded test contract.

## 1. Authority

- An operator or an authorized agent may initiate this skill. Agent invocation never widens authority: it needs an enclosing Run, Task or Campaign scope whose compiled capsule already grants every read, write, RPC, budget and external effect below.
- Effects: Leased test-workspace edits and test execution; no canonical RPC.
- Allowed RPCs: none. This skill calls no daemon RPC.
- Canonical state: never mutated by this skill.
- Local write root: none.
- Executable grants come from the compiled capsule of the enclosing scope alone; nothing on this page adds or widens a tool, path, RPC, credential or external effect.

## 2. Context

The behavior named by `<target...>`, its source, call sites and public contract, and, in add or repair mode, the leased Task workspace that grants the write.

Resolve the subject before acting. Name every entity with its identifier and its exact current revision so staleness is detectable; a fact without a revision is a summary, not context.

## 3. Task

Choose and, when authorized, implement the cheapest oracle that can falsify the stated behavior.

```text
/test <design|add|repair|run> <target...> [--kind <unit|property|integration|golden|conformance|e2e>] [--invariant <text>] [--case <text>...] [--command <command>...] [--seed <N>] [--examples <N>] [--mode <inspect|apply>] [--budget <spec>] [--dry-run]
```

Select exactly one action: `design`, `add`, `repair`, `run`. An option the selected action does not declare is refused before you start.

## 4. Method

1. Read source, call sites, and public contract before selecting test kind.
2. For a defect, reproduce red on the pre-fix basis before accepting green. For a new public CLI, RPC, schema, lifecycle, or golden, establish the contract fixture first.
3. Cover required boundaries and error paths. Use property or metamorphic tests only for a named invariant or relation.
4. Avoid tests that merely mirror implementation and mocks that substitute interaction for truth. Golden updates require a paired diff and independent review.
5. Apply mode consumes an existing Task grant; strategy mode writes nothing. Run targeted commands and report unrelated failures separately.

## 4b. Applicable rules

The obligations the effective rule graph holds for activities `test`. They bind what you do; they grant no capability.

- must: **Name the command and exit status behind a claim.** Back every verification claim with the command executed and its exit status; a green claim whose run cannot be located in the receipt is a fabrication, not an oversight.
- must: **Finish independent parts when one part fails.** When part of the scope cannot be completed, complete every independent remaining part in full, then state plainly what was left undone and why.
- must: **Re-execute only what failed.** After a failure, re-execute only what failed, unless the change is cross-cutting, the artifact is a cross-scope scorecard, or a release gate needs a full pass.

## 5. Constraints

- Stop on ambiguous contract, unobservable oracle, unsafe fixture, nondeterminism that cannot be controlled, or any request to weaken/delete a valid existing test.
- Stopping is a valid outcome, not a failure: when the answer needs an operator or a precondition fails, return `blocked` with the reason rather than guessing.

## 6. Output

Output one TestSkillReport containing strategy, kind, oracle, files, red/green receipts where applicable, covered cases, unverified cases, commands, and outcome.

The report validates against `TestSkillReport`, and its terminal outcome is exactly one of `strategy_ready`, `tests_added`, `passed`, `failed`, `blocked`. Prose in the report is explanation, never the result.
