---
name: verify
description: "Verify one Delivery Batch at one exact revision, as auditor or as reviewer."
argument-hint: "<batch-or-revision-ref> [--mode <gates|audit|review|security|all>] [--gate <id>...] [--severity-floor <P0|P1|P2|P3>] [--agents <1..8>] [--budget <spec>] [--milestone <ref>] [--journey <step>...] [--accepted-binding <binding>] [--requested-by <principal>] [--no-cache] [--expected-revision <N>] [--idempotency-key <key>] [--output <human|json|markdown>]"
user-invocable: true
disable-model-invocation: false
---

# /verify

Verify one Delivery Batch at one exact revision, as auditor or as reviewer.

## 1. Authority

- An operator or an authorized agent may initiate this skill. Agent invocation never widens authority: it needs an enclosing Run, Task or Campaign scope whose compiled capsule already grants every read, write, RPC, budget and external effect below.
- Operates on: Batch, through `projection.batch.detail.read`, `projection.evidence.read`, `runtime.delivery.verify_batch`, `runtime.delivery.assess_completion`, `eawf milestone open-approval`.
- Effects: Batch and evidence reads plus the Batch verification, Task completion and acceptance-question verbs, which file verification receipts.
- Allowed RPCs: `projection.batch.detail.read`, `projection.evidence.read`, `runtime.delivery.verify_batch`, `runtime.delivery.assess_completion`, `eawf milestone open-approval` (`runtime.delivery.open_acceptance_approval`). Any other RPC is denied before it reaches a handler. A host session drives a route through the `eawf` command named beside it; a route with no command beside it is driven by the daemon or a native Run.
- Canonical state changes only through those RPCs, and every mutating call carries `--expected-revision` and `--idempotency-key`.
- Local write root: none.
- Executable grants come from the compiled capsule of the enclosing scope alone; nothing on this page adds or widens a tool, path, RPC, credential or external effect.

## 2. Context

One Delivery Batch or exact revision, named by `<batch-or-revision-ref>`, bound to its commit, tree, criteria digest and policy digest.

Resolve the subject before acting. Name every entity with its identifier and its exact current revision so staleness is detectable; a fact without a revision is a summary, not context.

## 3. Task

You verify one Delivery Batch at one exact revision. You do not repair it.

You may be invoked as an auditor or as a reviewer. They are different jobs: an audit is closed-world — it tries to falsify each required criterion. A review is open-world — it looks for defects nobody wrote a criterion for. Do the one you were assigned.

```text
/verify <batch-or-revision-ref> [--mode <gates|audit|review|security|all>] [--gate <id>...] [--severity-floor <P0|P1|P2|P3>] [--agents <1..8>] [--budget <spec>] [--milestone <ref>] [--journey <step>...] [--accepted-binding <binding>] [--requested-by <principal>] [--no-cache] [--expected-revision <N>] [--idempotency-key <key>] [--output <human|json|markdown>]
```

## 4. Method

1. Bind the exact head: commit, tree, criteria digest, policy digest. Every finding you record is against that revision. If the head moves, stop; your result would be stale.
2. As auditor: for each required criterion, attempt to falsify it. Check that the receipts entail what they claim rather than that they exist. One required criterion that fails or cannot be verified makes the whole audit fail, whatever the aggregate looks like.
3. As reviewer: search for defects by category — correctness, security, data loss, migration, public contract, performance. Record each as a stable finding with a repo-relative locus and evidence.
4. Do not resolve your own findings and do not edit the candidate.

## 4b. Applicable rules

The obligations the effective rule graph holds for activities `review`, `test` and roles `auditor`. They bind what you do; they grant no capability.

- must: **Finish independent work first and block only when proceeding is unsafe.** When part of the scope fails or awaits an answer, finish every independent part, state what was left undone and why, and raise a question only where the dependent work begins. Block only when proceeding on any assumption would be unsafe, or useless if wrong; unattended, record a typed open question and continue.
- must: **Name the command and exit status behind a claim.** Back every verification claim with the command executed and its exit status; a green claim whose run cannot be located in the receipt is a fabrication, not an oversight.
- must: **Evaluate the practice set at each decision point.** Retrieve the current activity's practice set with one query and evaluate it at each decision point rather than recalling it: at least before dispatching work, before presenting a choice to the operator and before emitting a terminal report.
- must: **Record each conduct deviation as a typed local row.** Record every conduct deviation, including a practice whose trigger fired and whose obligation was not met, as a typed row in the machine-local store, never the committed surface, bound to its run and breached obligation, with detection method, severity, evidence reference and disposition.
- must: **Expand every abbreviation on first use.** Expand every abbreviation, internal code and lifecycle identifier on first use in an operator-facing surface; write for a competent newcomer, not for the author of the state.
- must: **Add a helper, knob or abstraction only with a present-day caller.** Add a helper, abstraction, parameter, configuration leaf, feature flag or extension point only with a production caller in the same change; for one served only by a test or future use site, name the missing caller and have the author supply one or remove it. Tolerate duplication at two call sites; extract at the third.
- must: **Validate at the entry boundary and guard only reachable states.** Validate untyped data once, at the boundary where it enters, and pass the typed object downstream unchecked. Add no defensive branch, fallback or validation for a state unreachable on the real call paths; where reachability is genuinely uncertain, record the uncertainty instead of coding around it.
- must: **State each claim once across rendered prose.** Hold prose to the same budget as code: a rendered artifact states its claim once, and restating a requirement in a second file is duplicate ownership, not emphasis.
- must: **Re-execute only what failed.** After a failure, re-execute only what failed, unless the change is cross-cutting, the artifact is a cross-scope scorecard, or a release gate needs a full pass.
- must: **Surface a decision as a typed choice with named options.** Surface a decision as a typed choice with named options, never as free text, and give each option a plain-prose description of what happens if it is selected.
- must: **Demand the call site for a wired-at-call-site criterion.** Never accept that a function exists as evidence for a wired-at-call-site criterion; demand the file:line of its production call site.
- must: **Judge the work without the implementing session's log.** Never read the implementing session's log; judge the work from the diff, the declared criteria and the evidence you gather yourself.
- must: **Prove the audit can fail before trusting it.** Before trusting an audit, name for at least one criterion the concrete broken input your check would reject, such as a wrong constant, a deleted call site or a reverted edit; when no such input exists, report verdict=blocked naming the untestable criterion.
- must: **Report a criterion with no falsifier as unverified.** Report a legacy or attested criterion that has no falsifier as unverified, never as passed.
- should: **Fix a high-miss practice by where it surfaces.** Treat a practice rule with a high lifetime miss rate as a surfacing defect: bind it to a narrower trigger or an earlier decision point, or rewrite or retire it; never restate it more emphatically.
- should: **Run the Task's own gates before re-reading its prose.** Run the audited Task's own gates rather than re-reading its prose, and report a gate that cannot fail on broken input as a finding.

## 5. Constraints

- You receive no producer transcript and no context from the Run that made the work. That independence is the point of the job.
- A finding may be accepted as risk only when it is advisory and outside security, migration, data loss, authority, public contract, required criteria, and release proof. Everything else is resolved or superseded.
- Absence of evidence is not a pass. If you cannot verify a criterion, say unverified; that blocks, and it should.
- Stopping is a valid outcome, not a failure: when the answer needs an operator or a precondition fails, return `blocked` with the reason rather than guessing.

## 6. Output

Output one report whose `summary` gives the audit or review result: per-criterion verdicts with the falsifier attempted, or stable findings with severity and evidence. Aggregate verdicts are derived from rows, never asserted.

The report validates against `VerificationReport`, and its terminal outcome is exactly one of `passed`, `failed`, `unverified`, `stale`, `blocked`. Prose in the report is explanation, never the result. Check it with `eawf skill check-report /verify` before returning it. It carries these fields and no other; a nested field is written under its parent.

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `skill_id` | `"verify"` | yes | The id of the skill that produced the report. |
| `outcome` | one of `"passed"`, `"failed"`, `"unverified"`, `"stale"`, `"blocked"` | yes | How the invocation ended; the result. |
| `summary` | string | no | Explanation of the outcome and what supports it; never the result. |
