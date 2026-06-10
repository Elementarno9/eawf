# A31-P25 ship-gate audit

## Summary

- P25 (C03 spec infra + C07a runtime dispatch + C07b VCS events + C08 profiles) shipped 19 waves under iter P25-I01; phase verdict **pass** (full deliverable scope landed per the per-wave success criteria; pre-commit + mypy + ruff + pytest gauntlet all green; PR #22 review pass with zero findings across 271 files / +28,969 / -475) [1].
- W01 ship: `PhaseSpec` + `IterSpec` + `WaveSpec` Pydantic v2 schemas + common types under `src/eawf/specs/` (extra="forbid"; KPI block + tests block + non-empty enforcement on success_criteria) [2].
- W02 ship: `verify-implements` audit-DSL kind + cadence (grep + diff verdict markers) — declarative spec-to-source verification keyed off `spec.implements` [3].
- W03 ship: daemon spec writer (`state.write_spec` RPC) + spec cache + DRAFT/READY/IMPLEMENTED/ARCHIVED lifecycle [4].
- W04 ship: closed-wave `success_criteria` -> `WaveSpec` backfill writer + bulk-run; legacy closed-wave success criteria now project into the spec store [5].
- W05 ship: spec validators (KPI / non-empty / mockup heuristic / tests-real-paths) + pre-commit `spec-paths` hook gating PRs that reference missing test paths [6].
- W06 ship: canonical `Event` Pydantic model + event store per-kind JSONL layout (10 kinds under `.ea/store/`) [7].
- W07 ship: commit-prefix lint hardening (state-type carve-out + CORE-path allowlist + W## strict gate) + worktree home flipped from `.claude/worktrees/` to `.ea/worktrees/` + KISS-007 helpers [8].
- W08 ship: multi-repo registry under the user-scope `eawf/registry.json` + scope dispatch ladder (cwd -> workspace -> repo -> user) + explicit registry growth (no scan/walk/import-from-scan) [9].
- W09 ship: render envelope + `Eä` brand glyph (capital E + a-umlaut, bold accent, outside-left of breadcrumb) + Nerd-Font + ASCII fallback [10].
- W10 ship: `RuntimeAdapter` Protocol + 3 adapters (claude-code / codex / opencode) for plugin installation surface unification [11].
- W11 ship: `PluginManifest(BaseModel)` + `eawf plugin sync` regen verb + KISS-004 helpers [12].
- W12 ship: `eawf plugin doctor` with 4 drift kinds (missing / extra / hand-edited / version-skew) + KISS-001 coauthor env-detection opt-in [13].
- W13 ship: capability matrix (8 rows × 3 runtimes) + drift detector for runtime feature parity tracking [14].
- W14 ship: layered config taxonomy + branch layer + wave layer + ~170-key field registry + 1.0 schema_version migration [15].
- W15 ship: `ProfileBody` v2 + `conflicts_with` + `overrides` + composition loader (multi-profile merge with conflict surfacing) [16].
- W16 ship: 3 bootstrap templates (research + engineering + reverse-eng) + multi-profile init wizard plumbing [17].
- W17 ship: AGENTS.md rules 26 (/prep always plan-mode for Case A + Case B) + 27 (iter/phase close timing) + /flow re-order (research → prep → audit → ship → review → polish) + skill close-timing rules [18].
- W18 ship: C07a SDK pre-release probe snapshot — pre-2026-06-15 baseline artifact at `.ea/artifacts/research/long-term/2026-05-19-sdk-prerelease-probe.md` for v0.4 diff comparison [19].
- W19 ship: P25-I01 polish — flipped stale `.claude/worktrees` test reference to `.ea/worktrees/` + scrubbed W04 backfill stub citations + scoped `spec-paths` pre-commit hook to `## Tests` section only [20].
- Local pre-commit gauntlet (ruff + ruff-format + trim-whitespace + eof-fixer + yaml + toml + large-files + merge-conflict + debug-statements + detect-secrets + commit-prefix-lint + insert-coauthor + spec-paths) ✅ pass [21].
- mypy (`uv run mypy src/`) ✅ no issues found in 347 source files [21].
- Full test suite ✅ green via `uv run pytest -x -q` [21].
- Artifact validator (`uv run eawf artifact validate`) ✅ pass on the one promoted artifact (W18 SDK probe snapshot) [21].
- PR #22 review pass: cavecrew-reviewer verdict **approve** (zero findings); rules 1-27 compliance verified; secrets/PII clean; chassis intact on new artifact [22].

## Followups

None blocking. Carry-forwards to v0.4:

- **F1** Three writer-rows from the authority map remain on legacy direct-write internal subsystems (state CLI / layered config / registry writers); per Decision D-SUP-01 the v0.4 hygiene wave migrates these into daemon internals.
- **F2** `runtime.kind` config still emits the `deprecated_runtime_kind` warning on every `eawf config get`; flipping `schema_version` to 1.0 + emitting `runtime.preference: [<id>]` silences it. Mechanical migration scheduled for early v0.4.

## References

[1] `.ea/state.json` — `state.waves["P25-I01-W##"]` outcome strings + closed_at + commit SHA chain (19 wave feat/fix/chore commits + 13 [P25-CORE] state-bookkeeping commits on `feature/eawf-v0.3-p25`)
[2] commit `d2e400d [P25-W01] feat: C03 PhaseSpec + IterSpec + WaveSpec schemas + common types` + `src/eawf/specs/`
[3] commit `812e85c [P25-W02] feat: C03 verify-implements audit-DSL kind + cadence`
[4] commit `be7988b [P25-W03] feat: C03 daemon spec writer + cache + DRAFT/READY/IMPLEMENTED/ARCHIVED`
[5] commit `6a8c43b [P25-W04] feat: C03 closed-wave success_criteria -> WaveSpec backfill writer + bulk-run`
[6] commit `e3e13ea [P25-W05] feat: C03 spec validators + heuristics + pre-commit hook` + `tools/pre_commit_spec_paths.py`
[7] commit `dfd7d26 [P25-W06] feat: C07b canonical Event model + 10-kind store layout`
[8] commit `6d82607 [P25-W07] feat: C07b commit-prefix lint hardening + worktree home + KISS-007`
[9] commit `a00b0d7 [P25-W08] feat: C07b multi-repo registry + scope dispatch ladder`
[10] commit `c6dde9c [P25-W09] feat: C07b render envelope + Eä brand glyph + Nerd-Font + ASCII fallback`
[11] commit `6ca324d [P25-W10] feat: C07a RuntimeAdapter Protocol + 3 adapters`
[12] commit `58430b8 [P25-W11] feat: C07a PluginManifest + plugin sync + KISS-004 helpers`
[13] commit `d58ad64 [P25-W12] feat: C07a plugin doctor + 4 drift kinds + KISS-001 coauthor env-detection fix`
[14] commit `ebc123b [P25-W13] feat: C07a capability matrix + drift detector`
[15] commit `10c258e [P25-W14] feat: C08 layered config taxonomy + ~170-key registry + 1.0 migration`
[16] commit `8725854 [P25-W15] feat: C08 ProfileBody v2 + composition loader`
[17] commit `27e0f94 [P25-W16] feat: C08 3 bootstrap templates + multi-profile init`
[18] commit `b6a60d0 [P25-W17] chore: AGENTS.md rules 26/27 + /flow re-order + skill close-timing`
[19] commit `541d4f3 [P25-W18] test: C07a SDK pre-release probe snapshot (pre-2026-06-15 baseline)` + `.ea/artifacts/research/long-term/2026-05-19-sdk-prerelease-probe.md`
[20] commit `c43766e [P25-W19] fix: polish — flip stale .claude/worktrees test + scope spec-paths hook to ## Tests section`
[21] local `uv run pre-commit run --all-files` + `uv run mypy src/` + `uv run ruff check .` + `uv run pytest -x -q` + `uv run eawf artifact validate` invocations during the ship-gate audit
[22] PR #22 — cavecrew-reviewer agent_end report; aggregate verdict approve; zero findings across 271 files / +28,969 / -475

## Provenance

- audit_id: A31-P25-ship-gate
- audit_kind: ship-gate
- scope_id: urn:eawf:v1:repo:eawf
- verdict: pass (full deliverable scope landed; 0 review findings; CI gauntlet green; 2 mechanical carry-forwards to v0.4 are non-blocking)
- created_at: 2026-05-20
- author: claude-opus-4-7 (session eawf-flow-p25; reviewer subagent ac4910169ea2b2137)
- supersedes: none (P25 is a new phase)

## Scrub

- status: clean
- references: repo-relative paths + commit SHAs + PR URL only
- local paths: none in body
- real emails: none
- abstract placeholder names: not applicable
