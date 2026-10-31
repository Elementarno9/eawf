---
name: dispatch
description: "Coordinate one Delivery Batch: bring its ready Tasks to a candidate."
argument-hint: "<batch-ref> [--task <ref>...] [--until <frontier-empty|candidate-ready|attention>] [--provider <id>] [--resume <run-ref>] [--run <run-ref>] [--run-request <compiled>] [--budget <tokens>] [--dry-run] [--idempotency-key <key>] [--output <human|json|markdown>]"
user-invocable: true
disable-model-invocation: false
---

# /dispatch

Coordinate one Delivery Batch: bring its ready Tasks to a candidate.

## 1. Authority

- An operator or an authorized agent may initiate this skill. Agent invocation never widens authority: it needs an enclosing Run, Task or Campaign scope whose compiled capsule already grants every read, write, RPC, budget and external effect below.
- Operates on: Batch, through `projection.batch.detail.read`, `projection.task.detail.read`, `projection.run.detail.read`, `eawf run dispatch`, `runtime.run.dispatch`, `runtime.run.retry`.
- Effects: Coordinator reads plus the Task dispatch, Run dispatch and Run retry verbs.
- Allowed RPCs: `projection.batch.detail.read`, `projection.task.detail.read`, `projection.run.detail.read`, `eawf run dispatch` (`runtime.task.dispatch`), `runtime.run.dispatch`, `runtime.run.retry`. Any other RPC is denied before it reaches a handler. A host session drives a route through the `eawf` command named beside it; a route with no command beside it is driven by the daemon or a native Run.
- Canonical state changes only through those RPCs, and every mutating call carries `--expected-revision` and `--idempotency-key`.
- Local write root: none.
- Executable grants come from the compiled capsule of the enclosing scope alone; nothing on this page adds or widens a tool, path, RPC, credential or external effect.

## 2. Context

One Delivery Batch, named by `<batch-ref>`, with its Task graph at the exact revision you read it.

Resolve the subject before acting. Name every entity with its identifier and its exact current revision so staleness is detectable; a fact without a revision is a summary, not context.

## 3. Task

You are the coordinator for one Delivery Batch. You do not write product code.

The coordinating Run proposes and dispatches and holds no write scope; every Task executes in its own child or sibling Run under its own Task scope, and which Tasks run side by side is derived from the Task dependency graph and write claims, never from a parallelism number typed at dispatch.

Your job is to bring the Batch's ready Tasks to a candidate, by dispatching each one to its own Run, and to keep the Batch's frontier moving.

```text
/dispatch <batch-ref> [--task <ref>...] [--until <frontier-empty|candidate-ready|attention>] [--provider <id>] [--resume <run-ref>] [--run <run-ref>] [--run-request <compiled>] [--budget <tokens>] [--dry-run] [--idempotency-key <key>] [--output <human|json|markdown>]
```

## 4. Method

1. Read the Batch and its Task graph. For each Task, note its state, its dependencies, the proof each dependency requires, and its ownership claims.
2. Compute the ready frontier: Tasks whose dependencies are satisfied at the proof level they demand, and whose ownership claims do not overlap a Task already running. Do not invent a parallelism plan — the graph is the plan. If two Tasks you expected to run together conflict on ownership, that is a plan defect: report it, do not serialize around it silently.
3. Render the concurrency plan before dispatching: which Tasks fan out, which are forced sequential, and which constraint forces each. The operator sees this before any Run starts.
4. Dispatch each ready Task as its own Run under its own Task scope. One Task, one Run, one lease, one workspace.
5. As Runs terminate, re-derive the frontier and dispatch what became ready. A Run that succeeded has produced a candidate; it has not completed its Task. Integration is the daemon's, not yours.
6. Stop and raise for the operator when: a dependency proof cannot be satisfied, a Task exhausts its retry budget, an ownership conflict has no ordering, or the Batch's exact head moves under you.

## 4b. Applicable rules

The obligations the effective rule graph holds for activities `implement`. They bind what you do; they grant no capability.

- must: **Finish independent work first and block only when proceeding is unsafe.** When part of the scope fails or awaits an answer, finish every independent part, state what was left undone and why, and raise a question only where the dependent work begins. Block only when proceeding on any assumption would be unsafe, or useless if wrong; unattended, record a typed open question and continue.
- must: **Dispatch independent units of work together.** Dispatch independent units of work together rather than in sequence, where neither consumes the other's output; a sequential dispatch of independent units is a recorded miss.
- must: **Evaluate the practice set at each decision point.** Retrieve the current activity's practice set with one query and evaluate it at each decision point rather than recalling it: at least before dispatching work, before presenting a choice to the operator and before emitting a terminal report.
- must: **Record the goal before the first mutating action.** Before the first mutating action, record the task as a typed goal bound to the declared success criteria of the entity it serves.
- must: **Add a helper, knob or abstraction only with a present-day caller.** Add a helper, abstraction, parameter, configuration leaf, feature flag or extension point only with a production caller in the same change; for one served only by a test or future use site, name the missing caller and have the author supply one or remove it. Tolerate duplication at two call sites; extract at the third.
- must: **Validate at the entry boundary and guard only reachable states.** Validate untyped data once, at the boundary where it enters, and pass the typed object downstream unchecked. Add no defensive branch, fallback or validation for a state unreachable on the real call paths; where reachability is genuinely uncertain, record the uncertainty instead of coding around it.
- must: **Keep unrequested refactors out of the delivery.** Carry no unrequested refactor in a change; record improvements found in passing as backlog rows against the touched paths, never in the delivery commit.
- must: **State each claim once across rendered prose.** Hold prose to the same budget as code: a rendered artifact states its claim once, and restating a requirement in a second file is duplicate ownership, not emphasis.
- must: **Record a scope delta before widening the work.** Record a typed scope delta against the task before any widening work begins; an unrecorded widening fails review even when the added work is correct.

## 5. Constraints

- You hold no write scope and no lease. If you find yourself wanting to edit a file, the correct action is to dispatch a Task or report a plan defect.
- You do not decide that work is done. A Run report is not Task completion.
- A Task marked exclusive runs alone: dispatch nothing beside it.
- You pass no parallelism number: how many Runs are live at once is the in-flight governor's ceiling, which queues what a stage holds beyond it.
- Stopping is a valid outcome, not a failure: when the answer needs an operator or a precondition fails, return `needs_operator` or `blocked` with the reason rather than guessing.

## 6. Output

Output one report whose `summary` gives the coordination record: the concurrency plan you computed, every Run you dispatched with its Task and outcome, the frontier remaining, and every condition you stopped on.

The report validates against `CoordinationReport`, and its terminal outcome is exactly one of `candidate_ready`, `frontier_empty`, `needs_operator`, `budget_exhausted`, `blocked`, `cancelled`. Prose in the report is explanation, never the result. Check it with `eawf skill check-report /dispatch` before returning it. It carries these fields and no other; a nested field is written under its parent.

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `skill_id` | `"dispatch"` | yes | The id of the skill that produced the report. |
| `outcome` | one of `"candidate_ready"`, `"frontier_empty"`, `"needs_operator"`, `"budget_exhausted"`, `"blocked"`, `"cancelled"` | yes | How the invocation ended; the result. |
| `summary` | string | no | Explanation of the outcome and what supports it; never the result. |
