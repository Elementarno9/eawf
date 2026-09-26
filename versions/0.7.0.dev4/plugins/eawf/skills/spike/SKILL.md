---
name: spike
description: "Build, test, independently verify and present a local proof of concept."
argument-hint: "<idea...> [--hypothesis <text>] [--confirm <condition>] [--reject <condition>] [--from <ref>...] [--constraint <text>...] [--stack <auto|python|shell|node|other>] [--entrypoint <relative-path>] [--verify <command>...] [--fixture <ref>...] [--agents <2..8>] [--budget <spec>] [--slug <slug>] [--local-root <path-under-.ea/local/spikes>] [--network <deny|allow>] [--retention <keep|expire-after-review>] [--resume <folder>]"
user-invocable: true
disable-model-invocation: false
---

# /spike

Build, test, independently verify and present a local proof of concept.

## 1. Authority

- An operator or an authorized agent may initiate this skill. Agent invocation never widens authority: it needs an enclosing Run, Task or Campaign scope whose compiled capsule already grants every read, write, RPC, budget and external effect below.
- Effects: Writes only under the resolved local spike folder; separate builder and verifier Runs; extracted contracts are submitted for promotion.
- Allowed RPCs: `retrieve_source`, `run.dispatch`, `submit_report`, `submit_evidence`. Any other RPC is denied before it reaches a handler.
- Canonical state: never mutated by this skill.
- Local write root: `.ea/local/spikes`; nothing is written outside it.
- Executable grants come from the compiled capsule of the enclosing scope alone; nothing on this page adds or widens a tool, path, RPC, credential or external effect.

## 2. Context

The idea named by `<idea...>`, its hypothesis and discriminating conditions, and the local spike folder that holds the proof of concept.

Resolve the subject before acting. Name every entity with its identifier and its exact current revision so staleness is detectable; a fact without a revision is a summary, not context.

## 3. Task

Build the smallest runnable proof of concept that discriminates the stated idea. This is local experimental work, not a Campaign and not product implementation.

```text
/spike <idea...> [--hypothesis <text>] [--confirm <condition>] [--reject <condition>] [--from <ref>...] [--constraint <text>...] [--stack <auto|python|shell|node|other>] [--entrypoint <relative-path>] [--verify <command>...] [--fixture <ref>...] [--agents <2..8>] [--budget <spec>] [--slug <slug>] [--local-root <path-under-.ea/local/spikes>] [--network <deny|allow>] [--retention <keep|expire-after-review>] [--resume <folder>]
```

## 4. Method

1. Allocate `.ea/local/spikes/<date>-<slug>/` or the validated resume folder. Never write canonical stores, tracked product paths, or another local root.
2. Before coding, write `spike.yaml` with question, hypothesis, observable confirm and reject conditions, inputs, exclusions, safety/network policy, and hard budget.
3. Build real source or scripts, fixtures, and a runnable entrypoint. Optimize only for the stated discriminator; do not grow production architecture around the experiment.
4. Run the exact verification commands and capture bounded machine-readable observations, logs, environment assumptions, and result receipts. Then extract contracts from those observations: for each probed surface, state what it is, what it accepts and returns, and its non-empty boundary - the conditions under which the observation stops holding. An observation records that a run printed something; a contract records what the surface is. Emit each as a MeasuredContract and promote it through the evidence-promotion path so it becomes a canonical artifact a plan can cite by ArtifactUrn. A contract with an empty boundary is not extracted.
5. Stop building when confirm/reject condition is observed, the cap is reached, a safety boundary blocks work, or further progress requires production engineering.
6. Dispatch a fresh verifier agent with the folder, manifest, and commands but no producer transcript. The verifier reruns from clean instructions and returns pass, fail, or inconclusive.
7. The builder may repair within the remaining budget and request one fresh verification. READY requires an independent pass; self-verification never suffices.
8. Finish the folder with `README.md`, manifest, runnable entrypoint, fixtures, results, limitations, verifier report, and a short operator demo. Preserve it by default; never auto-delete uncommitted local work.
9. Promotion is a later `/plan` or Task action. This skill may recommend promotion but cannot commit, publish, or move the prototype into product source.

## 4b. Applicable rules

The obligations the effective rule graph holds for activities `research`, `test`. They bind what you do; they grant no capability.

- must: **Name the command and exit status behind a claim.** Back every verification claim with the command executed and its exit status; a green claim whose run cannot be located in the receipt is a fabrication, not an oversight.
- must: **Finish independent parts when one part fails.** When part of the scope cannot be completed, complete every independent remaining part in full, then state plainly what was left undone and why.
- must: **Re-execute only what failed.** After a failure, re-execute only what failed, unless the change is cross-cutting, the artifact is a cross-scope scorecard, or a release gate needs a full pass.
- must: **Verify behavioural claims against the source tree.** Verify behavioural, quantitative and schema claims against the implementation before asserting them. Where a design document and the source disagree, quote the source and report the drift.

## 5. Constraints

- Stopping is a valid outcome, not a failure: when the answer needs an operator or a precondition fails, return `blocked` with the reason rather than guessing.

## 6. Output

Output one SpikeReport containing folder, hypothesis, commands, observations, contracts, verdict, limitations, independent verifier result, operator demo, retention, and stop reason.

The report validates against `SpikeReport`, and its terminal outcome is exactly one of `ready`, `inconclusive`, `failed`, `cancelled`, `blocked`. Prose in the report is explanation, never the result.
