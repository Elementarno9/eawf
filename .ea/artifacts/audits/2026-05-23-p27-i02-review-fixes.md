# P27-I02 review-disposition closure audit

## Summary

- P27-I02 delivered the P27 critical-review backlog (W01-W26 original waves + W27-W29 stage-3 audit fixes). Stage-4 polish (W30) and a fresh-context `/review` over the full I02 diff surfaced 1 security + 3 correctness must-fix + 7 should-fix; the operator elected fix-all-now. Nine fix-waves W31-W39 resolve every must-fix and should-fix or consciously defer it to I03. Overall verdict **minor**: every must-fix is fixed and verified, the full suite is green (5593 passed, 27 skipped), and four non-blocking follow-ups are deferred to I03 [1].
- Audited HEAD: `cf82508` on `feature/eawf-v0.3-p27`. Verification chain: five fresh-context review agents (four domain reviewers over the W01-W39 diff + one daemon-cluster re-reviewer) plus two full gauntlets (`3619b56` and `cf82508`); each fix-wave cites its commit on the feature branch [1].
- **W33 — compressed-IPv6 scrub leak (security) — pass.** `SensitiveScrubber` now redacts leading-`::` / fully-compressed / v4-mapped IPv6 literals (`fe80::1`, `::1`, `::ffff:192.168.0.1`) that previously leaked; 9 regression tests incl. negatives [2].
- **W31 + W39 — daemon validation RPC code — pass.** Validation rejections emit `-32002` (ValidationFailed / exit 2) for the closure kinds (`PHASE_CLOSE`/`ITER_CLOSE`/`WAVE_CLOSE`); every other lifecycle-guard rejection emits `-32602` (InvalidInput / exit 1) matching the in-process fallback, so daemon-up and daemon-down agree on exit code. W39 corrected a parity regression W31 introduced (over-broad `-32002`), caught by the daemon-cluster re-review; wire-contract + non-closure parity tests added [3].
- **W32 — transport-fallback double-apply + WAL ordering — pass.** A post-send transport drop now raises `DaemonMutationIndeterminate` instead of blindly re-running the in-process fallback (no double-apply); `wal.mark_applied` moved before `append_envelope` in both daemon and fallback so a crash in the state-written/event-missing window leaves an APPLIED record that `replay_wal` re-issues [4].
- **W34 — migration runner safety net — pass.** `run_chain` round-trips `State.model_validate` before `write_canonical` and restores from backup on failure; an empty/whitespace migrated title becomes a `min_length>=1` placeholder, closing a silent-brick path [5].
- **W35 — /ship build gate — pass.** `_run_gauntlet` runs every requested gate that resolves to a command (a configured `build` gate is no longer silently dropped); abort-on-red preserved [6].
- **W36 — phase-close atomicity — pass.** `iters_without_audit` and `single_wave_without_decision` moved into the `close_phase` transition so both the daemon-proxy and in-process paths enforce them atomically under the lock [7].
- **W37 — decision supersede cycle guard — pass.** `supersede_decision` rejects a non-ACTIVE superseder (blocks A->B->A) and `INV.DECISION.SUPERSEDE_CYCLE` walks the supersede chain as a backstop [8].
- **W38 — audit dispatch guard + projector mtime + nits — pass-with-followups.** Malformed auditor-dispatch guard, projector size+mtime skip-gate, backfill docstring, `_TS_PATTERN` `\Z`, and the duckdb all-PK upsert landed; three items deferred (see triage) [9].
- Cross-cutting: rule-25 clean (no design-decision provenance in the source diff); new ingestion fields ride `extra="forbid"` models; ruff + mypy (466 files) + pre-commit (14 hooks) + full pytest all green [1].

## Followup triage

Four non-blocking follow-ups are deferred to P27-I03; none gate the I02 close.

- **DEFER (I03) — /audit zero-check -> partial (W38 fix A).** Emitting `partial` when zero checks run collapses the `/flow` happy-path (flow breaks on any non-`ok` status); it needs a flow short-circuit relaxation first, so it ships as its own I03 wave [9].
- **DEFER (I03) — snapshot golden-dir alignment (W38 fix G).** Aligning the `tui` vs `tui_config_modal` golden roots would physically move golden files; both resolve today, so it is held for a dedicated golden-move wave [9].
- **DEFER (I03) — pr_body phase-id match (W38 fix H).** The substring match runs against the 72-char-capped title; no clean phase field exists on `Decision` to match instead, so it is held rather than inventing a field [9].
- **DEFER (I03) — WAL-dir fsync after mark_applied (W32 follow-up).** The S7 reorder's crash-safety holds for process-kill/exception crashes; true power-loss durability of the `.pending`->`.applied` rename needs a WAL-dir fsync. Pre-existing and architectural [4].

## References

[1] `.ea/state.json` — `state.waves["P27-I02-W30".."P27-I02-W39"]` outcome strings + commit chain on `feature/eawf-v0.3-p27`; audited HEAD `cf82508`; full suite 5593 passed, 27 skipped (`-n auto`, eval + tui-snapshot dirs excluded per CI)
[2] commit `39c9c4e [P27-I02-W33] fix: scrub compressed and leading-:: IPv6 literals` + `src/eawf/logging/scrub.py` + `tests/unit/test_log_scrub.py`
[3] commits `d19b50e [P27-I02-W31] fix: daemon emits -32002 for validation_failed + live client mapping` and `92a3124 [P27-I02-W39] fix: align daemon validation exit codes with the in-process fallback` + `src/eawf/daemon/{server.py,methods/state.py}` + `src/eawf/cli/{_dispatch.py,errors.py}`
[4] commit `3619b56 [P27-I02-W32] fix: guard transport-fallback double-apply + reorder WAL mark_applied before event append` + `src/eawf/cli/{_dispatch.py,_daemon_client.py,commands/lifecycle.py}` + `src/eawf/daemon/{methods/state.py,wal.py,recovery.py}`
[5] commit `d56695e [P27-I02-W34] fix: round-trip State.model_validate in run_chain + handle empty migrated titles` + `src/eawf/migrations/{_base.py,v1_0_to_v1_1.py}`
[6] commit `f6ff4d1 [P27-I02-W35] fix: run configured build gate in /ship gauntlet` + `src/eawf/skills/ship.py`
[7] commit `c1fc0f9 [P27-I02-W36] fix: enforce phase-close blockers in close_phase transition` + `src/eawf/lifecycle/phase.py` + `src/eawf/cli/commands/lifecycle_phase.py`
[8] commit `a9a25e8 [P27-I02-W37] fix: reject superseding with a non-ACTIVE decision to block supersede cycles` + `src/eawf/evidence/decision.py` + `src/eawf/validate/invariants.py`
[9] commit `f1fda43 [P27-I02-W38] fix: audit malformed-dispatch guard + projector mtime gate + review nits` + `src/eawf/skills/audit.py` + `src/eawf/telemetry/projector.py`

## Provenance

- audit_id: A36-P27-i02-fixes
- audit_kind: ship-gate
- scope_id: P27-I02
- verdict: minor (every must-fix + should-fix resolved or deferred; full suite green at 5593 passed; four non-blocking follow-ups deferred to I03)
- created_at: 2026-05-23
- author: claude-opus-4-7 (session eawf-flow-p27-i02)
- supersedes: none

## Scrub

- status: clean
- references: repo-relative paths + commit SHAs only
- local paths: none in body
- real emails: none
- abstract placeholder names: not applicable
