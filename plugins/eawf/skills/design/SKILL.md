---
name: design
description: "Read-only design pass for an interactive surface: produces a statechart + action x context matrix + journey scenarios backed by an 11-rule lint contract. No state mutations."
argument-hint: "<surface-slug> [--final] [--from-brief <path>]"
user-invocable: true
disable-model-invocation: true
---

# /design

## Purpose

Prevent the P20-I03 class of failure: a phase that audit-passes its declared criteria but breaks the lived user experience at runtime because a transition, a refresh trigger, or a multi-keystroke dispatch was never enumerated in the design step. `/design` produces a triangulated design spec — statechart + action x context matrix + journey scenarios — backed by a six-category event taxonomy, per-view liveness contracts, a multi-keystroke substate rule, cross-component contracts, and an 11-rule lint contract. It runs after `/research --final`, before `/roadmap propose`. Read-only: it writes only under `.ea/local/`.

## Canonical algorithm

1. Resolve the surface slug from the argument (or `AskUserQuestion` if absent). The slug suffixes the artifact stem: `<YYYY-MM-DD>-<surface>-design.md`.
2. Round 1 — personas + goals. AUQ batch; every selected persona has
   >=1 goal and every (persona, goal) gets a one-sentence success
criterion.
3. Round 2 — event sources + statechart + liveness contracts. AUQ batch. The six-category event taxonomy is mandatory; every long-lived view requires a liveness contract row; every multi-char key sequence requires a substate chain (never a flat `KEY_oH`).
4. Round 3 — action x context matrix. AUQ batch. No blank cells (`-` is the only legal intentional no-op); matrix columns are parity-locked to statechart states.
5. Round 4 — journey scenarios. AUQ batch. >=1 scenario per (persona x goal), >=1 time-advance scenario per long-lived view, plus edge scenarios per declared failure mode.
6. Self-lint. Walk the 11-rule lint contract (L1..L11); round-close is gated on a lint pass.
7. Write `.ea/local/research/<YYYY-MM-DD>-<surface>-design.md` using the artifact chassis and return the output envelope. On `--final` with residual unknowns > 0, emit a `/blitz` follow-up under the standard recursion guard.

## The three load-bearing rigour mechanisms

These are the additions over a generic statechart-plus-matrix doc; each catches one concrete P20-I03 defect at design-review time:

- Per-view liveness contract — caught the live git pane that never rebound to a refresh trigger.
- Six-category event taxonomy — caught the missing periodic tick (round 2 rejects close when a long-lived view declares no refresh trigger).
- Multi-keystroke substate rule — caught the verb-prefix sequence wired flat (lint L6 rejects flat `KEY_oH` entries; the substate chain is mandatory).

## Lint contract (L1..L11)

Until the `eawf design lint <path>` binary lands, the skill agent walks the rules in-conversation at the end of rounds 3 and 4. A round does not close while any rule fails. The rules cover statechart/matrix parity (L1, L2), no surprise events (L3), no unfilled cells (L4), liveness-row completeness (L5), substate-backed multi-char keys (L6), scenario coverage (L7, L8), scrub-clean citations (L9), cross-component rows resolving to real test paths (L10), and append-only revisions (L11).

## Design to audit loop

When a phase touches an interactive surface, the ship-gate audit binds to the design artifact and adds criteria A1..A4 (every statechart `on:` clause, every non-`-` matrix cell, every Gherkin scenario, and every liveness contract has a covering test). Without this loop the design artifact becomes write-once advisory and the P20-I03 class recurs.

## Pre-flight checklist

- [ ] No state mutations — read-only; writes only under `.ea/local/`.
- [ ] Surface slug matches the future wave / iter / phase prefix so dispatch renderers surface this brief.
- [ ] Event taxonomy populated for all six categories (present or N/A with a one-sentence reason).
- [ ] Statechart `on:` clauses complete; no named state has an unhandled named event.
- [ ] Every long-lived view has a liveness contract row with all five fields.
- [ ] Every multi-char key sequence is a substate chain — no flat `KEY_oH` entries.
- [ ] Matrix has no blank cells; `-` is the only legal no-op value.
- [ ] >=1 journey scenario per (persona x goal); >=1 time-advance scenario per long-lived view; edge scenarios per declared edge category.
- [ ] Cross-component contracts list every cross-module event with its integration test path.
- [ ] Lint L1..L11 all pass.
- [ ] Citations repo-relative, external URL, or eawf URN — no absolute local paths, no host-local URLs, no PII.

## Decision surfaces

Every round's picks (personas, goals, event sources, statechart shape, matrix cells, scenarios) are surfaced through `AskUserQuestion` batches, never free-text. `/design` is convention-only: no state field, no `/roadmap propose` hard gate. The artifact is cited from the roadmap proposal body and from each wave dispatch prompt; dispatch renderers surface design artifacts under a `## Design references` section by filename match.

## Output contract

Eä-rendered skill envelope (`OutputEnvelope`) with `header.skill = "/design"`. Body carries the surface slug, the artifact path, the rounds completed, the declared / N/A event sources, the screen + long-lived-view counts, the matrix fill tally, the scenario count, the lint status, and any residual unknowns.
