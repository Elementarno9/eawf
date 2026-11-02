---
name: research
description: "Answer one question as a one-page local brief, swift or in rounds; no Campaign."
argument-hint: "<topic...> [--question <text>] [--scope <urn>] [--from <ref>...] [--depth <shallow|medium|deep|exhaustive>] [--rounds <1..5>] [--include <selector>...] [--exclude <selector>...] [--sources <repo|external|both>] [--web <auto|allow|deny|required>] [--domains <domain>...] [--recency-days <N>] [--max-sources <1..20>] [--agents <1..3>] [--budget <spec>] [--save [<relative-path>]] [--output <markdown|json>]"
user-invocable: true
disable-model-invocation: false
---

# /research

Answer one question as a one-page local brief, swift or in rounds; no Campaign.

## 1. Authority

- An operator or an authorized agent may initiate this skill. Agent invocation never widens authority: it needs an enclosing Run, Task or Campaign scope whose compiled capsule already grants every read, write, RPC, budget and external effect below.
- Operates on: question, through `projection.evidence.read`, `eawf research check`.
- Effects: Evidence read and repository reads; one gitignored local brief.
- Allowed RPCs: `projection.evidence.read`. Any other RPC is denied before it reaches a handler. A host session drives a route through the `eawf` command named beside it; a route with no command beside it is driven by the daemon or a native Run.
- Run tools: `eawf_state_query`, `repo_read`, `repo_search`, `submit_report`. These exist only inside a native Run eawf dispatches; a host session has none of them and uses the `eawf` commands above instead.
- Canonical state: never mutated by this skill.
- Local write root: `.ea/local/research`; nothing is written outside it.
- Executable grants come from the compiled capsule of the enclosing scope alone; nothing on this page adds or widens a tool, path, RPC, credential or external effect.

## 2. Context

The question named by `<topic...>` and `--question`, within the scope and the sources this invocation declares. The mode is swift unless `--rounds <N>` is given, which selects rounds mode with at most N rounds; `--depth` sets the survey budget.

Resolve the subject before acting. Name every entity with its identifier and its exact current revision so staleness is detectable; a fact without a revision is a summary, not context.

## 3. Task

Answer one bounded question in one markdown brief of at most one rendered page. You do not create, own, resume, or mutate a Campaign.

```text
/research <topic...> [--question <text>] [--scope <urn>] [--from <ref>...] [--depth <shallow|medium|deep|exhaustive>] [--rounds <1..5>] [--include <selector>...] [--exclude <selector>...] [--sources <repo|external|both>] [--web <auto|allow|deny|required>] [--domains <domain>...] [--recency-days <N>] [--max-sources <1..20>] [--agents <1..3>] [--budget <spec>] [--save [<relative-path>]] [--output <markdown|json>]
```

## 4. Method

1. State the exact question and the decision or next action it informs. Narrow an over-broad topic before reading.
2. Inspect supplied and repository-local primary sources first. Fetch external sources only under the resolved source policy.
3. Swift mode: run one survey pass and ask the operator nothing. Independent parallel slices are allowed within the agent ceiling, but recursion and additional rounds are forbidden.
4. Rounds mode: after the first survey pass, each further round may ask the operator one preference question through the host's structured question tool, with one option per path and a recommended option, then run one more survey pass shaped by the answer. Ask only a question whose answer changes the recommendation, and stop at N rounds even with questions left; record the rest under Open gaps.
5. Reconcile evidence once. Distinguish implementation fact, document claim, external claim, and inference. Resolve every citation used by the verdict.
6. Compare plausible alternatives with their main advantage and cost. Give a verdict, confidence, and material open gaps.
7. If evidence cannot decide, recommend the cheapest discriminating next step. Do not turn the invocation into a Campaign or Spike implicitly: a question that needs more than the rounds allow is recommended as a Campaign, which the operator plans with `eawf campaign new` (`runtime.campaign.start`) and the daemon drives step by step with `eawf campaign run` (`runtime.campaign.run`).
8. Write the brief to the `--save` path under `.ea/local/research/`, by default `<date>-<slug>.md` there: an H1 title, then exactly the sections Question, Findings, Alternatives, Recommendation, Open gaps, References in that order. References is a numbered table with the columns `#` and `Reference`, one repo-relative path, URL or Eawf URN per row, each cited in the prose as `[N]`.
9. Run `eawf research check <brief>` and fix every error it names before returning. Stop at the last pass, one rendered page, or the first hard budget cap.

## 4b. Applicable rules

The obligations the effective rule graph holds for activities `research` and roles `researcher`. They bind what you do; they grant no capability.

- must: **Name a refuted claim only when a finding contradicts it.** Name a claim in refuted_claim_ids only when a finding directly contradicts one of the live claims the prompt listed; never infer a contradiction from absent support, and never name a claim the prompt did not list.
- must: **Back every claim with a reference that resolves and entails it.** Back every claim in a brief with at least one reference that resolves and entails it, a file:line, a store URN or an external URL; mark a claim you cannot back as unresolved and queue it as a next-research item instead of citing weakly.

## 5. Constraints

- Every parallel slice declares what its result would rule out; a slice that cannot name it is not dispatched.
- A brief that `eawf research check` refuses is not a result.
- Stopping is a valid outcome, not a failure: when the answer needs an operator or a precondition fails, return `blocked` with the reason rather than guessing.

## 6. Output

Output one SwiftResearchReport. Its `summary` states the mode, the rounds used, the brief's path, the verdict, its confidence and the stop reason.

The report validates against `SwiftResearchReport`, and its terminal outcome is exactly one of `answered`, `open`, `blocked`. Prose in the report is explanation, never the result. Check it with `eawf skill check-report /research` before returning it. It carries these fields and no other; a nested field is written under its parent.

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `skill_id` | `"research"` | yes | The id of the skill that produced the report. |
| `outcome` | one of `"answered"`, `"open"`, `"blocked"` | yes | How the invocation ended; the result. |
| `summary` | string | no | Explanation of the outcome and what supports it; never the result. |
| `coverage` | object | yes | What the pass covered and did not. |
| `coverage.covered` | list of string | yes | What the pass covered. |
| `coverage.not_covered` | list of object | yes | What the pass did not cover, with a reason each. |
| `coverage.not_covered[].item` | string | yes | What the pass did not reach. |
| `coverage.not_covered[].reason` | string | yes | Why it was not reached. |
