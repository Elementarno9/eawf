---
name: decide
description: "Propose, supersede or show a Decision."
argument-hint: "<propose|supersede|show> [<decision-ref>] [--summary <text>] [--rationale <text>] [--alternative <text>...] [--supersedes <ref>] [--scope <urn>] [--dry-run]"
user-invocable: true
disable-model-invocation: false
---

# /decide

Propose, supersede or show a Decision.

## 1. Authority

- An operator or an authorized agent may initiate this skill. Agent invocation never widens authority: it needs an enclosing Run, Task or Campaign scope whose compiled capsule already grants every read, write, RPC, budget and external effect below.
- Operates on: Decision, through `eawf question open-decision`, `eawf question answer`.
- Operator-only actions: `supersede`. An agent that reaches one files it with `eawf question open-decision` (`runtime.question.open_decision`), shows the bound question the answer carries, and stops; it never chooses the recommended option itself. In Codex the bound question is the answer's `numbered_prompt`, printed verbatim, and the operator's reply is relayed with `eawf question answer` (`runtime.question.answer_numbered`); the console answers the same record, and the first answer wins.
- Effects: The question open-decision and answer verbs.
- Allowed RPCs: `eawf question answer` (`runtime.question.answer_numbered`), `eawf question open-decision` (`runtime.question.open_decision`). Any other RPC is denied before it reaches a handler. A host session drives a route through the `eawf` command named beside it; a route with no command beside it is driven by the daemon or a native Run.
- Canonical state changes only through those RPCs, and every mutating call carries `--expected-revision` and `--idempotency-key`.
- Local write root: none.
- Executable grants come from the compiled capsule of the enclosing scope alone; nothing on this page adds or widens a tool, path, RPC, credential or external effect.

## 2. Context

One Decision, named by `<decision-ref>` or framed by this invocation, within its exact scope.

Resolve the subject before acting. Name every entity with its identifier and its exact current revision so staleness is detectable; a fact without a revision is a summary, not context.

## 3. Task

Drive one named Decision action without choosing for the operator.

```text
/decide <propose|supersede|show> [<decision-ref>] [--summary <text>] [--rationale <text>] [--alternative <text>...] [--supersedes <ref>] [--scope <urn>] [--dry-run]
```

Select exactly one action: `propose`, `supersede`, `show`. An option the selected action does not declare is refused before you start.

## 4. Method

1. Resolve exact scope, Decision revision, evidence, audits, Hypotheses, questions, and effective policy.
2. For propose, frame one choice with stable option keys, at least two real alternatives, consequences, conflicts, and evidence. Do not recommend an option without supporting evidence.
3. For supersede, record the replacement first, then link the old Decision to it. Superseding is the operator's act; an agent prepares it and stops.
4. For show, list the scope's Decisions and their supersession graph without mutation.
5. Run only the selected decision verb and return what it recorded.

## 4b. Applicable rules

The obligations the effective rule graph holds for activities `design`. They bind what you do; they grant no capability.

- must: **Give every activity a non-empty rule set.** Keep every activity governed by at least one rule; an activity whose effective rule set is empty is a defect, never a statement that no rules apply.
- must: **Scope conduct rules to the closed activity set.** Scope an activity-bound rule only to research, plan, design, implement, test, review, integrate, commit, release, deploy or operate; a rule scoped to any other activity is refused with the offending value named.
- must: **Finish independent work first and block only when proceeding is unsafe.** When part of the scope fails or awaits an answer, finish every independent part, state what was left undone and why, and raise a question only where the dependent work begins. Block only when proceeding on any assumption would be unsafe, or useless if wrong; unattended, record a typed open question and continue.
- must: **Expand every abbreviation on first use.** Expand every abbreviation, internal code and lifecycle identifier on first use in an operator-facing surface; write for a competent newcomer, not for the author of the state.
- must: **Add a helper, knob or abstraction only with a present-day caller.** Add a helper, abstraction, parameter, configuration leaf, feature flag or extension point only with a production caller in the same change; for one served only by a test or future use site, name the missing caller and have the author supply one or remove it. Tolerate duplication at two call sites; extract at the third.
- must: **Deliver every must rule by a deterministic vehicle.** Deliver every must rule through a certified deterministic vehicle, never through a retrieval command a model may decline to run; a retrieval command delivers only should and information rules.
- must: **Give every practice rule an observable trigger.** Declare a trigger for every practice rule: an observable condition that marks the moment it applies, distinct from its activity scope.
- must: **State each claim once across rendered prose.** Hold prose to the same budget as code: a rendered artifact states its claim once, and restating a requirement in a second file is duplicate ownership, not emphasis.
- must: **Surface a decision as a typed choice with named options.** Surface a decision as a typed choice with named options, never as free text, and give each option a plain-prose description of what happens if it is selected.
- should: **Fix a high-miss practice by where it surfaces.** Treat a practice rule with a high lifetime miss rate as a surfacing defect: bind it to a narrower trigger or an earlier decision point, or rewrite or retire it; never restate it more emphatically.

## 5. Constraints

- Stop on stale revision, unresolved evidence, hidden plan/scope change, conflicting active Decision, or missing operator authority.
- Stopping is a valid outcome, not a failure: when the answer needs an operator or a precondition fails, return `blocked` with the reason rather than guessing.

## 6. Output

Output one DecisionSkillReport. Its `summary` states the before/after references, option table, evidence chain, consequences, receipt or Attention reference, and blockers.

The report validates against `DecisionSkillReport`, and its terminal outcome is exactly one of `shown`, `proposed`, `superseded`, `blocked`. Prose in the report is explanation, never the result. Check it with `eawf skill check-report /decide` before returning it. It carries these fields and no other; a nested field is written under its parent.

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `skill_id` | `"decide"` | yes | The id of the skill that produced the report. |
| `outcome` | one of `"shown"`, `"proposed"`, `"superseded"`, `"blocked"` | yes | How the invocation ended; the result. |
| `summary` | string | no | Explanation of the outcome and what supports it; never the result. |
