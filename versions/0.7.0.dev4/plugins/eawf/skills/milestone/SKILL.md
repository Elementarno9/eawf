---
name: milestone
description: "Define, activate, revise, repair or cancel a Milestone."
argument-hint: "<define|show|activate|revise|repair|cancel> [<milestone-ref>] [--track <ref>] [--title <text>] [--outcome <text>] [--appetite <duration>] [--exclude <text>...] [--journey-step <text>...] [--batch <ref>...] [--reason <text>] [--from-spec <path|->] [--dry-run]"
user-invocable: true
disable-model-invocation: true
---

# /milestone

Define, activate, revise, repair or cancel a Milestone.

## 1. Authority

- Only an authenticated operator initiates this skill. An agent may prepare evidence or recommend the invocation, but never calls it.
- Effects: Milestone RPCs.
- Allowed RPCs: `read_entity`, `domain.milestone.create`, `domain.milestone.activate`, `domain.milestone.revise`, `domain.milestone.repair`, `domain.milestone.cancel`. Any other RPC is denied before it reaches a handler.
- Canonical state changes only through those RPCs, and every mutating call carries `--expected-revision` and `--idempotency-key`.
- Local write root: none.
- Executable grants come from the compiled capsule of the enclosing scope alone; nothing on this page adds or widens a tool, path, RPC, credential or external effect.

## 2. Context

One Milestone, named by `<milestone-ref>` or defined by this invocation, within its Track.

Resolve the subject before acting. Name every entity with its identifier and its exact current revision so staleness is detectable; a fact without a revision is a summary, not context.

## 3. Task

You operate one Milestone through the selected define, show, activate, revise, repair, or cancel action.

```text
/milestone <define|show|activate|revise|repair|cancel> [<milestone-ref>] [--track <ref>] [--title <text>] [--outcome <text>] [--appetite <duration>] [--exclude <text>...] [--journey-step <text>...] [--batch <ref>...] [--reason <text>] [--from-spec <path|->] [--dry-run]
```

Select exactly one action: `define`, `show`, `activate`, `revise`, `repair`, `cancel`. An option the selected action does not declare is refused before you start.

## 4. Method

1. Resolve its Track, exact revision, outcome, appetite, exclusions, acceptance journey, repository set, and required Batches.
2. For define or revise, make the outcome observable and the acceptance journey executable. Keep exclusions explicit. Never infer missing scope merely to make the contract complete.
3. For activate, require an approved current contract, satisfiable repository ownership, no blocking policy conflict, and a valid planning route. Preview the activation consequences.
4. For repair, bind the failing acceptance, audit, review, or release evidence and propose bounded repair scope. Repair cannot silently widen the original outcome.
5. For cancel, enumerate active or pending descendants and require their legal disposition. Preserve every receipt and reason.
6. Submit only the selected action's RPC with expected revision and idempotency key, then render the resulting Milestone state.

## 4b. Applicable rules

The obligations the effective rule graph holds for activities `plan`. They bind what you do; they grant no capability.

- must: **Give every activity a non-empty rule set.** Keep every activity governed by at least one rule; an activity whose effective rule set is empty is a defect, never a statement that no rules apply.
- must: **Scope conduct rules to the closed activity set.** Scope an activity-bound rule only to research, plan, design, implement, test, review, integrate, commit, release, deploy or operate; a rule scoped to any other activity is refused with the offending value named.
- must: **Block on a question only when proceeding is unsafe.** Raise a blocking question only when proceeding under any available assumption would be unsafe or would make the completed work useless if the assumption proved wrong; resolve every other uncertainty by assumption plus disclosure.
- must: **Dispatch independent units of work together.** Dispatch independent units of work together rather than in sequence, where neither consumes the other's output; a sequential dispatch of independent units is a recorded miss.
- must: **Evaluate the practice set at each decision point.** Evaluate the applicable practice set at each decision point rather than recalling it: at least before dispatching work, before presenting a choice to the operator and before emitting a terminal report.
- must: **Expand every abbreviation on first use.** Expand every abbreviation, internal code and lifecycle identifier on first use in an operator-facing surface; write for a competent newcomer, not for the author of the state.
- must: **State the concern once and finish the task.** When a task looks ill-specified, state the concern once, record the assumption you proceed under and complete the work; do not halt and do not silently substitute your own reading.
- must: **Finish independent work before raising a question.** Before raising a question, complete all work that does not depend on its answer, and raise it only where the dependent work begins.
- must: **Recommend the option best for the long term.** Mark the option best for the long term as recommended and state why; where the repository configures a value the choice would override, show the configured value beside the recommendation.
- must: **Decide instead of asking when every option is the same work.** Do not ask a question with no consequence: where every option leads to the same work, select one, state the selection and proceed.
- must: **Retrieve the current practice set in one step.** Retrieve the practice set for the current activity with one query at each decision point instead of remembering which practices apply.
- must: **Record a scope delta before widening the work.** Record a typed scope delta against the task before any widening work begins; an unrecorded widening fails review even when the added work is correct.
- must: **Surface a decision as a typed choice with named options.** Surface a decision as a typed choice with named options, never as free text, and give each option a plain-prose description of what happens if it is selected.
- should: **Show a concrete rendering when options differ in structure.** Where options differ structurally, give each a concrete rendering of its outcome, such as a layout, a diagram or a worked example, rather than a description of the difference.

## 5. Constraints

- Stop on stale revision, missing outcome proof, contradictory exclusions, unresolved descendant disposition, authority failure, or need for an operator scope decision.
- Stopping is a valid outcome, not a failure: when the answer needs an operator or a precondition fails, return `blocked` with the reason rather than guessing.

## 6. Output

Output one MilestoneSkillReport with action, contract summary, before/after revisions, descendant effects, receipt, acceptance gaps, and blockers.

The report validates against `MilestoneSkillReport`, and its terminal outcome is exactly one of `shown`, `defined`, `activated`, `revised`, `repair_requested`, `cancelled`, `blocked`. Prose in the report is explanation, never the result.
