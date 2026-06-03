---
name: math-explainer
description: Author a verification-grounded math-explainer over typed MathClaim/MathExplainer rows: each claim pins intuition + a runnable CI-checked example gate + assumptions/regime + a canonical citation, run through an in-skill clarity loop (vale-prose + EAWF019 + draft validate). No state mutations.
argument-hint: "<explainer-slug> [--final] [--from-brief <path>]"
user-invocable: true
disable-model-invocation: true
---

# /math-explainer

## Purpose

Author a math-explainer a non-expert can trust. Agents are strong at olympiad-style math and weak at research-level conceptual math, and the dominant failure is a fluent, confident, *invalid* derivation rather than a refusal — math that *looks* execution-grounded but is not. The defence is the same discipline that lets a reader who cannot check the math act on it: pin every claim to an executed check. `/math-explainer` drives that contract over the typed `MathExplainer` + `MathClaim` rows (`kernel/spec/math.py`), running an in-skill clarity loop so a draft that fails the newcomer / facet test is revised, not emitted. Read-only: it writes only under `.ea/local/`.

## The four-facet per-claim contract

Every `MathClaim` carries all four facets — Pydantic refuses to construct one that drops any:

1. **Intuition** — prose that *explains* why the statement holds (the analogy, the shape of the argument), not a citation standing in for the conceptually-hard step. Substituting a reference for the hard step is the non-expert trap.
2. **Runnable example** — a `GateSpec` (`command_exit_zero`) hosting the verifier: a CAS identity, a high-precision numeric cross-check, a property test, a units check, an SMT counterexample hunt, an interval-arithmetic bound, or a proof-assistant obligation. The claim is *trustable* only when this gate actually runs in CI.
3. **Assumptions / regime** — the one-line regime where the claim holds plus the explicit assumptions it rests on. This is what tells a non-expert *when not to trust the result*.
4. **Citation** — a canonical `EvidenceRef` (audit / artifact / decision / store URN or external URL) that resolves and entails the claim.

Route by claim shape and refute before you certify: most verifiers (CAS, numeric, property test, units, SMT-sat, reference-DB lookup) only *refute* — record `assurance="refute"`; reserve `assurance="certify"` for validated-interval arithmetic and a kernel-checked proof. Require a second independent path before marking any conceptual claim resolved.

## Canonical algorithm

1. Resolve the explainer slug from the argument (or `AskUserQuestion` if absent). The slug stems the artifact: `<YYYY-MM-DD>-<slug>-math.md` plus its typed sidecar `<YYYY-MM-DD>-<slug>-math.json`.
2. Draft each claim with all four facets. Write the intuition first; never hand-write SDE / derivation math from memory — ground it against the source code or a cited reference and mark intuition-vs- rigor.
3. Wire each claim's `example_gate` to a real verifier command and pin its `verifier` + `assurance` hints. The gate is dead unless its `args['argv']` is a non-empty vector the runner can execute.
4. Run the in-skill clarity loop (below). A claim that fails any leg is revised in place — do not emit a draft that fails the loop.
5. Write the `MathExplainer` JSON sidecar and the chassis-backed markdown companion under `.ea/local/research/`, then return the output envelope. On `--final` with residual unknowns > 0, emit a `/blitz` follow-up under the standard recursion guard.

## The in-skill clarity loop

Three layers, each with exactly one owner (no double-enforcement):

- **Form (prose)** — `eawf hook vale-prose <doc>-math.md`: unglossed jargon, a hedge with no number, notation-spelling consistency, the readability of the intuition. Cannot see structure or correctness.
- **Structure / binding** — `eawf hook eawf019-math-facets <doc>-math.json`: every claim carries the four facets; each citation resolves to a reference; every fenced example is actually collected by the runner (the silent-skip regression where a "tested" pin runs nothing); each formula parses. Cannot run the math.
- **Draft artifact** — `eawf draft validate <doc>-math.md`: the chassis sections (Summary / References / Provenance / Scrub), dense `[N]` citations, and the scrub gate (no machine paths, host-local URLs, or PII).

Correctness itself (the gate-runner executing each verifier) and entailment (a *different* model confirming the intuition entails the formula and the citation supports the claim) sit beyond the in-skill loop — the gate-runner owns truth, the L3 judge owns meaning. A lint can guarantee a claim *has* a verifier of the right type; only running it shows the math holds.

## Promotability

A `MathExplainer` is constructible while its claims are ungrounded but `is_promotable()` is false until *every* claim's gate is runnable and its citation resolves — the same EviBound shape as `IntentBrief.evidence_refs`. A claim is promoted on verification- grounding (gate resolves + citation resolves + judge confirms the intuition entails), never on author confidence. The local draft promotes to `.ea/artifacts/` only when it informs a decision recorded in `state.json` (the artifact-chassis rule then applies).

## Pre-flight checklist

- [ ] No state mutations — read-only; writes only under `.ea/local/`.
- [ ] Explainer slug matches the wave / iter / phase prefix so dispatch renderers surface this brief.
- [ ] Every claim carries all four facets — intuition, runnable example gate, assumptions/regime, canonical citation.
- [ ] No claim substitutes a citation for the conceptually-hard step; the intuition explains, not asserts.
- [ ] Each `example_gate` is a real `command_exit_zero` gate with a non-empty argv — no cosmetic "tested against" pin.
- [ ] `verifier` + `assurance` hints set; `certify` reserved for interval arithmetic and proof assistants.
- [ ] Derivation math grounded against source / a cited reference — none hand-written from memory.
- [ ] `eawf hook vale-prose`, `eawf hook eawf019-math-facets`, and `eawf draft validate` all clean.
- [ ] Citations repo-relative, external URL, or eawf URN — no absolute local paths, no host-local URLs, no PII.

## Output contract

Eä-rendered skill envelope (`OutputEnvelope`) with `header.skill = "/math-explainer"`. Body carries the explainer slug, the markdown + JSON artifact paths, the claim count, the per-claim verifier
+ assurance tally, the clarity-loop leg statuses (vale-prose / EAWF019 /
draft validate), the promotability verdict, and any residual unknowns.
