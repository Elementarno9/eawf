---
name: init
description: "Initialise a new Eä Workflow workspace. Renders managed regions of AGENTS.md and the .claude/ plugin tree."
argument-hint: "[--profile=<id>]"
user-invocable: true
disable-model-invocation: true
---

# /init

## Cross-links

Init persists a typed `Project` row (id, profile composition, default `RoleSpec` set, vcs cadence) — downstream skills resolve role and release-cadence config off the `Project` rather than re-reading the profile yaml. The init wizard emits a `MEMORY` seed entry (`MutationKind=create`) recording the project bootstrap so the live trust scorecard (`workflow/estimation/trust_scorecard.TrustScorecard`) has a base date. That scorecard is no longer write-idle — it reads the closed-wave store projection to score EU-calibration trust per output scope.

## Canonical algorithm

1. Discover existing `.ea/` (if any) and load profile composition.
2. Render managed regions of `AGENTS.md`, `CLAUDE.md`, `.claude/`.
3. Persist `.ea/state.json` and `.ea/profile.yaml`.

## Pre-flight checklist

- [ ] Working tree is clean before the first init.
- [ ] Profile composition is declared.

## Decision surfaces

When the wizard pauses on an unanswered question, the `status=needs_user` envelope routes the operator to an `AskUserQuestion` prompt for the missing field rather than guessing a default.

## Output contract

Skill envelope wrapping the wizard outcome. `status=needs_user` when the wizard pauses on an unanswered question.
