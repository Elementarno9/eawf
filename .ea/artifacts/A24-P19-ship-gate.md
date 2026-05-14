# A24-P19 ship-gate audit

## Summary

- P19 state records W01 through W09 closed; W10 carries this audit as the
  final phase-close deliverable [1].
- Lifecycle: explicit `PLANNED -> ACTIVE -> CLOSED` transitions for phases
  and iters with append-only mutation tiers shipped in W01, locking
  the planned-scope revisability contract that the rest of the phase
  builds on [2].
- Wave claim gating: deps + W## monotonic order enforced by
  `wave claim`, with an `--out-of-order` escape hatch for parallel
  worktree dispatch (W02) [3].
- Phase-close hardening: at least one closed wave required before
  `phase close` accepts the audit (W03) [4].
- `Wave.commit` field dropped from the state model; SHAs are now
  derived from `git log --grep '[P##-W##]'` at render time so commit
  history is the durable signal across cherry-pick + rebase (W04) [5].
- Commit-prefix lint tightened: bare `[P##]` rejected, `-W##` or
  `-CORE` suffix mandatory, `[P##-CORE]` restricted to state-only
  paths (W05) [6].
- Roadmap planner CLI lands `propose`, `revise`, `apply`, `drop`,
  `show` for phase-at-a-time roadmap edits, with `EVENT` envelopes on
  every state mutation (W06) [7][8].
- `/prep` activates the next PLANNED phase, runs the V11 hard gate,
  and dispatches the wave DAG (W07) [9].
- AGENTS.md updated with the lifecycle + roadmap procedure, the
  naming conventions for fields/params/log keys, and the planned-
  scope revisability contract (W08) [10].
- W09 reactive wave synced the golden fixtures against the W04
  `Wave.commit` drop and the W08 AGENTS.md re-wrap; CI on PR #16
  went green on all four py3.14 runners after the fix [11][12].
- Local verification: `uv run pytest -q` (full suite green),
  `uv run pre-commit run --all-files` (clean) prior to phase-close
  state mutations [11].

## References

[1] .ea/state.json
[2] src/eawf/lifecycle/transitions.py
[3] src/eawf/lifecycle/wave_claim.py
[4] src/eawf/lifecycle/phase_close.py
[5] src/eawf/lifecycle/wave_sha.py
[6] tools/commit_prefix_lint.py
[7] src/eawf/cli/commands/roadmap.py
[8] src/eawf/render/envelope.py
[9] src/eawf/cli/commands/prep.py
[10] AGENTS.md
[11] tests/golden/scenarios/test_scenarios.py
[12] tests/golden/plan_view/conftest.py

## Provenance

- kind: ship-gate
- phase: P19
- iter: P19-I01
- audit_id: A24-P19
- artifact_id: ART-A24-P19
- scope_id: P19
- branch: main (post-merge of feature/eawf-v0.4-p19, PR #16)
- verification: `uv run pytest -q`; `uv run pre-commit run --all-files`

## Scrub

- status: clean
