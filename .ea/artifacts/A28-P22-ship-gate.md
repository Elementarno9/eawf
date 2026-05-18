# A28-P22 ship-gate audit

## Summary

- P22 stage-0 closure phase shipped 7 waves W01..W07 inline (no worktree dispatch); each wave's success criteria cleared against the recorded outcome string [1].
- P22 replaces the archived P21 (16-wave profile-bodies plan, predated the v0.3-v0.5 spec series ratified 2026-05-18); branch `feature/eawf-v0.3-p22` (renamed from `-p21` after the archive forced the ID bump).
- W01 ship: AGENTS.md amended — rule 4 rewritten (state-CLI superseded by daemon canonical mutator per D-SUP-01), rules 23 (DRY/KISS/YAGNI), 24 (fail-fast/SRP/least-surprise/SoC/pure/explicit), 25 (no design-decision references in source) added; rule 17 mutator-path block rewritten to reference the daemon + 2026-05-18 authority map; spike-workflow + anti-patterns appended; golden fixtures regenerated; managed-region versions bumped (`non-negotiable-rules` 1.4 → 1.5, `naming-conventions` 1.1 → 1.2, `spike-workflow` 1.0 → 1.1, `anti-patterns` 1.0 → 1.1) [2].
- W02 ship: `.gitignore` extended — `.ea/worktrees/` (per operator Q13 2026-05-18) and `.ea/state.json.bak.*` (per C10 §5.5; forward-fix is canonical recovery path) [3].
- W03 ship: `eawf decision add --supersedes <id>` flag wired; `add_decision()` atomically flips parent ACTIVE → SUPERSEDED + sets `superseded_by`; rejection paths cover unknown parent, self-supersede, and already-SUPERSEDED parent; 4 unit tests in `test_decision_supersede.py`; `DecisionPayload` schema extended with `supersedes: str | None` (forced by `extra="forbid"`; surfaced as test_store_paths_consistency failure during ship-gate sweep, fixed inline under W07) [4].
- W04 ship: 6 D-SUP decision rows landed at scope `urn:eawf:v1:repo:eawf` — D-SUP-01 (V1 daemon-Day-1 reverses roadmap-synthesis daemon-deferred), D-SUP-02 (single dispatcher), D-SUP-03 (V8 session-reuse on retry), D-SUP-04 (BYOK supported), D-SUP-05 (reactive runtime fallback supersedes halt-only pattern), D-SUP-TUI-01 (Textual supersedes rich) [5].
- W05 ship: 3 new auto-memory entries under `/Users/user/.claude/projects/...` (out-of-repo) — `feedback_no_design_comments_in_source.md`, `feedback_eu_calibration.md`, `project_v03_v05_spec_series.md`; `MEMORY.md` index extended with 3 new lines [6].
- W06 ship: `promote_draft` patched for subdirectory slugs (`long-term/<filename>` form) — `_SLUG_RE` accepts single-level subdir, `_artifact_path` routes to `.ea/artifacts/{kind}/{slug}.md`, `_artifact_id` sanitises `/` → `-`, URI computed from rel path; new `--legacy-chassis` flag skips chassis-heading + dense-citation checks for pre-chassis long-form briefs (still enforces sentinel + scrub status + PII scan); 22 spec-series briefs promoted under `.ea/artifacts/research/long-term/` (13 cluster briefs C00..C11 incl. C07a/C07b split + 4 C04 sub-clusters + authority-map + c12-rollup + migration-dag + future-ideas + combined audit doc); 22 `ART-research-long-term-*` rows registered in `state.artifacts` [7].
- W07 ship: this audit row + the pre-merge fixes surfaced by the test sweep — `DecisionPayload.supersedes` field add + fresh-repo golden regen for AGENTS.md drift [8].
- `uv run pre-commit run --all-files` ✅ pass (ruff, ruff-format, trim-whitespace, eof-fixer, yaml, toml, large-files, merge-conflict, debug-statements, detect-secrets) [9].
- `uv run pytest tests/ -q` ✅ 3214 passed, 12 deselected in 235s [9].

## References

[1] `.ea/state.json` — `state.waves["P22-I01-W01"..]` outcome strings + `closed_at` + `commit` SHA chain
[2] commit `cf16d84 [P22-W01] chore: AGENTS.md amendments` + commit `ac84c6d` follow-up + `src/eawf/profiles/data/core.yaml` + `tests/golden/agents_md/`
[3] commit `7133c0f [P22-W02] chore: .gitignore` + `.gitignore`
[4] commit `99f1dfa [P22-W03] feat: extend eawf decision add --supersedes` + `src/eawf/evidence/decision.py` + `src/eawf/cli/commands/evidence.py` + `tests/unit/test_decision_supersede.py`
[5] commit `c13e56b [P22-W04] feat: land 6 D-SUP rows` + `.ea/state.json` `state.decisions["D-SUP-*"]` rows
[6] external auto-memory directory (Claude Code per-project memory store; repo-relative paths intentionally omitted per AGENTS rule 16) — 3 new feedback/project files + updated `MEMORY.md`
[7] commit `ac84c6d [P22-W06] feat: promote 22 v0.3-v0.5 spec series briefs` + `src/eawf/cli/commands/draft.py` + `.ea/artifacts/research/long-term/` (22 files)
[8] commit `ff60dc6 [P22-W07] fix: DecisionPayload accepts supersedes; regen fresh_repo agents.golden` + `src/eawf/store/kinds/decision.py` + `tests/golden/scenarios/fresh_repo/agents.golden.json`
[9] local `uv run pre-commit run --all-files` + `uv run pytest tests/ -q` invocations during W07 ship-gate sweep

## Provenance

- audit_id: A28-P22-ship-gate
- audit_kind: ship-gate
- scope_id: urn:eawf:v1:repo:eawf
- verdict: pass
- created_at: 2026-05-18
- author: claude-opus-4-7 (session p22-w07-inline)
- supersedes: none (P22 is a new phase, not a re-run)

## Scrub

- status: clean
- references: repo-relative paths + commit SHAs only
- local paths: none in body
- real emails: none
- abstract placeholder names: not applicable
