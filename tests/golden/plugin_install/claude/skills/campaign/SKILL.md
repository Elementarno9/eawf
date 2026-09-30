---
name: campaign
description: "Run a complete Campaign from definition through terminal synthesis."
argument-hint: "<run|resume|steer|cancel|show> [<topic-or-campaign-ref>...] [--goal <text>] [--track <ref>] [--audience <text>] [--use <text>] [--artifact <kind>] [--question <text>...] [--exclude <text>...] [--require-source <selector>...] [--method <policy>] [--rounds <N>] [--agents <N>] [--budget <spec>] [--checkpoint <each-round|on-risk|terminal>] [--stop <rule>...] [--feedback <ref>...] [--resume <campaign-ref>] [--reason <text>]"
user-invocable: true
disable-model-invocation: true
---

# /campaign

Run a complete Campaign from definition through terminal synthesis.

## 1. Authority

- Only an authenticated operator initiates this skill, by design: it is kept out of the model's reach. An agent may prepare evidence or recommend the invocation, but never calls it.
- Operates on: Campaign, through `projection.campaign.read`, `projection.campaign.view`, `projection.campaign.artifact`, `runtime.campaign.start`, `runtime.campaign.run`, `runtime.campaign.plan.revise`, `runtime.campaign.close`.
- Operator choices: an agent that reaches a choice this skill puts to the operator files it with `eawf question open-decision` (`runtime.question.open_decision`), shows the bound question the answer carries, and stops; it never chooses the recommended option itself. In Codex the bound question is the answer's `numbered_prompt`, printed verbatim, and the operator's reply is relayed with `eawf question answer` (`runtime.question.answer_numbered`); the console answers the same record, and the first answer wins.
- Effects: Campaign start, drive, revise and close verbs, their reads, and Run tools.
- Allowed RPCs: `projection.campaign.read`, `projection.campaign.view`, `projection.campaign.artifact`, `runtime.campaign.start`, `runtime.campaign.run`, `runtime.campaign.plan.revise`, `runtime.campaign.close`, `runtime.question.answer_numbered`, `runtime.question.open_decision`. Any other RPC is denied before it reaches a handler.
- Run tools: `submit_evidence`, `submit_report`, `ask_operator`.
- Canonical state changes only through those RPCs, and every mutating call carries `--expected-revision` and `--idempotency-key`.
- Local write root: none.
- Executable grants come from the compiled capsule of the enclosing scope alone; nothing on this page adds or widens a tool, path, RPC, credential or external effect.

## 2. Context

One Campaign: a new one defined by the brief this invocation supplies, or the existing Campaign named by its reference, with its plan and artifact revisions.

Resolve the subject before acting. Name every entity with its identifier and its exact current revision so staleness is detectable; a fact without a revision is a summary, not context.

## 3. Task

Coordinate one Research Campaign from a strict brief to terminal disposition. You do no delivery work and edit no product files.

```text
/campaign <run|resume|steer|cancel|show> [<topic-or-campaign-ref>...] [--goal <text>] [--track <ref>] [--audience <text>] [--use <text>] [--artifact <kind>] [--question <text>...] [--exclude <text>...] [--require-source <selector>...] [--method <policy>] [--rounds <N>] [--agents <N>] [--budget <spec>] [--checkpoint <each-round|on-risk|terminal>] [--stop <rule>...] [--feedback <ref>...] [--resume <campaign-ref>] [--reason <text>]
```

Select exactly one action: `run`, `resume`, `steer`, `cancel`, `show`. An option the selected action does not declare is refused before you start.

## 4. Method

1. Validate the brief: title, owning Track, the questions to work (the Campaign's own first), depth, fan-out width and budget. An incomplete brief raises one prepared operator question instead of creating a shell Campaign.
2. Show the plan `runtime.campaign.start` would approve -- one step per question and method the depth names, then one synthesis step -- and approve it only on the operator's protected approval. The daemon files each question and approves the exact plan; never select approval yourself.
3. `runtime.campaign.run` drives the approved plan in the daemon: each round it starts every ready step, up to the fan-out width, on a Run of its own, and the research agent works that step's question read-only.
4. Each round checkpoints in the daemon: the accountant charges the round's spend to the step and the Campaign, the round's report is kept as an artifact revision of the step, the step finishes with its outcome, and the findings it names are promoted as held CFN findings.
5. A hard budget axis at its limit stops dispatch, and the Campaign records the budget stop so it never reads as converged on its own; widening the budget, the questions or the steps is a plan revision (`runtime.campaign.plan.revise`) the operator approves, which keeps every started step unchanged.
6. A failed round returns its step to pending and pauses the drive; `campaign run` resumes it on a new Run. Read progress through `projection.campaign.view` and each checkpoint through `projection.campaign.artifact`.
7. The drive converges the Campaign once every step is done, or once a budget stop leaves nothing running; `runtime.campaign.close` cancels it with the operator's reason. A pause is resumable, never false completion.

## 4b. Applicable rules

The obligations the effective rule graph holds for activities `research` and roles `researcher`. They bind what you do; they grant no capability.

- must: **Verify behavioural claims against the source tree.** Verify behavioural, quantitative and schema claims against the implementation before asserting them. Where a design document and the source disagree, quote the source and report the drift.
- must: **Name a refuted claim only when a finding contradicts it.** Name a claim in refuted_claim_ids only when a finding directly contradicts one of the live claims the prompt listed; never infer a contradiction from absent support, and never name a claim the prompt did not list.
- must: **Back every claim with a reference that resolves and entails it.** Back every claim in a brief with at least one reference that resolves and entails it, a file:line, a store URN or an external URL; mark a claim you cannot back as unresolved and queue it as a next-research item instead of citing weakly.

## 5. Constraints

- Only the operator approves a plan or its revision, and only the operator cancels; you never select approval yourself.
- Every parallel researcher Run declares what its result would rule out; a Run that cannot name it is not dispatched.
- Stopping is a valid outcome, not a failure: when the answer needs an operator or a precondition fails, return `needs_operator` or `paused` with the reason rather than guessing.

## 6. Output

Output one CampaignRunReport containing the Campaign and plan revisions, the steps with their Runs and outcomes, the artifact revisions, the promoted findings, the budget spend and stop, and the final disposition.

The report validates against `CampaignRunReport`, and its terminal outcome is exactly one of `completed`, `dropped`, `failed`, `needs_operator`, `paused`, `budget_exhausted`. It carries a `coverage` block listing what the pass covered and, with a reason each, what it did not. Prose in the report is explanation, never the result. Check it with `eawf skill check-report /campaign` before returning it.
