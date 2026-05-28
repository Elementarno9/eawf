# P28-I03 deep audit (6-cluster fresh-context pass) — 2026-05-28

## Summary

Fresh-context audit of phase P28 (verify spine + lifecycle + clarity + plugins + trust + eval, 96 closed waves across I01/I02/I03). Six parallel auditor subagents covered disjoint criterion clusters; each re-read source against per-wave success criteria, ran targeted pytest, mypy, pre-commit. Aggregate verdict **minor** [1] (pass-with-followups with one criterion-text drift remediated via planned wave P28-I03-W52).

| Cluster | Verdict | Pass / total | Severity findings |
|---|---|---:|---|
| Verify spine (W01-W11, W17, W19, W20) | pass-with-followups | 13/13 | 1 nit (B074 runner-level argv revalidate) |
| Lifecycle + commits (W01, W02, W17, W21, W24, W31, W33, W34, W37, W43, W44) | fail | 10/11 | 1 major: W24 IntentBrief field-set drift |
| Clarity + docs (W04, W11, W16, W22-W25, W36, W47) | pass-with-followups | 14/16 | 5 stale `v0.3` doc refs; 4 hard-wrapped docs (own lint) |
| Plugins + harnesses (W07-W10, W32, W45) | pass | 6/6 | clean |
| Trust + eval + observability (W11-W15, W27-W30, W42) | pass-with-followups | 10/10 | 1 minor (ActualSummary EU conflation at `daemon/methods/state.py:498-503`); no trust scorecard CLI |
| Polish-remediation tail (W38-W51) + uncommitted | pass-with-followups | 11/11 | 1 minor: legacy stub `P28-I03-audit` envelope shadowed by this row |

Targeted suites: 323 + 26 + 7 + 457 + 25 + 7130 = green across all clusters; `uv run mypy src/` = clean; `uv run pre-commit run --all-files` = clean.

## Cluster findings

### Verify spine [2]

All 13 criteria pass. `EvidenceRef.kind` accepts `decision` (`src/eawf/kernel/spec/common.py:107`); CriterionSpec + GateSpec extend `_StrictModel` (`common.py:171-268`); `StoreKind.EVIDENCE` daemon-owned at `src/eawf/runtime/daemon/methods/evidence.py:62-108`, CLI gated on `EAWF_EVIDENCE_DIRECT_WRITE=1` + `{ci,recovery}` mode at `src/eawf/surfaces/cli/commands/evidence.py:88-202`; `validate_gate_argv` rejects shell-deny + path-qualified + git-allowlist at `src/eawf/runtime/sandbox/argv_policy.py:63-78,134-148,178-222`; ship gauntlet wrapped at `src/eawf/workflow/skills/ship.py:419-429`; CloseReadiness 3-seam wave/iter/phase (`src/eawf/workflow/verify/readiness.py:722-862`); gate runner timeout→blocked (`src/eawf/workflow/audit_dsl/registry.py:358-370`); waiver modes A/B/C (`src/eawf/workflow/lifecycle/waivers.py:20-181`); profile-flagged enforce default False (`src/eawf/platform/profiles/models.py:172`); citation_resolves check kind (`src/eawf/workflow/audit_dsl/models.py:208-225`).

- Nit: `_check_command_exit_zero` (`src/eawf/workflow/audit_dsl/registry.py:312-380`) does not route argv through `validate_gate_argv`; relies on callers — defense-in-depth follow-up under existing B074 tracking.

### Lifecycle + commits [3]

10/11 pass. `tools/commit_prefix_lint.py:117-374` accepts all five subject forms with phase-active gating; release `(release=vX.Y.Z)` annotation at `:133`; `.github/workflows/phase-release.yaml:34-105` parses + tags + publishes; `RoadmapPlan` strict-mode at `src/eawf/kernel/spec/roadmap_plan.py:95-166`; `lifecycle_phase.py:55-208` wires cadence-driven release preflight; `narrative.py:120-173` emits What/Why/Validation/Risks; TUI wave-detail consumes NarrativeBundle at `src/eawf/surfaces/tui/screens/overlays/detail.py:364-387`.

- Major: W24 IntentBrief drift — code ships `{goal, motivation, success_signal, evidence_refs, source_brief_ids}` (`src/eawf/kernel/spec/intent.py:70-74`) vs roadmap-brief criterion `{problem, desired_outcome, planned_steps<=10, risks<=10, priority_rationale}`. v0.4.1+ forward-scope brief has zero IntentBrief references. Operator chose migration; remediation wave `P28-I03-W52` planned PENDING.
- Minor: `AGENTS.md:739` documents cadence as `per_phase` (underscore); config schema accepts `per-phase` (hyphen). Doc drift — operators copying YAML from AGENTS.md will fail strict validation. Fix during /polish.

### Clarity + docs [4]

14/16 pass. Lints `eawf012` design-provenance, `eawf013` bracket-position, `eawf014` no-manual-wrap, `eawf015` EARS advisory all present under `src/eawf/platform/lint/`; ast-grep visible floor wired to reviewdog at `.github/workflows/reviewdog.yml:36-46`; `pyproject.toml:23-28` `[project.urls]` has 5 URLs; `CONTRIBUTING.md` exists with 70 lines; eval rule-adherence with 6 RuleIds + 7-case corpus.

- Minor: 5 stale `v0.3` references survived W47 doc refresh at `docs/architecture/installation.md:265,268`, `docs/architecture/profiles.md:15`, `docs/policy/fixed-decisions.md:97`, `docs/help/exit-codes.md:18`, `docs/help/migration.md:23`.
- Minor: EAWF014 self-check flags `docs/tutorial/quickstart.md` (13), `docs/tutorial/troubleshooting.md` (26), `docs/reference/doctor.md` (26), `docs/concepts.md` (31) as hard-wrapped against AGENTS `markdown-no-manual-wrap` rule.

### Plugins + harnesses [5]

6/6 pass. Codex TOML emit at `src/eawf/runtime/runtimes/codex/skills.py:61`; OpenCode per-agent ACL + `permission.task` at `src/eawf/runtime/runtimes/opencode/plugin_install.py:336-396`; Claude `.mcp.json` scaffold at `src/eawf/runtime/runtimes/claude/plugin_install.py:148`; richer hooks at `hook_map.py:87`; uniform `eawf-mcp` at `src/eawf/runtime/mcp/installer.py:43`; `eawf init --quick` at `src/eawf/surfaces/cli/commands/init.py:320`; 8 profile packs (apps, docs, infra, ml, python, quality, research, robotics); TUI init wizard modal + `/init` palette verb wired. Rule 4 mutator-authority N/A — plugins write only to `.codex/` `.claude/` `.opencode/`.

### Trust + eval + observability [6]

10/10 pass with two follow-ups. `compute_trust_scorecard` at `src/eawf/workflow/estimation/trust_scorecard.py:426`; tier Literal `verified/attested/deferred_outcome/unavailable` at `:28`; `eawf why` CLI at `src/eawf/surfaces/cli/commands/why.py:23-43`; phase EU rollup work-sum + critical-path + queue + realistic at `src/eawf/workflow/estimation/metrics.py:330-384`; OTel single stable span name + curated attribute subset at `src/eawf/observability/telemetry/otel.py:47-309`.

- Minor: W30 conflation — `daemon/methods/state.py:498-503` maps `wave_session_rollup.attention_eu` to both `actual_attention_eu` and `actual_agent_runtime_eu`; runtime EU column structurally absent from `WaveSessionRollup` (`telemetry/join.py:33-46`). Fields cannot diverge until rollup grows a runtime-EU column.
- Info: no `eawf trust scorecard` CLI surface; compute is library-only; only `eawf why` consumes it.

### Polish-remediation tail + uncommitted [7]

11/11 pass. Lifecycle-blocker rejection at `src/eawf/workflow/lifecycle/phase.py:211-249`; sandbox argv hardened (W41); daemon-authority writes routed (W40); TUI variance bucket drilldown landed (W46); doc freshness pass (W47); pytest 7130 / 27 skipped (env-only) / 1 xfail; mypy 537 files clean; pre-commit clean.

- Uncommitted diff vetted: `.ea/config.yaml` shape matches `kernel/config/registry/leaf_catalog.py:1147-1182`; stale-wave prefix test added; `.degraded-banner--hidden` referenced from `surfaces/tui/app.py:84,618`.
- Info: legacy `P28-I03-audit` envelope content `"Phase 2 stub"` is rejected by `phase_close` gate at `phase.py:222`; this row (A42) shadows it with non-stub check results.

## Operator decisions

| Item | Decision | Mechanism |
|---|---|---|
| W24 IntentBrief drift | migrate code to criterion | wave `P28-I03-W52` planned PENDING under P28-I03 [8] |
| Stub audit envelope | replace with real envelope | this artifact + audit row A42 |
| /polish timing | run now | task #4 |

## Remediation footprint

- W52 scope: `src/eawf/kernel/spec/intent.py`, `src/eawf/kernel/state/models.py`, `src/eawf/surfaces/render/narrative.py`, `tests/kernel/spec/test_intent.py`, `tests/surfaces/render/test_narrative.py`.
- Migration is safe: every `Wave/Iter/Phase/BacklogItem.intent` field is `None` on current state.json (no live IntentBrief instances to migrate). Estimated effort bucket M.

## References

- [1] eawf audit envelope A42-P28-i03-deep — scope P28-I03, kind evaluation, verdict minor.
- [2] Cluster auditor 1 — verify spine.
- [3] Cluster auditor 2 — lifecycle + commits.
- [4] Cluster auditor 3 — clarity + docs.
- [5] Cluster auditor 4 — plugins + harnesses.
- [6] Cluster auditor 5 — trust + eval + observability.
- [7] Cluster auditor 6 — polish-remediation tail.
- [8] `state.json` Wave entry `P28-I03-W52` status PENDING.

## Provenance

- session: deep-audit pass 2026-05-28
- triggered_by: operator `/audit full review P28 (additional try)`
- scope_id: `P28-I03`
- predecessors: A41-P28-i01-verify-spine, stub `P28-I03-audit`
- audit_envelope: `A42-P28-i03-deep`

## Scrub

- status: clean
- No PII / credentials / absolute external paths in this artifact. All file references repo-relative.
