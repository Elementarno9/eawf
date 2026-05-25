# P27-I05 backlog closeout audit (11 delivered/stale items)

## Summary

- W27 closes 11 backlog items that the 2026-05-24 I05 triage classified category-D ("already-covered / stale"): B003, B015, B021, B026, B028, B044, B056, B064, B010, B048, B065. Each behavioural claim was re-verified against the current (post-regroup) source tree per the verify-before-claim rule, and each close pins the commit that delivered (or superseded) the covering code. Overall verdict **pass**: every item is either materialised in the tree with file evidence or is doc-only bookkeeping whose premise has been superseded [1].
- Eight items are delivered features still present in the tree; two (B010, B048) are satisfied by superseding machinery; one (B065) is obsolete doc-only bookkeeping. No code change ships in W27 — it is a backlog-rollout state mutation that records the evidence chain in `state.json`.
- **B003 — delivered (P25-W10).** `OpenCodeAdapter` implements the `RuntimeAdapter` protocol (real session / error-class impl, not a stub) at `src/eawf/runtime/runtimes/opencode/adapter.py` [2].
- **B015 — delivered (P13-W05).** Session-level plugin-mode hooks map Eä events to Claude Code hook events: `runtimes/claude/hook_map.py` + `hooks_router.py` + `surfaces/render/hooks.py` [3].
- **B021 — delivered (P19-W20).** `eawf artifact verify` recomputes sha256 for registered artifacts (`artifact_verify` + `_verify_one_artifact`) at `surfaces/cli/commands/evidence_artifact.py`; the "target P09" reference was stale [4].
- **B026 — delivered (P08-W02).** Wave DAG persistence: `Wave.deps` + `Wave.blocks` edges, per-wave status enum, worktree branch tracking on `kernel/state/models.py`, plus `wave graph` / `next-ready` verbs; the "target P08 orchestration" reference was stale [5].
- **B028 — delivered (P08-W04).** Per-wave token-budget cap field `Wave.token_budget` on `kernel/state/models.py` with the `estimation.budget.enforce` config default; the enforcement ladder is tracked separately as B066 -> v0.4 [6].
- **B044 — delivered (P19-W19).** AGENTS.md sync-from-decisions render path: `surfaces/render/agents_md.py` plumbs typed Decision records into the Decisions section and `surfaces/cli/commands/sync.py` rebuilds the managed region [7].
- **B056 — delivered (P14-W03).** Profile-config `runtime.adapters` list with `runtime.kind` deprecated alias: `_normalise_runtime_adapters` in `kernel/config/layered.py` synthesises `adapters` from legacy `kind` with a deprecation warning; the explicit list wins [8].
- **B064 — delivered (P19-W15).** `normalize-coauthor` commit-msg hook shipped as `tools/normalize_coauthor.py` (+ `tools/coauthor_policy.py`, `tools/insert_coauthor.py`) [9].
- **B010 — superseded (P17-W05).** The v0.1.0 manual release-tag concern is superseded by the release-notes renderer `surfaces/render/release_notes.py` / `eawf release notes`; the live v0.3 tag + notes ride W23/W24 [10].
- **B048 — superseded (P27-I01-W27).** The "project version-target setter (v0.3 prep)" intent is satisfied by the canonical `tools/version_bump.py` rewrite path plus the `current_target_version()` getter; W23 drives the 0.2.0 -> 0.3.0 bump through it [11].
- **B065 — obsolete (D09).** The `defer:publication` tag marker is doc-only bookkeeping; its premise collapsed when D09 un-deferred B005 (PyPI publish) to v0.3. The remaining publication items are tracked individually; the tag itself blocks nothing [12].

## Followup triage

None for the closed set. Adjacent open items remain intentionally deferred: B066 (token-budget enforcement -> v0.4), and the publication-group items B001/B002/B004/B016/B017 (-> v0.4+ distribution). B005 (PyPI publish, the lone v0.3 blocker) is W24's deliverable.

## References

[1] `.ea/state.json` — `state.backlog["B003"|"B015"|"B021"|"B026"|"B028"|"B044"|"B056"|"B064"|"B010"|"B048"|"B065"]` resolution + resolving_commit + audit linkage to `state.audits["A39-P27-i05-backlog"]`; triage input `.ea/local/research/2026-05-24-i05-backlog-triage.md`
[2] `src/eawf/runtime/runtimes/opencode/adapter.py` (`class OpenCodeAdapter`); resolving commit `2188c5a [P25-W10]`
[3] `src/eawf/runtime/runtimes/claude/hook_map.py`, `src/eawf/runtime/runtimes/claude/hooks_router.py`, `src/eawf/surfaces/render/hooks.py`; resolving commit `deec936 [P13-W05]`
[4] `src/eawf/surfaces/cli/commands/evidence_artifact.py` (`artifact_verify`, `_verify_one_artifact`); resolving commit `32c31ba [P19-W20]`
[5] `src/eawf/kernel/state/models.py` (`Wave.deps`, `Wave.blocks`); resolving commit `473ab21 [P08-W02]`
[6] `src/eawf/kernel/state/models.py` (`Wave.token_budget`), `src/eawf/kernel/config/defaults.py` (`estimation.budget.enforce`); resolving commit `f778cb3 [P08-W04]`
[7] `src/eawf/surfaces/render/agents_md.py`, `src/eawf/surfaces/cli/commands/sync.py`; resolving commit `005b93f [P19-W19]`
[8] `src/eawf/kernel/config/layered.py` (`_normalise_runtime_adapters`); resolving commit `7aee720 [P14-W03]`
[9] `tools/normalize_coauthor.py`; resolving commit `b92f854 [P19-W15]`
[10] `src/eawf/surfaces/render/release_notes.py`; resolving commit `c076b01 [P17-W05]`
[11] `tools/version_bump.py` + `current_target_version()`; resolving commit `877bf87 [P27-I01-W27]`
[12] D09 ("Defer B005 PyPI publish to v0.3"); resolving commit `ed387d6 [P13-CORE]`

## Provenance

- audit_id: A39-P27-i05-backlog
- audit_kind: evaluation
- scope_id: P27-I05
- verdict: pass (11 items verified delivered/superseded/obsolete with commit + current-tree evidence)
- created_at: 2026-05-25
- author: claude-opus-4-7 (session flow-p27-i05-w27)
- supersedes: none

## Scrub

- status: clean
- references: repo-relative paths + commit SHAs + decision/backlog ids only
- local paths: none in body
- real emails: none
- abstract placeholder names: not applicable
