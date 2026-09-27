---
name: verify
description: "Verify one Delivery Batch at one exact revision, as auditor or as reviewer."
argument-hint: "<batch-or-revision-ref> [--mode <gates|audit|review|security|all>] [--gate <id>...] [--severity-floor <P0|P1|P2|P3>] [--agents <1..8>] [--budget <spec>] [--no-cache] [--output <human|json|markdown>]"
user-invocable: true
disable-model-invocation: true
---

# /verify

Verify one Delivery Batch at one exact revision, as auditor or as reviewer.

## 1. Authority

- An operator or an authorized agent may initiate this skill. Agent invocation never widens authority: it needs an enclosing Run, Task or Campaign scope whose compiled capsule already grants every read, write, RPC, budget and external effect below.
- Effects: Read and check effects plus verification receipts.
- Allowed RPCs: `read_entity`, `query_evidence`, `verification.submit`, `verification.status`, `verification.resume`, `batch.audit.submit`, `batch.review.submit`, `operation.status`. Any other RPC is denied before it reaches a handler.
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
/verify <batch-or-revision-ref> [--mode <gates|audit|review|security|all>] [--gate <id>...] [--severity-floor <P0|P1|P2|P3>] [--agents <1..8>] [--budget <spec>] [--no-cache] [--output <human|json|markdown>]
```

## 4. Method

1. Bind the exact head: commit, tree, criteria digest, policy digest. Every finding you record is against that revision. If the head moves, stop; your result would be stale.
2. As auditor: for each required criterion, attempt to falsify it. Check that the receipts entail what they claim rather than that they exist. One required criterion that fails or cannot be verified makes the whole audit fail, whatever the aggregate looks like.
3. As reviewer: search for defects by category — correctness, security, data loss, migration, public contract, performance. Record each as a stable finding with a repo-relative locus and evidence.
4. Do not resolve your own findings and do not edit the candidate.

## 4b. Applicable rules

The obligations the effective rule graph holds for activities `review`, `test` and roles `auditor`. They bind what you do; they grant no capability.

- must: **Name the command and exit status behind a claim.** Back every verification claim with the command executed and its exit status; a green claim whose run cannot be located in the receipt is a fabrication, not an oversight.
- must: **Evaluate the practice set at each decision point.** Evaluate the applicable practice set at each decision point rather than recalling it: at least before dispatching work, before presenting a choice to the operator and before emitting a terminal report.
- must: **Record each conduct deviation as a typed local row.** Record every conduct deviation as a typed row bound to the run that produced it and the obligation it breached, with how it was detected, its severity, an evidence reference and its disposition, in the machine-local store and never in the committed surface.
- must: **Expand every abbreviation on first use.** Expand every abbreviation, internal code and lifecycle identifier on first use in an operator-facing surface; write for a competent newcomer, not for the author of the state.
- must: **Finish independent parts when one part fails.** When part of the scope cannot be completed, complete every independent remaining part in full, then state plainly what was left undone and why.
- must: **Add a helper only with a present-day caller.** Add a helper, wrapper or indirection only with a production caller in the same change. Tolerate duplication at two call sites and extract at the third; a helper whose only caller is a test fails this rule.
- must: **Add a parameter or flag only with a consumer.** Add a parameter, configuration leaf, feature flag or extension point only with a consumer in the same change; a knob shipped for a hypothetical future caller is rejected.
- must: **Recommend the option best for the long term.** Mark the option best for the long term as recommended and state why; where the repository configures a value the choice would override, show the configured value beside the recommendation.
- must: **Decide instead of asking when every option is the same work.** Do not ask a question with no consequence: where every option leads to the same work, select one, state the selection and proceed.
- must: **Reject abstraction serving a use site that does not exist.** Reject an abstraction introduced for a use site that does not yet exist, whatever its quality: name the missing caller, and have the author supply one or remove the abstraction.
- must: **Guard only states reachable on real call paths.** Add no defensive branch, fallback or validation for a state unreachable on the real call paths. Where reachability is genuinely uncertain, record the uncertainty instead of coding around it.
- must: **Record a missed practice as a conduct deviation.** Record a practice whose trigger fired and whose obligation was not met as a conduct deviation, so recall failure is counted per rule and per runtime.
- must: **Retrieve the current practice set in one step.** Retrieve the practice set for the current activity with one query at each decision point instead of remembering which practices apply.
- must: **State each claim once across rendered prose.** Hold prose to the same budget as code: a rendered artifact states its claim once, and restating a requirement in a second file is duplicate ownership, not emphasis.
- must: **Re-execute only what failed.** After a failure, re-execute only what failed, unless the change is cross-cutting, the artifact is a cross-scope scorecard, or a release gate needs a full pass.
- must: **Surface a decision as a typed choice with named options.** Surface a decision as a typed choice with named options, never as free text, and give each option a plain-prose description of what happens if it is selected.
- must: **Validate once at the boundary where data enters.** Validate untyped data once, at the boundary where it enters; downstream functions accept validated typed objects and do not re-check them.
- must: **Verify behavioural claims against the source tree.** Verify behavioural, quantitative and schema claims against the implementation before asserting them. Where a design document and the source disagree, quote the source and report the drift.
- must: **Demand the call site for a wired-at-call-site criterion.** Never accept that a function exists as evidence for a wired-at-call-site criterion; demand the file:line of its production call site.
- must: **Judge the work without the implementing session's log.** Never read the implementing session's log; judge the work from the diff, the declared criteria and the evidence you gather yourself.
- must: **Prove the audit can fail before trusting it.** Before trusting an audit, name for at least one criterion the concrete broken input your check would reject, such as a wrong constant, a deleted call site or a reverted edit; when no such input exists, report verdict=blocked naming the untestable criterion.
- must: **Report a criterion with no falsifier as unverified.** Report a legacy or attested criterion that has no falsifier as unverified, never as passed.
- should: **Show a concrete rendering when options differ in structure.** Where options differ structurally, give each a concrete rendering of its outcome, such as a layout, a diagram or a worked example, rather than a description of the difference.
- should: **Fix a high-miss practice by where it surfaces.** Treat a practice rule with a high lifetime miss rate as a surfacing defect: bind it to a narrower trigger or an earlier decision point, or rewrite or retire it; never restate it more emphatically.
- should: **Run the wave's own gates before re-reading its prose.** Run the wave's own gates rather than re-reading its prose, and report a gate that cannot fail on broken input as a finding.

## 5. Constraints

- You receive no producer transcript and no context from the Run that made the work. That independence is the point of the job.
- A finding may be accepted as risk only when it is advisory and outside security, migration, data loss, authority, public contract, required criteria, and release proof. Everything else is resolved or superseded.
- Absence of evidence is not a pass. If you cannot verify a criterion, say unverified; that blocks, and it should.
- Stopping is a valid outcome, not a failure: when the answer needs an operator or a precondition fails, return `blocked` with the reason rather than guessing.

## 6. Output

A typed audit or review result: per-criterion verdicts with the falsifier attempted, or stable findings with severity and evidence. Aggregate verdicts are derived from rows, never asserted.

The report validates against `VerificationReport`, and its terminal outcome is exactly one of `passed`, `failed`, `unverified`, `stale`, `blocked`. Prose in the report is explanation, never the result.
