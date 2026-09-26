---
name: release
description: "Prove, preflight, approve, publish, observe and recover a release."
argument-hint: "<create|show|pin|preflight|approve|publish|observe|retry-target|recover> [<release-ref>] [--version <version>] [--milestone <ref>...] [--channel <dev|rc|stable>] [--source <sha>] [--target <id>...] [--wait] [--resume <operation-ref>] [--dry-run]"
user-invocable: true
disable-model-invocation: true
---

# /release

Prove, preflight, approve, publish, observe and recover a release.

## 1. Authority

- Only an authenticated operator initiates this skill. An agent may prepare evidence or recommend the invocation, but never calls it.
- Effects: Release and external-operation RPCs.
- Allowed RPCs: `read_entity`, `query_evidence`, `release.create`, `release.pin`, `release.proof.prepare`, `release.approve`, `release.publish`, `release.observe`, `release.recover`, `operation.status`, `operation.resume`, `operation.cancel`. Any other RPC is denied before it reaches a handler.
- Canonical state changes only through those RPCs, and every mutating call carries `--expected-revision` and `--idempotency-key`.
- Local write root: none.
- Executable grants come from the compiled capsule of the enclosing scope alone; nothing on this page adds or widens a tool, path, RPC, credential or external effect.

## 2. Context

One Release, named by `<release-ref>` or drafted by this invocation, with its ReleaseTrain declaration.

Resolve the subject before acting. Name every entity with its identifier and its exact current revision so staleness is detectable; a fact without a revision is a summary, not context.

## 3. Task

You operate one Release from draft through observed publication using the selected action. Submission success never means publication success.

```text
/release <create|show|pin|preflight|approve|publish|observe|retry-target|recover> [<release-ref>] [--version <version>] [--milestone <ref>...] [--channel <dev|rc|stable>] [--source <sha>] [--target <id>...] [--wait] [--resume <operation-ref>] [--dry-run]
```

Select exactly one action: `create`, `show`, `pin`, `preflight`, `approve`, `publish`, `observe`, `retry-target`, `recover`. An option the selected action does not declare is refused before you start.

## 4. Method

1. Resolve the ReleaseTrain declaration, version/channel, membership, exact source, artifacts, manifests, target policy, gate receipts, and current operation attempts.
2. For create, construct a strict draft from accepted Milestones. For pin, freeze source and membership and invalidate any approval whose inputs changed.
3. For preflight, execute every required readiness signal against exact artifacts. Report all failures together with remediation; never weaken a gate to make the release ready.
4. For approve, present the frozen manifest and consequences through a protected action. The agent never chooses approval.
5. For publish, submit one durable external operation per target. Treat accepted, effected, and independently observed as distinct facts.
6. For observe, query each target through its observation adapter. For retry-target or recover, operate only missing or ambiguous legs and preserve the burned version and frozen artifacts.
7. Declare RELEASED only after every required target is independently observed with matching digests. Return operation references immediately unless `--wait` was requested.

## 4b. Applicable rules

The obligations the effective rule graph holds for activities `release`. They bind what you do; they grant no capability.

- must: **Measure projection size against each runtime cap.** Record the projection byte size and each certified runtime's declared cap in the render transaction, and fail the render when the projection is larger than any cap instead of shipping it truncated.
- must: **Record the goal before the first mutating action.** Before the first mutating action, record the task as a typed goal bound to the declared success criteria of the entity it serves.
- must: **Record a runtime cap only as a measured value.** Record a runtime's declared cap as a measured value with its measurement provenance, never as a figure chosen for headroom; report a cap not re-measured against a certified runtime version as stale.
- must: **Deliver every must rule by a deterministic vehicle.** Deliver every must rule through a certified deterministic vehicle, never through a retrieval command a model may decline to run; a retrieval command delivers only should and information rules.
- must: **Deliver the root projection in full to every runtime.** Deliver the root projection in full to every certified runtime. A runtime whose project-document cap is below the rendered size is a packaging failure, never the fault of the agent that then breaches an unreceived rule.
- must: **Name the runtime, cap and first lost rule on truncation.** Never truncate silently: when a host cap cannot be satisfied, the failure names the runtime, the cap, the rendered size and the first obligation lost past the boundary.
- must: **Render an unresolved view reference as a command.** Render an unresolved detailed-view reference as an actionable message naming the command that generates the view, never as a dangling path.

## 5. Constraints

- Stop on stale source, dirty or non-ancestor tree, version/channel mismatch, missing gate, invalid approval, ambiguous external result requiring operator action, or unsupported target capability.
- Stopping is a valid outcome, not a failure: when the answer needs an operator or a precondition fails, return `blocked` with the reason rather than guessing.

## 6. Output

Output one ReleaseSkillReport with frozen manifest digest, readiness matrix, approvals, per-target attempts/effects/observations, recovery state, and terminal release truth.

The report validates against `ReleaseSkillReport`, and its terminal outcome is exactly one of `shown`, `drafted`, `pinned`, `ready`, `not_ready`, `approved`, `submitted`, `observed`, `recovering`, `released`, `blocked`. Prose in the report is explanation, never the result.
