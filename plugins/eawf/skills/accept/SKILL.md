---
name: accept
description: "Decide a Milestone acceptance bundle: prepare, accept, reject or request repair."
argument-hint: "<prepare|accept|reject|request-repair|show> <milestone-ref> [--bundle <ref>] [--criterion <id>...] [--reason <text>] [--repair-scope <text>] [--dry-run]"
user-invocable: true
disable-model-invocation: true
---

# /accept

Decide a Milestone acceptance bundle: prepare, accept, reject or request repair.

## 1. Authority

- Only an authenticated operator initiates this skill. An agent may prepare evidence or recommend the invocation, but never calls it.
- Effects: Milestone acceptance RPCs.
- Allowed RPCs: `read_entity`, `query_evidence`, `milestone.acceptance.prepare`, `milestone.acceptance.decide`. Any other RPC is denied before it reaches a handler.
- Canonical state changes only through those RPCs, and every mutating call carries `--expected-revision` and `--idempotency-key`.
- Local write root: none.
- Executable grants come from the compiled capsule of the enclosing scope alone; nothing on this page adds or widens a tool, path, RPC, credential or external effect.

## 2. Context

One Milestone, named by `<milestone-ref>`, and the acceptance bundle bound to its exact revision.

Resolve the subject before acting. Name every entity with its identifier and its exact current revision so staleness is detectable; a fact without a revision is a summary, not context.

## 3. Task

You prepare or dispose one Milestone acceptance decision. You do not manufacture acceptance evidence and never accept on the operator's behalf.

```text
/accept <prepare|accept|reject|request-repair|show> <milestone-ref> [--bundle <ref>] [--criterion <id>...] [--reason <text>] [--repair-scope <text>] [--dry-run]
```

Select exactly one action: `prepare`, `accept`, `reject`, `request-repair`, `show`. An option the selected action does not declare is refused before you start.

## 4. Method

1. Resolve the Milestone contract and exact revision, required Batches, current integration generations, criteria, audits, reviews, verification receipts, limitations, and unresolved findings.
2. For show or prepare, build the acceptance bundle from resolving evidence. Mark every requirement passed, failed, stale, waived where legally permitted, or unverified. Absence of evidence is unverified.
3. For accept, present the frozen bundle and consequences through a protected operator action. Acceptance is legal only when all blocking requirements pass at the exact revision.
4. For reject, preserve the bundle and operator reason. For request-repair, convert named failures into bounded repair scope without changing the Milestone promise.
5. Submit only the selected acceptance RPC with expected revision and idempotency key, then return its durable receipt.

## 4b. Applicable rules

The obligations the effective rule graph holds for activities `review`. They bind what you do; they grant no capability.

- must: **Name the command and exit status behind a claim.** Back every verification claim with the command executed and its exit status; a green claim whose run cannot be located in the receipt is a fabrication, not an oversight.
- must: **Evaluate the practice set at each decision point.** Evaluate the applicable practice set at each decision point rather than recalling it: at least before dispatching work, before presenting a choice to the operator and before emitting a terminal report.
- must: **Record each conduct deviation as a typed local row.** Record every conduct deviation as a typed row bound to the run that produced it and the obligation it breached, with how it was detected, its severity, an evidence reference and its disposition, in the machine-local store and never in the committed surface.
- must: **Expand every abbreviation on first use.** Expand every abbreviation, internal code and lifecycle identifier on first use in an operator-facing surface; write for a competent newcomer, not for the author of the state.
- must: **Add a helper only with a present-day caller.** Add a helper, wrapper or indirection only with a production caller in the same change. Tolerate duplication at two call sites and extract at the third; a helper whose only caller is a test fails this rule.
- must: **Add a parameter or flag only with a consumer.** Add a parameter, configuration leaf, feature flag or extension point only with a consumer in the same change; a knob shipped for a hypothetical future caller is rejected.
- must: **Recommend the option best for the long term.** Mark the option best for the long term as recommended and state why; where the repository configures a value the choice would override, show the configured value beside the recommendation.
- must: **Decide instead of asking when every option is the same work.** Do not ask a question with no consequence: where every option leads to the same work, select one, state the selection and proceed.
- must: **Reject abstraction serving a use site that does not exist.** Reject an abstraction introduced for a use site that does not yet exist, whatever its quality: name the missing caller, and have the author supply one or remove the abstraction.
- must: **Guard only states reachable on real call paths.** Add no defensive branch, fallback or validation for a state unreachable on the real call paths. Where reachability is genuinely uncertain, record the uncertainty instead of coding around it.
- must: **Record a missed practice as a conduct deviation.** Record a practice whose trigger fired and whose obligation was not met as a conduct deviation, so recall failure is counted per rule and per runtime.
- must: **Retrieve the current practice set in one step.** Retrieve the practice set for the current activity with one query at each decision point instead of remembering which practices apply.
- must: **State each claim once across rendered prose.** Hold prose to the same budget as code: a rendered artifact states its claim once, and restating a requirement in a second file is duplicate ownership, not emphasis.
- must: **Surface a decision as a typed choice with named options.** Surface a decision as a typed choice with named options, never as free text, and give each option a plain-prose description of what happens if it is selected.
- must: **Validate once at the boundary where data enters.** Validate untyped data once, at the boundary where it enters; downstream functions accept validated typed objects and do not re-check them.
- must: **Verify behavioural claims against the source tree.** Verify behavioural, quantitative and schema claims against the implementation before asserting them. Where a design document and the source disagree, quote the source and report the drift.
- should: **Show a concrete rendering when options differ in structure.** Where options differ structurally, give each a concrete rendering of its outcome, such as a layout, a diagram or a worked example, rather than a description of the difference.
- should: **Fix a high-miss practice by where it surfaces.** Treat a practice rule with a high lifetime miss rate as a surfacing defect: bind it to a narrower trigger or an earlier decision point, or rewrite or retire it; never restate it more emphatically.

## 5. Constraints

- Stop on stale evidence, unresolved blocking findings, missing Batch proof, illegal waiver, ambiguous membership, or missing operator authority.
- Stopping is a valid outcome, not a failure: when the answer needs an operator or a precondition fails, return `blocked` with the reason rather than guessing.

## 6. Output

Output one AcceptanceSkillReport with bundle digest, per-requirement verdicts, evidence links, decision receipt or PendingAction, repair scope, and blockers.

The report validates against `AcceptanceSkillReport`, and its terminal outcome is exactly one of `shown`, `prepared`, `accepted`, `rejected`, `repair_requested`, `blocked`. Prose in the report is explanation, never the result.
