---
name: ship
description: "Close out a phase by running the full local CI surface, opening the phase PR, and (after merge) advancing state."
argument-hint: "<phase-id> [--dry-run]"
user-invocable: true
disable-model-invocation: true
---

# /ship

## Cross-links

`/ship` reads the phase `CloseReadiness` projection (gate-pack aggregate + `EvidenceRecord` summary + outstanding follow-ups) to decide what gates still need clearing. The phase-close commit only lands once `CloseReadiness.status == "ready"`. The phase-PR body is synthesized from the same projection so reviewer and tool see the same shape. `MEMORY` mutations driven by ship (e.g. release-notes entries) carry an explicit `MutationKind` for downstream audit.

The per-criterion close gate runs `run_oracle` (`workflow/verify/oracle.run_oracle`) over each wave's `CriterionSpec`. Enforcement is advisory by default — the band-scoped `verify.enforce` bit defaults to `False`, so a failing oracle or a cross-vendor jury veto is surfaced advisory rather than blocking the close unless a quality band opts in. The trust scorecard (`workflow/estimation/trust_scorecard.TrustScorecard`) reads the closed-wave store projection live, so ship's EU-calibration tier is read off real history rather than recomputed.

## Canonical algorithm

1. Resolve `<phase-id>`; verify all waves under it are complete.
2. Run the local verification gauntlet (pre-commit, mypy, pytest, ruff).
3. Validate artifact markdown and PR prose against the chassis/scrub rules.
4. Push the long-running feature branch.
5. Open the phase PR via `gh pr create`.
6. **PR-review pass.** Read remote review comments via `gh pr view <PR> --comments` (or the inline equivalent). For each actionable finding, append a follow-up wave to the current iter via `eawf roadmap revise --add-wave` (not a new iter — per the `iter-phase-close-timing` rule). Implement, re-push, wait for green CI, re-request review until clean.
7. **Bundle close in the final pre-merge commit.** Once CI is green and the review-passed branch is on the remote, emit a single `[P<NN>] state: close iter + phase (audit=<id>)` commit (the legacy `[P<NN>-CORE] state: ...` form remains valid per the `commit-prefix` block in AGENTS.md) that bundles `eawf iter close P<NN>-I<MM>` + `eawf phase close P<NN>` (no other touched files). The operator merges that commit to end the phase.

## Pre-flight checklist

- [ ] All waves under `<phase-id>` are complete.
- [ ] Cherry-picks from worktree subagents have all landed.
- [ ] `eawf artifact validate` passes for promoted markdown.
- [ ] CI on the latest push is green.
- [ ] `/audit` and `/polish` have already run on the iter — phase close is gated on both per `iter-phase-close-timing`.

## Decision surfaces

`gh pr create`, `gh pr merge`, and any push to a protected branch are irreversible/visible-to-others actions per AGENTS.md — surface the final confirm through `AskUserQuestion` (options: `proceed` / `defer` / `abort`) unless `vcs.auto_push`, `vcs.pr_open`, and the merge strategy are pre-resolved by config.

## Output contract

Skill envelope carrying the PR URL, the post-merge state mutation, and any deferred follow-ups.
