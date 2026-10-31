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

- Only an authenticated operator initiates this skill, by design: it is kept out of the model's reach. An agent may prepare evidence or recommend the invocation, but never calls it.
- Operates on: Milestone, through `projection.milestone.read`, `projection.milestone.acceptance`, `eawf milestone open-approval`, `eawf milestone seal-approval`, `eawf milestone accept`, `runtime.delivery.request_acceptance_repair`.
- Effects: Milestone acceptance reads plus the approval, acceptance and repair verbs.
- Allowed RPCs: `projection.milestone.read`, `projection.milestone.acceptance`, `eawf milestone open-approval` (`runtime.delivery.open_acceptance_approval`), `eawf milestone seal-approval` (`runtime.delivery.seal_acceptance_approval`), `eawf milestone accept` (`domain.milestone.accept`), `runtime.delivery.request_acceptance_repair`. Any other RPC is denied before it reaches a handler. A host session drives a route through the `eawf` command named beside it; a route with no command beside it is driven by the daemon or a native Run.
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
- must: **Evaluate the practice set at each decision point.** Retrieve the current activity's practice set with one query and evaluate it at each decision point rather than recalling it: at least before dispatching work, before presenting a choice to the operator and before emitting a terminal report.
- must: **Record each conduct deviation as a typed local row.** Record every conduct deviation, including a practice whose trigger fired and whose obligation was not met, as a typed row in the machine-local store, never the committed surface, bound to its run and breached obligation, with detection method, severity, evidence reference and disposition.
- must: **Expand every abbreviation on first use.** Expand every abbreviation, internal code and lifecycle identifier on first use in an operator-facing surface; write for a competent newcomer, not for the author of the state.
- must: **Add a helper, knob or abstraction only with a present-day caller.** Add a helper, abstraction, parameter, configuration leaf, feature flag or extension point only with a production caller in the same change; for one served only by a test or future use site, name the missing caller and have the author supply one or remove it. Tolerate duplication at two call sites; extract at the third.
- must: **Validate at the entry boundary and guard only reachable states.** Validate untyped data once, at the boundary where it enters, and pass the typed object downstream unchecked. Add no defensive branch, fallback or validation for a state unreachable on the real call paths; where reachability is genuinely uncertain, record the uncertainty instead of coding around it.
- must: **State each claim once across rendered prose.** Hold prose to the same budget as code: a rendered artifact states its claim once, and restating a requirement in a second file is duplicate ownership, not emphasis.
- must: **Surface a decision as a typed choice with named options.** Surface a decision as a typed choice with named options, never as free text, and give each option a plain-prose description of what happens if it is selected.
- should: **Fix a high-miss practice by where it surfaces.** Treat a practice rule with a high lifetime miss rate as a surfacing defect: bind it to a narrower trigger or an earlier decision point, or rewrite or retire it; never restate it more emphatically.

## 5. Constraints

- Stop on stale evidence, unresolved blocking findings, missing Batch proof, illegal waiver, ambiguous membership, or missing operator authority.
- Stopping is a valid outcome, not a failure: when the answer needs an operator or a precondition fails, return `blocked` with the reason rather than guessing.

## 6. Output

Output one AcceptanceSkillReport. Its `summary` states the bundle digest, per-requirement verdicts, evidence links, decision receipt or PendingAction, repair scope, and blockers.

The report validates against `AcceptanceSkillReport`, and its terminal outcome is exactly one of `shown`, `prepared`, `accepted`, `rejected`, `repair_requested`, `blocked`. Prose in the report is explanation, never the result. Check it with `eawf skill check-report /accept` before returning it. It carries these fields and no other; a nested field is written under its parent.

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `skill_id` | `"accept"` | yes | The id of the skill that produced the report. |
| `outcome` | one of `"shown"`, `"prepared"`, `"accepted"`, `"rejected"`, `"repair_requested"`, `"blocked"` | yes | How the invocation ended; the result. |
| `summary` | string | no | Explanation of the outcome and what supports it; never the result. |
