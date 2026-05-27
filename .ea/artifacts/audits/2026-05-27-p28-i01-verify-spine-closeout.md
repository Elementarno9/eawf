# P28-I01 verify spine + foundations closeout audit

## Summary

- Fresh-context audit of iter P28-I01 across its 15 closed waves (41 success criteria). Overall verdict **pass**: every criterion verified against source / diff / fixture per the AGENTS.md verify-before-claim ladder, the polish sweep returned zero changes + zero deferred + zero hard findings, and the gate trio (pre-commit + pytest + mypy) is green [1].
- **Waves W01-W04 (spec vocab + EvidenceRecord store)**: `EvidenceRef.kind` accepts `decision` and the two evidence vocabularies reconcile via `EvidenceKind` imported from `kernel/spec/common.py`; `CriterionSpec` and `GateSpec` extend `_StrictModel` with the `evidence_kind` enum while `Wave.success_criteria` stays `list[str]`; `StoreKind.EVIDENCE` writes through the daemon JSON-RPC `evidence.append` method with direct-JSONL writes gated on `EAWF_EVIDENCE_DIRECT_WRITE=1` (CI / recovery only) and no `MutationKind` added (replay-safe) [2].
- **Waves W05-W09 (verify spine + gate runner + readiness)**: `validate_gate_argv` rejects shell-deny heads with a read-only git sub-allowlist and wraps `_run_gate_command`; B-mint B074 replaces B044; `CloseReadiness` is pure read-only with advisory warnings (no blocks) wired at three close seams (`lifecycle_wave.py`, `daemon/methods/state.py`, `runtime/worktree/wave_land.py`); `compile_gate` emits `CheckSpec` with jury/attested flavors returning `None` for v0.4.0; the spec-promote READY validator rejects bad argv via the `_argv_passes_l0_policy` model_validator [3].
- **Waves W10-W11 (profiles + waivers)**: `ProfileBody.verify` schema lands on `platform/profiles/models.py`; three profiles (`python`, `apps`, `robotics`) compile distinct floor-check packs sharing the same `CloseReadiness` shape; `--waive --reason --decision` is repeatable per gate, mode C requires a decision/audit ref while mode A accepts no ref [4].
- **Waves W12-W14 (dispatch + role contracts)**: `render_role_contract` feeds `SubagentSpec` so per-role prompts are no longer hardcoded executor; `ExecutorReportBody` generalizes via `store_kind_for_role` and the three spawn-seam adapters (claude / codex / opencode) accept the `role_contract` kwarg; `pr_body.py` kind mapping uses the unified vocab via `_EVIDENCE_KIND_TO_CITATION_KIND` with 4 test files migrated [5].
- **Waves W07 + W15 (gate runner timeouts + scope + diff-base)**: W07 closed-empty after a subagent crash with the deliverable rolled forward into W15 (recorded in commit `cee4267`); W15 lands `TimeoutExpired -> blocked GateResult`, scope resolution (`changed` / `touched` / `all`), timeout class -> seconds mapping, `EAWF_GATE_FILES` env publishing, and `diff_base` derivation from the wave SHA via `derive_diff_base()` [6].
- **Polish sweep**: zero changes applied across the ~100 file diff, zero deferred items, zero hard findings; naming canonicals (`scope_id` / `evidence_kind` / `effort_bucket` / `agent_role` / `output_dir`), log keys (`wave=` / `iter=` / `phase=`), f-strings, library log format, error phrasing, docstring `Raises:` blocks, AGENTS rule 25 (no design-decision provenance comments), AGENTS rule 16 (no PII / machine paths), and ASCII-only source comments all clean [7].
- **Gate trio**: `uv run pre-commit run --all-files` pass (14 hooks); `uv run pytest -x -q` 6734 passed / 27 platform-skipped / 12 deselected / 1 xfailed (pre-existing); `uv run mypy src/` 0 errors across 505 files [8].

## Followup triage

Five non-blocking informational items lifted into P28-I02 / P28-I03 scope (no iter-close hold):

1. **W07 carry-forward bookkeeping** — empty-close pattern after subagent crash; deliverable lives in W15. State consistent; the empty-close bypasses the readiness advisory log line, acceptable under W06's advisory-only stance.
2. **W08 jury/attested gate flavors** — return `None` documented as v0.4.1 deferral; readiness compute falls through to the evidence-row path correctly (covered by `test_legacy_path_unchanged_under_w08`).
3. **B044 -> B074 mint** — historical state.json / event.jsonl refs to B044 retained per W05 commit body; no doctrine reading collides with the new B074 hardening backlog.
4. **W10 robotics HIL sentinel fields** — `requires_gpu=true` + `runs_outside_jail=true` are dead carriers until v0.4.1 lands the jail subsystem; acceptable schema-first wiring.
5. **W14 unmigrated vocab targets** — coverage hits `repo`->`artifact` + `commit`->`artifact` only; `urn` / `url` / `audit` / `decision` / `store_record` / `external_url` paths in `_EVIDENCE_KIND_TO_CITATION_KIND` remain untested (commit body acknowledges; defer to P28-I02).

## References

[1] `.ea/state.json` `state.iters["P28-I01"]` (15 wave_ids all status=closed)
[2] `src/eawf/kernel/spec/common.py:107-113` (EvidenceKind), `:143-147,171,191,197` (CriterionSpec / GateSpec / evidence_kind); `src/eawf/kernel/state/enums.py:354` (StoreKind.EVIDENCE); `src/eawf/runtime/daemon/methods/evidence.py:8-13,61`; `src/eawf/surfaces/cli/commands/evidence.py` (CI / recovery gate)
[3] `src/eawf/runtime/sandbox/argv_policy.py:14,114-115,167-170,242`; `src/eawf/workflow/skills/ship.py:42,411`; `src/eawf/workflow/audit_dsl/{models.py:35,registry.py:14,261}` (B-mint); `src/eawf/workflow/verify/readiness.py`; `src/eawf/workflow/verify/compile.py:24,71`; `src/eawf/kernel/spec/promotion.py` + `common.py:229-268`
[4] `src/eawf/platform/profiles/models.py:159,297,334`; `src/eawf/platform/profiles/data/{python,apps,robotics}.yaml`; `src/eawf/workflow/lifecycle/waivers.py:176-184`
[5] `src/eawf/workflow/dispatch/renderer.py:200,211`; `tests/unit/test_role_contract_projection.py`; `src/eawf/runtime/daemon/dispatch_runner.py:482,664`; `src/eawf/runtime/runtimes/{claude,codex,opencode}/adapter.py`; `src/eawf/surfaces/render/pr_body.py:248-254`
[6] commit `cee4267 [P28-I01] state: close W11 + W15 via wave land-batch` (W07 empty-close note); `src/eawf/workflow/audit_dsl/registry.py:82,95,208-223,246,253,272-284`; `src/eawf/workflow/lifecycle/wave_sha.py:130`
[7] polisher subagent report (session aa737e342dedd6d64)
[8] auditor subagent report (session a1fac089235730c37): `uv run pre-commit run --all-files` exit 0; `uv run pytest -x -q` 6734 / 27 / 12 / 1; `uv run mypy src/` 0 errors / 505 files

## Provenance

- audit_id: A41-P28-i01-verify-spine
- audit_kind: evaluation
- scope_id: P28-I01
- verdict: pass (41/41 success criteria verified with source + diff + fixture evidence; polish sweep clean; gate trio green)
- created_at: 2026-05-27
- author: claude-opus-4-7 (session flow-p28-i01-close) + fresh-context auditor subagent + polisher subagent
- supersedes: none

## Scrub

- status: clean
- references: repo-relative paths + commit SHAs + wave / iter / audit ids only
- local paths: none in body
- real emails: none
- abstract placeholder names: not applicable
