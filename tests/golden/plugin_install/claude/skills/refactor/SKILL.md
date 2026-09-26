---
name: refactor
description: "Inspect or apply a bounded structural refactor."
argument-hint: "<target...> [--pattern <extract-function|extract-module|split-class|graduate|custom>] [--goal <text>] [--mode <inspect|apply>] [--include <selector>...] [--exclude <selector>...] [--constraint <text>...] [--test <command>...] [--budget <spec>] [--dry-run]"
user-invocable: true
disable-model-invocation: false
---

# /refactor

Inspect or apply a bounded structural refactor.

## 1. Authority

- An operator or an authorized agent may initiate this skill. Agent invocation never widens authority: it needs an enclosing Run, Task or Campaign scope whose compiled capsule already grants every read, write, RPC, budget and external effect below.
- Effects: Leased workspace edits only in apply mode; no canonical RPC.
- Allowed RPCs: none. This skill calls no daemon RPC.
- Canonical state: never mutated by this skill.
- Local write root: none.
- Executable grants come from the compiled capsule of the enclosing scope alone; nothing on this page adds or widens a tool, path, RPC, credential or external effect.

## 2. Context

The code named by `<target...>`, its call sites and public contracts, and, in apply mode, the leased Task workspace whose write scope covers it.

Resolve the subject before acting. Name every entity with its identifier and its exact current revision so staleness is detectable; a fact without a revision is a summary, not context.

## 3. Task

Improve the selected structure without changing observable behavior. Apply mode consumes an existing Task write grant; this skill never creates authority.

```text
/refactor <target...> [--pattern <extract-function|extract-module|split-class|graduate|custom>] [--goal <text>] [--mode <inspect|apply>] [--include <selector>...] [--exclude <selector>...] [--constraint <text>...] [--test <command>...] [--budget <spec>] [--dry-run]
```

## 4. Method

1. Inspect actual call sites, ownership, public contracts, and local changes. State the preserved behavior and target structural boundary.
2. Establish characterization or contract coverage before edits when apply mode is authorized.
3. Choose the smallest fitting pattern and make cohesive steps. Preserve public names and schemas unless the enclosing Task explicitly permits change.
4. Run targeted checks after each step and the affected suite at the end. Keep unrelated work untouched and avoid opportunistic cleanup.
5. In inspect mode, return the plan without edits. In apply mode, stop if the change requires new behavior, migration, expanded ownership, or undeclared files.

## 4b. Applicable rules

The obligations the effective rule graph holds for activities `implement`. They bind what you do; they grant no capability.

- must: **Block on a question only when proceeding is unsafe.** Raise a blocking question only when proceeding under any available assumption would be unsafe or would make the completed work useless if the assumption proved wrong; resolve every other uncertainty by assumption plus disclosure.
- must: **Dispatch independent units of work together.** Dispatch independent units of work together rather than in sequence, where neither consumes the other's output; a sequential dispatch of independent units is a recorded miss.
- must: **Evaluate the practice set at each decision point.** Evaluate the applicable practice set at each decision point rather than recalling it: at least before dispatching work, before presenting a choice to the operator and before emitting a terminal report.
- must: **Stop only at operator-declared checkpoints.** Stop at a checkpoint the operator declared and at no other point; a self-selected pause is a deviation.
- must: **Finish independent parts when one part fails.** When part of the scope cannot be completed, complete every independent remaining part in full, then state plainly what was left undone and why.
- must: **Record the goal before the first mutating action.** Before the first mutating action, record the task as a typed goal bound to the declared success criteria of the entity it serves.
- must: **Add a helper only with a present-day caller.** Add a helper, wrapper or indirection only with a production caller in the same change. Tolerate duplication at two call sites and extract at the third; a helper whose only caller is a test fails this rule.
- must: **State the concern once and finish the task.** When a task looks ill-specified, state the concern once, record the assumption you proceed under and complete the work; do not halt and do not silently substitute your own reading.
- must: **Finish independent work before raising a question.** Before raising a question, complete all work that does not depend on its answer, and raise it only where the dependent work begins.
- must: **Add a parameter or flag only with a consumer.** Add a parameter, configuration leaf, feature flag or extension point only with a consumer in the same change; a knob shipped for a hypothetical future caller is rejected.
- must: **Guard only states reachable on real call paths.** Add no defensive branch, fallback or validation for a state unreachable on the real call paths. Where reachability is genuinely uncertain, record the uncertainty instead of coding around it.
- must: **Keep unrequested refactors out of the delivery.** Carry no unrequested refactor in a change; record improvements found in passing as backlog rows against the touched paths, never in the delivery commit.
- must: **Retrieve the current practice set in one step.** Retrieve the practice set for the current activity with one query at each decision point instead of remembering which practices apply.
- must: **State each claim once across rendered prose.** Hold prose to the same budget as code: a rendered artifact states its claim once, and restating a requirement in a second file is duplicate ownership, not emphasis.
- must: **Record a scope delta before widening the work.** Record a typed scope delta against the task before any widening work begins; an unrecorded widening fails review even when the added work is correct.
- must: **Record an open question when running unattended.** In an unattended run, record a typed open question against the entity and continue on the independent remainder; never halt to await an operator who is not present.
- must: **Validate once at the boundary where data enters.** Validate untyped data once, at the boundary where it enters; downstream functions accept validated typed objects and do not re-check them.

## 5. Constraints

- Observable behavior after the change equals observable behavior before it; a change that needs new behavior belongs to a Task, not to this skill.
- Stopping is a valid outcome, not a failure: when the answer needs an operator or a precondition fails, return `blocked` with the reason rather than guessing.

## 6. Output

Output one RefactorReport containing baseline, invariant, chosen pattern, files, declared contract changes, verification receipts, residual risks, and terminal outcome.

The report validates against `RefactorReport`, and its terminal outcome is exactly one of `plan_ready`, `applied`, `verified`, `failed`, `blocked`. Prose in the report is explanation, never the result.
