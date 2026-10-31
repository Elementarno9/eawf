---
name: plan
description: "Propose, approve and apply a PlanRevision."
argument-hint: "<propose|approve|apply> [<milestone-or-revision-ref>] [--from <ref>...] [--strategy <minimal|balanced|parallel>] [--scope <urn>] [--agents <1..8>] [--budget <spec>] [--feedback <ref>...] [--dry-run]"
user-invocable: true
disable-model-invocation: false
---

# /plan

Propose, approve and apply a PlanRevision.

## 1. Authority

- An operator or an authorized agent may initiate this skill. Agent invocation never widens authority: it needs an enclosing Run, Task or Campaign scope whose compiled capsule already grants every read, write, RPC, budget and external effect below.
- Operates on: PlanRevision, through `projection.roadmap.read`, `eawf plan submit`, `eawf plan approve`, `eawf plan apply`.
- Operator-only actions: `approve`, `apply`. An agent that reaches one files it with `eawf question open-decision` (`runtime.question.open_decision`), shows the bound question the answer carries, and stops; it never chooses the recommended option itself. In Codex the bound question is the answer's `numbered_prompt`, printed verbatim, and the operator's reply is relayed with `eawf question answer` (`runtime.question.answer_numbered`); the console answers the same record, and the first answer wins.
- Effects: The roadmap read plus the PlanRevision submit, approve and apply verbs.
- Allowed RPCs: `projection.roadmap.read`, `eawf plan submit` (`planning.plan_revision.submit`), `eawf plan approve` (`planning.plan_revision.approve`), `eawf plan apply` (`planning.plan_revision.apply`), `eawf question answer` (`runtime.question.answer_numbered`), `eawf question open-decision` (`runtime.question.open_decision`). Any other RPC is denied before it reaches a handler. A host session drives a route through the `eawf` command named beside it; a route with no command beside it is driven by the daemon or a native Run.
- Canonical state changes only through those RPCs, and every mutating call carries `--expected-revision` and `--idempotency-key`.
- Local write root: none.
- Executable grants come from the compiled capsule of the enclosing scope alone; nothing on this page adds or widens a tool, path, RPC, credential or external effect.

## 2. Context

One Milestone, named by `<milestone-or-revision-ref>`, with its contract and its PlanRevision lineage at their exact revisions.

Resolve the subject before acting. Name every entity with its identifier and its exact current revision so staleness is detectable; a fact without a revision is a summary, not context.

## 3. Task

You are proposing one PlanRevision for one Milestone. You do not apply it.

```text
/plan <propose|approve|apply> [<milestone-or-revision-ref>] [--from <ref>...] [--strategy <minimal|balanced|parallel>] [--scope <urn>] [--agents <1..8>] [--budget <spec>] [--feedback <ref>...] [--dry-run]
```

Select exactly one action: `propose`, `approve`, `apply`. An option the selected action does not declare is refused before you start.

## 4. Method

0. Triage uncertainty before you decompose anything. Enumerate the plan's load-bearing unknowns: the integration surfaces, external contracts, and design forks whose real behaviour the plan depends on. For each, name the MeasuredContract that answers it and cite it by ArtifactUrn, or record it as unmeasured. This is a required step, not planner judgment: every other step below derives from text already written, so a plan can be fully valid, fully traceable, and entirely speculative without it.
1. Read the Milestone contract: outcome, appetite, exclusions, and the acceptance journey. These are the promise. Everything you plan exists to satisfy a step of that journey.
2. Atomize the source. Every requirement in the brief or Milestone becomes a source atom with an anchored span. An atom either maps to a criterion or is dropped explicitly with a reason and a Decision reference. Silent loss is the defect this step exists to prevent.
3. Decompose into Delivery Batches and Tasks. One Batch per repository. Give each Task one role, non-overlapping ownership claims, explicit dependencies with the proof each requires, and at least one criterion.
4. Write criteria that can fail. Each carries a typed response clause: what is observed, on what subject, where the proof lives, and what value is expected. A criterion no gate can falsify is not a criterion. Each also carries its grounding - measured, assumed, or accepted-risk - and a measured criterion cites the contract it rests on by ArtifactUrn rather than describing it.
5. Check your own plan against the deterministic lenses before submitting: every atom mapped or dropped, every outcome reachable, no dependency cycle, no concurrent write overlap, every criterion covered by a gate.
6. Submit with `eawf plan submit --from-spec <proposal.json> --idempotency-key <key> --actor <your-principal>`. The JSON file carries the proposal's key, author, body and parent_key; the actor is your own principal key, never an `OP-` operator key. The daemon validates and either rejects with findings or records the revision as VALIDATED.
7. Approval and apply are the operator's acts, not yours. File the approval as a typed question with `eawf question open-decision --from-spec <decision.json> --actor <your-principal>`, show the bound question it returns, and stop. The operator answers it and then runs `eawf plan approve <PRV-KEY> --expected-revision <N> --action-ref <action-urn> --approved-by <operator> --idempotency-key <key> --actor <operator>` and `eawf plan apply <PRV-KEY> --expected-revision <N> --idempotency-key <key> --actor <operator>`. Never run either yourself: the daemon refuses an operator key named from an agent session.

## 4b. Applicable rules

The obligations the effective rule graph holds for activities `plan` and roles `planner`. They bind what you do; they grant no capability.

- must: **Give every activity a non-empty rule set.** Keep every activity governed by at least one rule; an activity whose effective rule set is empty is a defect, never a statement that no rules apply.
- must: **Scope conduct rules to the closed activity set.** Scope an activity-bound rule only to research, plan, design, implement, test, review, integrate, commit, release, deploy or operate; a rule scoped to any other activity is refused with the offending value named.
- must: **Finish independent work first and block only when proceeding is unsafe.** When part of the scope fails or awaits an answer, finish every independent part, state what was left undone and why, and raise a question only where the dependent work begins. Block only when proceeding on any assumption would be unsafe, or useless if wrong; unattended, record a typed open question and continue.
- must: **Dispatch independent units of work together.** Dispatch independent units of work together rather than in sequence, where neither consumes the other's output; a sequential dispatch of independent units is a recorded miss.
- must: **Evaluate the practice set at each decision point.** Retrieve the current activity's practice set with one query and evaluate it at each decision point rather than recalling it: at least before dispatching work, before presenting a choice to the operator and before emitting a terminal report.
- must: **Expand every abbreviation on first use.** Expand every abbreviation, internal code and lifecycle identifier on first use in an operator-facing surface; write for a competent newcomer, not for the author of the state.
- must: **Record a scope delta before widening the work.** Record a typed scope delta against the task before any widening work begins; an unrecorded widening fails review even when the added work is correct.
- must: **Surface a decision as a typed choice with named options.** Surface a decision as a typed choice with named options, never as free text, and give each option a plain-prose description of what happens if it is selected.
- must: **Map every brief deliverable to a criterion or a deferral.** Map every enumerated brief deliverable to a criterion or to an explicit deferral row with its reason and target; while any span stays unmapped, halt planning with verdict=blocked naming the span.
- must: **Mark a criterion deterministic wherever a falsifier exists.** Set evidence_kind to deterministic wherever a falsifier exists, and to attested only for a claim that is genuinely judgment-bound.
- must: **Pin stable contracts verbatim in criterion text.** Pin stable contracts verbatim in the criterion text, such as digit and key maps, enum values, schemas and API shapes; a criterion that names only a chassis is a thinning defect.
- must: **Emit only typed criteria with a proof locus and a gate.** Give every emitted Task typed criteria (kind other than legacy), each with a response clause naming the observed verb, the object and a file:line proof locus, and at least one gate, usually a targeted pytest command, with policy=block and required=true.

## 5. Constraints

- An acceptance-mapped criterion still at assumed blocks APPROVE. There are exactly two exits: cite a SpikeReport or MeasuredContract to reach measured, or record an operator waiver Decision to reach accepted-risk, which carries a resolvable DecisionUrn. Learn the gate here rather than from a rejection.
- You cannot assign an observed head, a validation result, an approval, or any lifecycle state.
- A rejected revision is repaired by proposing a child revision with explicit dispositions for each finding, never by editing the rejected one.
- Do not plan work the exclusions forbid. If the outcome seems to require it, raise that as a question rather than widening scope.
- Stopping is a valid outcome, not a failure: when the answer needs an operator or a precondition fails, return `blocked` with the reason rather than guessing.

## 6. Output

Output one report whose `summary` gives the strict PlanRevision proposal by key, plus the atom-to-criterion coverage table, every atom you dropped with its reason, and the uncertainty register from step 0 with each unknown either resolved to a cited contract or named as unmeasured.

The report validates against `PlanSkillReport`, and its terminal outcome is exactly one of `proposed`, `rejected`, `approved`, `applied`, `blocked`. Prose in the report is explanation, never the result. Check it with `eawf skill check-report /plan` before returning it. It carries these fields and no other; a nested field is written under its parent.

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `skill_id` | `"plan"` | yes | The id of the skill that produced the report. |
| `outcome` | one of `"proposed"`, `"rejected"`, `"approved"`, `"applied"`, `"blocked"` | yes | How the invocation ended; the result. |
| `summary` | string | no | Explanation of the outcome and what supports it; never the result. |
