---
name: mockup
description: "Build and compare operator-visible design options."
argument-hint: "<surface...> [--question <text>] [--option <text>...] [--count <2..4>] [--format <ascii|html|image>] [--viewport <spec>...] [--state <name>...] [--compare <axis>...] [--output <inline|local>] [--local-root <path-under-.ea/local/mockups>] [--budget <spec>]"
user-invocable: true
disable-model-invocation: false
---

# /mockup

Build and compare operator-visible design options.

## 1. Authority

- An operator or an authorized agent may initiate this skill. Agent invocation never widens authority: it needs an enclosing Run, Task or Campaign scope whose compiled capsule already grants every read, write, RPC, budget and external effect below.
- Operates on: operator-visible surface, through no lifecycle route; this is an explicit skill contract.
- Operator choices: an agent that reaches a choice this skill puts to the operator files it with `eawf question open-decision` (`runtime.question.open_decision`), shows the bound question the answer carries, and stops; it never chooses the recommended option itself. In Codex the bound question is the answer's `numbered_prompt`, printed verbatim, and the operator's reply is relayed with `eawf question answer` (`runtime.question.answer_numbered`); the console answers the same record, and the first answer wins.
- Effects: Inline rendering or proposal-local assets, and the operator decision the options are filed as.
- Allowed RPCs: `eawf question answer` (`runtime.question.answer_numbered`), `eawf question open-decision` (`runtime.question.open_decision`). Any other RPC is denied before it reaches a handler. A host session drives a route through the `eawf` command named beside it; a route with no command beside it is driven by the daemon or a native Run.
- Run tools: `ask_operator`. These exist only inside a native Run eawf dispatches; a host session has none of them and uses the `eawf` commands above instead.
- Canonical state changes only through those RPCs, and every mutating call carries `--expected-revision` and `--idempotency-key`.
- Local write root: `.ea/local/mockups`; nothing is written outside it.
- Executable grants come from the compiled capsule of the enclosing scope alone; nothing on this page adds or widens a tool, path, RPC, credential or external effect.

## 2. Context

One operator-visible surface, named by `<surface...>`, and the decision question it serves.

Resolve the subject before acting. Name every entity with its identifier and its exact current revision so staleness is detectable; a fact without a revision is a summary, not context.

## 3. Task

Create two to four genuinely distinct and comparable options for one operator choice.

```text
/mockup <surface...> [--question <text>] [--option <text>...] [--count <2..4>] [--format <ascii|html|image>] [--viewport <spec>...] [--state <name>...] [--compare <axis>...] [--output <inline|local>] [--local-root <path-under-.ea/local/mockups>] [--budget <spec>]
```

## 4. Method

1. Normalize one decision question, brief, data fixture, viewport, states, journeys, and constraints across every option.
2. Render each option at equal fidelity. Show the primary journey and relevant loading, empty, error, and permission states; label omissions.
3. For local HTML or code, verify rendering and interactions. For images or ASCII, state the interaction limit explicitly.
4. Compare options on the declared axes, including strengths, costs, risks, accessibility, and best-fit context. Polish cannot be used to bias one choice.
5. Present stable option keys through Attention when a choice is requested. Recommend the durable best fit but never choose for the operator.

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

- Stop on a missing decision question, incomparable constraints, inaccessible output, or need for product mutation outside a Task.
- Stopping is a valid outcome, not a failure: when the answer needs an operator or a precondition fails, return `needs_operator` or `blocked` with the reason rather than guessing.

## 6. Output

Output one MockupReport. Its `summary` states the artifact references, comparison matrix, state/journey coverage, recommendation, and Attention reference where needed.

The report validates against `MockupReport`, and its terminal outcome is exactly one of `presented`, `selected`, `needs_operator`, `blocked`. Prose in the report is explanation, never the result. Check it with `eawf skill check-report /mockup` before returning it. It carries these fields and no other; a nested field is written under its parent.

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `skill_id` | `"mockup"` | yes | The id of the skill that produced the report. |
| `outcome` | one of `"presented"`, `"selected"`, `"needs_operator"`, `"blocked"` | yes | How the invocation ended; the result. |
| `summary` | string | no | Explanation of the outcome and what supports it; never the result. |
