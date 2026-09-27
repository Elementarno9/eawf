---
name: decide
description: "Propose, ratify, reject, supersede or obsolete a Decision."
argument-hint: "<propose|ratify|reject|supersede|obsolete|show> [<decision-ref>] [--title <text>] [--rationale <text>] [--alternative <text>...] [--consequence <text>...] [--evidence <ref>...] [--supersedes <ref>] [--scope <urn>] [--from <ref>...] [--dry-run]"
user-invocable: true
disable-model-invocation: true
---

# /decide

Propose, ratify, reject, supersede or obsolete a Decision.

## 1. Authority

- An operator or an authorized agent may initiate this skill. Agent invocation never widens authority: it needs an enclosing Run, Task or Campaign scope whose compiled capsule already grants every read, write, RPC, budget and external effect below.
- Operator-only actions: `ratify`, `reject`, `supersede`, `obsolete`. An agent that reaches one prepares a PendingAction and stops; it never chooses the recommended option itself.
- Effects: Decision RPCs.
- Allowed RPCs: `read_entity`, `query_evidence`, `decision.propose`, `decision.request_ratification`, `decision.reject`, `decision.supersede`, `decision.obsolete`, `decision.get`. Any other RPC is denied before it reaches a handler.
- Canonical state changes only through those RPCs, and every mutating call carries `--expected-revision` and `--idempotency-key`.
- Local write root: none.
- Executable grants come from the compiled capsule of the enclosing scope alone; nothing on this page adds or widens a tool, path, RPC, credential or external effect.

## 2. Context

One Decision, named by `<decision-ref>` or framed by this invocation, within its exact scope.

Resolve the subject before acting. Name every entity with its identifier and its exact current revision so staleness is detectable; a fact without a revision is a summary, not context.

## 3. Task

Drive one named Decision action without choosing for the operator.

```text
/decide <propose|ratify|reject|supersede|obsolete|show> [<decision-ref>] [--title <text>] [--rationale <text>] [--alternative <text>...] [--consequence <text>...] [--evidence <ref>...] [--supersedes <ref>] [--scope <urn>] [--from <ref>...] [--dry-run]
```

Select exactly one action: `propose`, `ratify`, `reject`, `supersede`, `obsolete`, `show`. An option the selected action does not declare is refused before you start.

## 4. Method

1. Resolve exact scope, Decision revision, evidence, audits, Hypotheses, questions, and effective policy.
2. For propose, frame one choice with stable option keys, at least two real alternatives, consequences, conflicts, and evidence. Do not recommend an option without supporting evidence.
3. For ratify, revalidate evidence and applicability, render persisted options unchanged, and create a protected operator action. The agent never supplies the chosen key.
4. For supersede, create and ratify the replacement first; the daemon then links the old ACTIVE Decision atomically. For obsolete, prove applicability ended and preserve the reason.
5. Submit only the selected Decision RPC with expected revision and idempotency key.

## 4b. Applicable rules

The obligations the effective rule graph holds for activities `design`. They bind what you do; they grant no capability.

- must: **Give every activity a non-empty rule set.** Keep every activity governed by at least one rule; an activity whose effective rule set is empty is a defect, never a statement that no rules apply.
- must: **Scope conduct rules to the closed activity set.** Scope an activity-bound rule only to research, plan, design, implement, test, review, integrate, commit, release, deploy or operate; a rule scoped to any other activity is refused with the offending value named.
- must: **Block on a question only when proceeding is unsafe.** Raise a blocking question only when proceeding under any available assumption would be unsafe or would make the completed work useless if the assumption proved wrong; resolve every other uncertainty by assumption plus disclosure.
- must: **Expand every abbreviation on first use.** Expand every abbreviation, internal code and lifecycle identifier on first use in an operator-facing surface; write for a competent newcomer, not for the author of the state.
- must: **State the concern once and finish the task.** When a task looks ill-specified, state the concern once, record the assumption you proceed under and complete the work; do not halt and do not silently substitute your own reading.
- must: **Recommend the option best for the long term.** Mark the option best for the long term as recommended and state why; where the repository configures a value the choice would override, show the configured value beside the recommendation.
- must: **Deliver every must rule by a deterministic vehicle.** Deliver every must rule through a certified deterministic vehicle, never through a retrieval command a model may decline to run; a retrieval command delivers only should and information rules.
- must: **Decide instead of asking when every option is the same work.** Do not ask a question with no consequence: where every option leads to the same work, select one, state the selection and proceed.
- must: **Reject abstraction serving a use site that does not exist.** Reject an abstraction introduced for a use site that does not yet exist, whatever its quality: name the missing caller, and have the author supply one or remove the abstraction.
- must: **Give every practice rule an observable trigger.** Declare a trigger for every practice rule: an observable condition that marks the moment it applies, distinct from its activity scope.
- must: **State each claim once across rendered prose.** Hold prose to the same budget as code: a rendered artifact states its claim once, and restating a requirement in a second file is duplicate ownership, not emphasis.
- must: **Surface a decision as a typed choice with named options.** Surface a decision as a typed choice with named options, never as free text, and give each option a plain-prose description of what happens if it is selected.
- must: **Verify behavioural claims against the source tree.** Verify behavioural, quantitative and schema claims against the implementation before asserting them. Where a design document and the source disagree, quote the source and report the drift.
- should: **Show a concrete rendering when options differ in structure.** Where options differ structurally, give each a concrete rendering of its outcome, such as a layout, a diagram or a worked example, rather than a description of the difference.
- should: **Fix a high-miss practice by where it surfaces.** Treat a practice rule with a high lifetime miss rate as a surfacing defect: bind it to a narrower trigger or an earlier decision point, or rewrite or retire it; never restate it more emphatically.

## 5. Constraints

- Stop on stale revision, unresolved evidence, hidden plan/scope change, conflicting active Decision, or missing operator authority.
- Stopping is a valid outcome, not a failure: when the answer needs an operator or a precondition fails, return `blocked` with the reason rather than guessing.

## 6. Output

Output one DecisionSkillReport with before/after references, option table, evidence chain, consequences, receipt or Attention reference, and blockers.

The report validates against `DecisionSkillReport`, and its terminal outcome is exactly one of `shown`, `proposed`, `ratified`, `rejected`, `superseded`, `obsoleted`, `blocked`. Prose in the report is explanation, never the result.
