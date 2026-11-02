---
name: backlog
description: "Add or list draft Tasks and propose their promotion through a plan."
argument-hint: "<add|list|promote> [<task-ref>] [--intent <text>] [--priority <critical|high|normal|low>] [--to-batch <ref>] [--criterion <text>...] [--owner <path-selector>...] [--limit <N>] [--dry-run]"
user-invocable: true
disable-model-invocation: false
---

# /backlog

Add or list draft Tasks and propose their promotion through a plan.

## 1. Authority

- An operator or an authorized agent may initiate this skill. Agent invocation never widens authority: it needs an enclosing Run, Task or Campaign scope whose compiled capsule already grants every read, write, RPC, budget and external effect below.
- Operates on: Task, through `projection.backlog.read`, `eawf task create`, `eawf plan submit`.
- Effects: The backlog read, the draft Task create verb and the plan submission verb.
- Allowed RPCs: `projection.backlog.read`, `eawf task create` (`domain.task.create`), `eawf plan submit` (`planning.plan_revision.submit`). Any other RPC is denied before it reaches a handler. A host session drives a route through the `eawf` command named beside it; a route with no command beside it is driven by the daemon or a native Run.
- Canonical state changes only through those RPCs, and every mutating call carries `--expected-revision` and `--idempotency-key`.
- Local write root: none.
- Executable grants come from the compiled capsule of the enclosing scope alone; nothing on this page adds or widens a tool, path, RPC, credential or external effect.

## 2. Context

A draft Task is a real Task in DRAFT: it carries a key, a one-line intent, a priority, and a due scope — the Milestone, Batch, or Release by which it must be defined and executed. It carries no Batch, no criteria, and no ownership until it is promoted. The queue is the draft Tasks of the scope this invocation names.

Resolve the subject before acting. Name every entity with its identifier and its exact current revision so staleness is detectable; a fact without a revision is a summary, not context.

## 3. Task

You maintain the draft Task queue for one scope.

```text
/backlog <add|list|promote> [<task-ref>] [--intent <text>] [--priority <critical|high|normal|low>] [--to-batch <ref>] [--criterion <text>...] [--owner <path-selector>...] [--limit <N>] [--dry-run]
```

Select exactly one action: `add`, `list`, `promote`. An option the selected action does not declare is refused before you start.

## 4. Method

1. Read the queue and the scopes it references. An item whose due scope has passed is overdue and is surfaced first.
2. Add a draft as instructed: one line of intent, a priority, and the criteria and ownership claims already known. A draft with neither is still a real Task, and the report says what it lacks.
3. Promotion is a plan act, not a queue act. To promote, the Task needs criteria, ownership claims, and a Batch, and it goes through a PlanRevision. If those are missing, say what is missing rather than promoting a shell.
4. Report the queue's shape: counts by priority, what is overdue, and what has sat in the queue longest. A row nobody promotes is a decision nobody is making, and naming it is more useful than carrying it.

## 4b. Applicable rules

The obligations the effective rule graph holds for activities `plan`. They bind what you do; they grant no capability.

- must: **Give every activity a non-empty rule set.** Keep every activity governed by at least one rule; an activity whose effective rule set is empty is a defect, never a statement that no rules apply.
- must: **Scope conduct rules to the closed activity set.** Scope an activity-bound rule only to research, plan, design, implement, test, review, integrate, commit, release, deploy or operate; a rule scoped to any other activity is refused with the offending value named.
- must: **Finish independent work first and block only when proceeding is unsafe.** When part of the scope fails or awaits an answer, finish every independent part, state what was left undone and why, and raise a question only where the dependent work begins. Block only when proceeding on any assumption would be unsafe, or useless if wrong; unattended, record a typed open question and continue.
- must: **Dispatch independent units of work together.** Dispatch independent units of work together rather than in sequence, where neither consumes the other's output; a sequential dispatch of independent units is a recorded miss.
- must: **Evaluate the practice set at each decision point.** Retrieve the current activity's practice set with one query and evaluate it at each decision point rather than recalling it: at least before dispatching work, before presenting a choice to the operator and before emitting a terminal report.
- must: **Expand every abbreviation on first use.** Expand every abbreviation, internal code and lifecycle identifier on first use in an operator-facing surface; write for a competent newcomer, not for the author of the state.
- must: **Record a scope delta before widening the work.** Record a typed scope delta against the task before any widening work begins; an unrecorded widening fails review even when the added work is correct.
- must: **Surface a decision as a typed choice with named options.** Surface a decision as a typed choice with named options, never as free text, and give each option a plain-prose description of what happens if it is selected.

## 5. Constraints

- A draft is never dispatchable. Every dispatch guard requires a planned Task.
- Do not invent criteria to make a draft promotable. Missing criteria mean the work is not understood yet.
- Stopping is a valid outcome, not a failure: when the answer needs an operator or a precondition fails, return `blocked` with the reason rather than guessing.

## 6. Output

Output one report whose `summary` gives the queue after your changes, plus the overdue set and the longest-waiting set called out by name.

The report validates against `BacklogSkillReport`, and its terminal outcome is exactly one of `listed`, `added`, `promotion_proposed`, `blocked`. Prose in the report is explanation, never the result. Check it with `eawf skill check-report /backlog` before returning it. It carries these fields and no other; a nested field is written under its parent.

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `skill_id` | `"backlog"` | yes | The id of the skill that produced the report. |
| `outcome` | one of `"listed"`, `"added"`, `"promotion_proposed"`, `"blocked"` | yes | How the invocation ended; the result. |
| `summary` | string | no | Explanation of the outcome and what supports it; never the result. |
