# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog [1], and this project adheres to Semantic Versioning [2].

## [0.6.0]

### Added
- **The binding pass: every built-but-idle contract is now turned on, captured, or surfaced.** P30's theme was "never ship an idle contract again", and the phase delivers the three corresponding strands. The fleet drive-loop wires the autonomous `/flow` driver so a phase's iters dispatch their wave DAG hands-off through the close gate. The `Track` substrate lands the durable strategy/feature vehicle (the renamed former `Subproject` entity) as a real state record that phases tag via `current.track_id`. Trust-validation un-idles the deterministic close-gate evidence path so each scored criterion mints a real `EvidenceRecord` rather than an empty stub. And the TUI cockpit reskins and binds the operator surfaces — fleet drive view, Track standings, evidence/trust drills, and the cross-repo workspace grid — to the live state the rest of the phase now captures.
- **Autopilot, the research campaign, the init experience, and Windows are now live.** The fleet drive-loop runs off the daemon event loop with bounded spawn/repair ladders, per-round frontier recompute, watcher liveness, pause/halt/resume, and cockpit auto-reattach. The research campaign control plane drives real rounds — round runner, claim/OpenQuestion writers, `research.run`/`steer`/`broadcast`/`override` RPCs, Round records, the EviBound synthesis gate, and the `eawf research question`/`status` verbs. A stepped, live-executing init wizard (four journeys: first-run, repo init, workspace bootstrap, success) replaces the command-plan chooser. The cosmic-terminal reskin ships a hand-tuned ASCII-art brand seal rendered as theme-portable text across every honest-empty hero. And Windows gains a named-pipe daemon transport (large-frame reassembly, CancelIoEx teardown, owner-only pipe DACL) so `pip install eawf[windows]` runs the daemon natively, verified by a `windows-latest` CI job.

### Fixed
- **Validation-repair pass (I21): the autopilot spawn, the research campaign, and the cockpit are corrected against live v0.6.0 validation.** The sandbox jail grants every lane (claude/codex/opencode) a write carve-out for its own state dir, so a jailed agent's Bash tool no longer dies with EPERM at init; the executor report binder unwraps the claude stream-json envelope (a shared helper) so a valid report binds first try instead of being rejected and synthesized, and the Watch tail renders that envelope readably. The research campaign is decoupled from an execution wave — it runs with no active wave, books its researcher spend to the campaign (never inflating a closed wave), streams researcher output + Feed markers, converges to a terminal `CONVERGED` state, closes its researcher sessions, resolves answered `OpenQuestion`s and lets a researcher raise a blocking clarification, and compacts near-duplicate claims. Systemic-perf + UI fixes: one durable event subscription per scope with a resume cursor (no re-subscribe storm), a parked closed-wave redispatch (no stale-run churn), timestamped console logs, disambiguated spawn/bind attempt counters, a de-duplicated research-board footer, a `precision` (was "variance") metric label, a right-aligned header runtime/clock, a running-round progress band, labelled domain tree leaves, and a keyboard drill-in to a claim's full text + evidence.
- **Headless-dispatch lifecycle consistency (I25): the autopilot lifecycle now behaves identically for codex and headless-claude, pinned by a live cross-runtime e2e gate.** Four live-only bugs — green in the in-process unit suite but broken under a real spawn — are fixed. A dispatch that completes after close-on-behalf no longer persists a phantom `SessionAttempt` (the persist re-reads the terminal `wave.status` under the state lock and drops it, so no attempt/cost/tokens accrue onto a closed wave); a wave wedged before its attempt registered — claimed with no in-flight lane and a dead process group — is reaped to FAILED at the drained-run seam instead of hanging forever; the grounded-repair ladder classifies an environmental floor-gate failure (a bare smoke repo missing `.pre-commit-config.yaml` / a package dir / pytest) as unfixable-by-the-executor and closes-with-followups rather than burning re-dispatch attempts on it; and the interactive-claude Stop-hook capture mints a per-attempt cost row so an interactive wave surfaces per-attempt cost the same way a headless wave does. The new `tests/e2e/test_lifecycle_consistency.py` drives a real codex frontier and a real claude frontier to green — both lanes close on-behalf with one priced attempt each, no post-close dispatch, the wedged orphan reaped, the run drained — and its four invariant assertions run with teeth in CI without a paid spawn.
- **Clean-repo smoke-run gaps (I21): the v0.6.0 acceptance runbook surfaced ten spawn/campaign/observability gaps, now closed.** Two capture/robustness bugs: a headless spawn's parsed per-class token usage now lands on `wave.tokens_consumed` (its own `SpawnResult` is authoritative, so it replaces a foreign claim-time sidecar baseline rather than differencing against it, and the `runtime.capture` writer merges token fields instead of null-clobbering them) so the runtime tab reads real data instead of "no data"; and the idle watchdog now counts a live background drive or research-run thread as in-flight (plus a `shutdown_drive` teardown) so a subscriber-less headless drive no longer self-kills mid-spawn at the default timeout. Four observability strands: claude spawns default to `--output-format stream-json` (routed through a shared terminal-result-envelope unwrap) so output streams during the run instead of a multi-minute blackout; the cost tab renders a cache-adjusted effective-token `eff` headline plus the per-class in/out/cache breakdown (reusing the metering price weights); and the Watch pane gains an in-flight liveness heartbeat (`thinking · elapsed/~expected · turns · pid`, effort-aware from the wave's bucket) with a taller stream pane and fuller backfill. Three campaign-hygiene fixes: `research campaign new --dry-run` reports the domain count without persisting a campaign (so a sanity resolve-check never leaks one), a `research campaign cancel` verb tombstones an active campaign over the existing daemon method, and the topic tree renders scope-wide questions in their own `scope questions` section rather than under an arbitrary first campaign.

### Migration
- **`state.json` `schema_version` advanced 1.13 -> 1.14.** Additive: it registers the `jury_ballot` store kind — the per-juror ballot store the jury-calibration substrate reads (the convener now persists one ballot row per convened juror). No persisted `State` field references the new kind, so the `v1_13_to_v1_14` migration is a marker-only version bump with no row rewrite. Run `eawf migrate` to advance an existing repo; the step is idempotent and replay-safe.
- **`state.json` `schema_version` advanced 1.12 -> 1.13.** Additive: it introduces the optional `Wave.criteria_floor_waiver` field (the typed, visible bypass of the new plan-time typed-criteria floor), defaulting to `None` on the model. Because a missing optional key is not an unknown key, a pre-existing state re-validates unchanged; the `v1_12_to_v1_13` migration is a marker-only version bump with no row rewrite. Run `eawf migrate` to advance an existing repo; the step is idempotent and replay-safe.
- **`state.json` `schema_version` advanced 1.8 -> 1.9.** Additive: it introduces the optional `Wave.runtime_baseline` field (the claim-time snapshot of cumulative runtime counters), defaulting to `None` on the model, and the `v1_8_to_v1_9` migration materialises the explicit nullable key on every wave row. Run `eawf migrate` to advance an existing repo; the step is idempotent and replay-safe, and no field is removed or renamed.
- **`state.json` `schema_version` advanced 1.9 -> 1.10 (the `Subproject` -> `Track` rename).** This edge is a rename, not an additive field: the `Subproject` entity becomes `Track`, the top-level `subprojects` key becomes `tracks`, the cursor field `current.subproject_id` becomes `current.track_id`, and each phase's `subproject_id` link becomes `Phase.track_id`. Because the live model forbids unknown keys (`extra="forbid"`), an un-migrated state carrying the old key names rejects on read; the `v1_9_to_v1_10` migration rewrites every name so a pre-existing state re-validates after migration. Run `eawf migrate` to advance an existing repo — the transform is auto-applied and no data is lost, only renamed.
- **`state.json` `schema_version` advanced 1.10 -> 1.11.** Additive: it adds the optional `harness` and `model` attribution fields to `ActualSummary` and `RuntimeBaseline` (inherited by `RuntimeLatest`) so captured EU actuals and runtime counters become calibratable by harness+model. Both default to `None`; the `v1_10_to_v1_11` migration materialises the explicit nullable keys on every `actuals` row and every wave's runtime snapshot. Run `eawf migrate`; the step is idempotent and replay-safe.
- **`state.json` `schema_version` advanced 1.11 -> 1.12.** Additive: it registers the `UserDecisionKind` enum and the `PauseResolution` typed record — the shared operator-decision shape spanning the `needs_user` pause surface and the fleet-fork surface. No existing persisted `State` field references them (resolutions live in the append-only event + evidence stores), so the `v1_11_to_v1_12` step is a marker-only version bump with no row rewrite. Run `eawf migrate` to advance an existing repo.

## [0.5.4]

### Fixed
- **The Codex plugin loader no longer rejects four `SKILL.md` files with `invalid YAML: mapping values are not allowed in this context`.** The `SKILL.md`, agent, and OpenCode-command frontmatter emitted the `description:` value unquoted, so a description containing a `: ` (colon-space) — the `design`, `prep`, `spike`, and `math-explainer` skills each carry one — parsed as a nested mapping under a strict YAML loader. Claude Code's looser frontmatter parser tolerated it; Codex's did not, so those skills failed to load. Every frontmatter `description:` emit site now routes through a shared `json.dumps`-backed double-quoted-scalar helper (`eawf.surfaces.render.frontmatter.yaml_scalar`), and the `SKILL.md` frontmatter test now parses the block as strict YAML rather than only checking key presence — the gap that let the bug ship.

## [0.5.3]

### Fixed
- **`uv tool install` / `/plugin install` no longer crashes with `ModuleNotFoundError: No module named 'hypothesis'`.** The `transition_coverage` audit-check kind imported `hypothesis` (a dev/test-only dependency) at module top level, and the audit-DSL registry binds every check-kind eagerly at CLI startup — so importing the installed CLI pulled in a dependency that a runtime-only install (`uv tool install`, which omits the dev dependency-group) does not ship, crashing `eawf` before it could run. The import is now lazy: the Hypothesis state-machine is built inside `_make_machine` only on the machine-driven coverage path, so the CLI starts cleanly without `hypothesis`, and the kind degrades to a failed check (rather than aborting the audit) if the machine path runs without it.

### Changed
- **Release pipeline now gates publish on green CI and a runtime-only install smoke.** Two prevention gaps let v0.5.2 ship broken: the publish workflows (PyPI + plugin) triggered on the version tag independently of CI, so a red test run never blocked a publish; and CI always installs the dev dependency-group, so its tests could not see a runtime module reaching for a dev-only dependency. Both are closed: a new `tool-install-smoke` CI job installs the built wheel with runtime deps only and exercises the CLI, and the PyPI + plugin publish jobs now require the CI workflow to have concluded `success` for the tagged commit before they run.

## [0.5.2]

### Changed
- **Published npm package renamed to the scoped `@elementarno/eawf`.** The npm registry's name-similarity guard rejects the unscoped `eawf` name (deemed too close to an existing package), so the Claude plugin now publishes under the account-scoped `@elementarno/eawf`. This changes only the underlying npm artifact and the marketplace pointer's `source.package`; the plugin and marketplace names stay `eawf`, so the install command remains `/plugin install eawf@eawf`. The direct npm form is `npm install @elementarno/eawf`.

## [0.5.1]

### Changed
- **Unified the plugin marketplace name to `eawf` across both runtimes.** The Claude Code and Codex catalogs now register under the same marketplace name `eawf`, so the install command is `/plugin install eawf@eawf` on either runtime; previously the Codex catalog registered as `eawf-codex`. The plugin name was already `eawf` everywhere — only the Codex marketplace identifier changed.

### Fixed
- **First successful npm publish of the Claude plugin.** The `v0.5.0` tag's `plugin-release` run failed at the npm publish step with `ENEEDAUTH` because the `NPM_TOKEN` repository secret had not been configured yet, and a follow-up `workflow_dispatch` run validates the rendered trees but never publishes by design. The token is now set, and two release-pipeline regressions are fixed alongside it: `actions/upload-artifact` was dropping the hidden `.claude-plugin/` tree from the packaged Claude plugin (so the npm tarball would have been incomplete), and the `phase-release` annotated-tag step had no git identity. `v0.5.1` is the first tag to publish the Claude plugin to npm.

## [0.5.0]

### Added
- **Self-hosted plugin marketplace.** Both runtimes now ship committed catalog pointers so operators can install Eä straight from the repo. `.claude-plugin/marketplace.json` declares the Claude Code marketplace (`eawf-local`) backed by the `eawf` npm package; `.agents/plugins/marketplace.json` declares the Codex marketplace (`eawf-local-codex`) backed by a `git-subdir` source at `./plugins/eawf` on the `plugins-dist` branch ref. A tag-triggered `.github/workflows/plugin-release.yaml` packages both runtimes, runs a blocking validate gate over the emitted trees, then publishes the Claude plugin to npm and pushes the Codex plugin to the `plugins-dist` branch.
- **`/design` and `/spike` registered as first-class plugin skills.** The two skills now render into the packaged plugin trees alongside the existing skill set instead of living only as local conveniences.
- **`eawf iter candidate-tag` command.** Reads (or, with a value, sets) the proposed `vMAJOR.MINOR.PATCH` release tag carried on the active iter via the new `Iter.candidate_tag` field.
- **Deterministic close-gate enforcement (the verify spine, first real use).** With the `agent_driven` + `quality` profiles enabled, every UI/UX-band wave closes through `run_oracle`, which scores each required criterion against its typed `GateSpec` at the cheapest deterministic tier first (`command_exit_zero` pytest / `affordance_parity` live key-probe / `svg_well_formed`), minting a `deterministic`/`pass` `EvidenceRecord` on close; a non-required (advisory) criterion never blocks the close. The `affordance_parity` gate is loop-safe under the daemon event loop (thread-offloaded Pilot mount).
- **Operator-surface (TUI) build-out.** The `tui` surface gained: an evidence-mode close-readiness ledger over typed criteria with a why-peek drill modal and scrub-gated export; trust-mode oracle-determinism, escape-ledger, verifier/calibration drills, and a calibration-readiness tile; a research board with new/cancel campaign bindings and populated unresolved/options/conflicts tabs; a config modal that locks non-writable layers read-only; block-eighths bars + a braille spinner + a determinate ProgressBar with a unicode->ascii render-mode flip; a first-run carousel tour and active-mode help highlight; a workspace portfolio-totals row, cross-repo attention chips, and PR-count column; and a configurable multi-line statusline with glyph/color modes, ctx + rate-window bars, a stale-while-revalidate cache, and a global-install wizard.

### Changed
- **`Wave.success_criteria` retyped from `list[str]` to `list[CriterionSpec]`.** Wave success criteria are now first-class typed rows (id, text, kind, acceptance style, evidence kind, quality dimension, measurable signal) rather than free-form strings, so the close-readiness loader can score real criteria instead of returning an empty stub. The operator-facing `--success` CLI input (and the daemon `add_wave` / `roadmap revise --add-wave` paths) still accept comma-separated strings; each is wrapped into a grandfathered `CriterionSpec` (`kind="legacy"`) at the boundary so the authoring surface is unchanged. The readiness `_load_criterion_specs` loader is no longer idle: it reads the typed field directly, and grandfathered legacy criteria render through the advisory legacy view path while authored typed criteria route through the gated spec path.
- **Plugin doctor gained a disk-to-registry orphan drift kind.** The `plugin doctor` walk now reports on-disk skill directories that have no corresponding `SkillSpec` row in the skill registry as an `orphan` drift kind, so an operator can prune stray artifacts per the AGENTS.md deletion rule (it flags only — registry growth stays explicit).
- **Plugin version derives from `eawf.__version__` across all three runtimes.** The Claude, Codex, and OpenCode adapters now stamp the packaged plugin version from the single `eawf.__version__` source instead of hard-coding it per runtime, so a version bump propagates everywhere automatically.

### Fixed
- **Four previously-idle EAWF lints wired as blocking pre-commit hooks.** `eawf002`, `eawf003`, `eawf010`, and `eawf011` were implemented but unwired; they now run as blocking pre-commit hooks.

### Migration
- **`state.json` `schema_version` advanced 1.3 -> 1.4.** The bump is additive: it introduces the optional `Iter.candidate_tag` field and is replay-safe via the `v1_3_to_v1_4` migration. Run `eawf migrate` to advance an existing repo; a state document written under 1.3 loads unchanged once migrated, and no field is removed or renamed.
- **`state.json` `schema_version` advanced 1.6 -> 1.7.** This edge is a real per-wave backfill, not a bare version bump: it retypes `Wave.success_criteria` from `list[str]` to `list[CriterionSpec]` and rewrites every legacy criterion string into a grandfathered `CriterionSpec` row (`id=CR-<n>`, `kind=legacy`, `acceptance_style=binary`, `evidence_kind=attested`, `quality_dimension=functional_suitability`, `measurable_signal` = the text truncated to 300 chars, or a fixed fallback for strings under the 20-char floor). An empty `success_criteria` list migrates to `[]`. Run `eawf migrate` to advance an existing repo; the `v1_6_to_v1_7` migration is replay-safe (an already-typed row passes through untouched) and an un-migrated 1.6 state with bare-string criteria would otherwise reject the typed field on load. The original criterion string is preserved verbatim in `CriterionSpec.text`, so no content is lost.
- **`state.json` `schema_version` advanced 1.7 -> 1.8.** Additive with a per-wave backfill: it introduces the typed `Wave.gates` list (`GateSpec` rows the deterministic close gate scores against each wave's success criteria) with a model default of `[]`, and the `v1_7_to_v1_8` migration walks every wave to materialise an explicit `gates: []` where the key is absent. Run `eawf migrate` to advance an existing repo; the step is idempotent and replay-safe (a wave that already carries a `gates` list passes through untouched), so an already-1.8 state is a lossless round-trip and no field is removed or renamed.

## [0.4.1] - 2026-05-29

### Fixed
- **`click` promoted to an explicit runtime dependency.** `eawf.surfaces.cli.commands.plan` imports `click` directly (it inspects `click.core.ParameterSource` to distinguish CLI-supplied flags from defaults). Pre-0.26 `typer` carried `click` as a hard transitive, so the direct import resolved on every install; `typer >= 0.26` migrated to `typer-slim` and made `click` optional, which broke fresh `uv tool install eawf==0.4.0` / `pip install eawf==0.4.0` with `ModuleNotFoundError: No module named 'click'` on the first CLI invocation. The runtime-deps table now lists `click >= 8.1` explicitly, and the `[tool.deptry.per_rule_ignores] DEP003` list drops the `click` entry that papered over the indirect import. No source changes — the fix is packaging-only.

## [0.4.0] - 2026-05-29

### BREAKING
- **`IntentBrief` field set ratified per W24 audit.** The brief schema is now exactly seven fields: required `problem` + `desired_outcome` (each <=200 chars), optional `planned_steps` / `risks` / `priority_rationale`, and the carry-over `evidence_refs` / `source_brief_ids`. The legacy `goal` / `motivation` / `success_signal` fields are removed; any state document still carrying them fails `extra="forbid"` at load time. CLI surface follows: `eawf roadmap revise` and `eawf backlog edit` drop the `--intent-goal` / `--intent-motivation` / `--intent-success-signal` flags. Migration: replace `--intent-goal <X>` with `--intent-problem <Y> --intent-desired-outcome <Z>`; replace `--intent-motivation <X>` with `--intent-priority-rationale <X>`; replace `--intent-success-signal <X>` with `--intent-desired-outcome <X>` (the two fields name the same target state at the schema level). Self-only consumer scope; the staged W59-W60-W61 migration kept the call graph green throughout.
- **Exit-code surface compressed 0..9 → 0..5 per C05 § 5.3.** The
  legacy nine-class CLI exit-code taxonomy is replaced by the five
  canonical buckets `OK (0)`, `USER_ERROR (1)`, `VALIDATION_ERROR (2)`,
  `STATE_CONFLICT (3)`, `DAEMON_UNREACHABLE (4)`, `INTERNAL_ERROR (5)`.
  The numeric values under the legacy names (`GENERIC_ERROR`,
  `NOT_FOUND`, `INVALID_INPUT`, `VALIDATION_FAILED`, `LOCK_CONFLICT`,
  `INSTRUMENT_MISSING`, `USER_DECLINED`, `INTEGRITY_VIOLATION`,
  `HOOK_BLOCKED`) have *changed*: each legacy name is now a
  deprecation alias mapped onto its new bucket per the § 5.3 bucket
  table. Scripts that pinned specific exit codes (e.g. `if rc == 4`
  meaning `VALIDATION_FAILED`) update to the new code (`2` for
  `VALIDATION_ERROR`) or to the alias name. Single-PR cutover —
  self-only consumer scope; no downstream announce window.
- **`ErrorEnvelope` JSON shape introduced per C05 § 5.4.** Every
  non-zero exit emits a typed envelope with `schema_version`,
  `error` (canonical bucket name), `message`, `exit_code`,
  `exit_name`, `suggested_next_step`, `data` (carries legacy
  subclass name as `data.kind` for CI-script pivots),
  `correlation_id` (set on daemon-mediated errors),
  `protocol_version` (set on `ProtocolMismatch`), and `timestamp`
  (UTC ISO-8601). Replaces the prior four-field shape
  (`error`/`message`/`exit_code`/`exit_name`).
- **Legacy `CliError` subclasses are now deprecation aliases.**
  `NotFound`/`InvalidInput`/`InstrumentMissing`/`UserDeclined` →
  `UserError`; `ValidationFailed` → `ValidationError`;
  `LockConflict`/`IntegrityViolation`/`HookBlocked` →
  `StateConflict`. Each remains importable as a subclass of its
  new bucket so existing `raise errors.NotFound(...)` callsites
  keep working; the legacy name surfaces through
  `ErrorEnvelope.data.kind`. Downstream waves retire each callsite
  to raise the new bucket class directly with `data={"kind": ...}`.

### Added
- Daemon JSON-RPC error code mapping (C02 `-3200X` codes) folded
  onto the five-class taxonomy per C05 § 5.3 table; surfaced via
  `eawf.cli.errors.cli_error_for_rpc(rpc_code, message)`.
- Per-`data.kind` hint refinement (`_KIND_HINTS`) preserves the
  legacy nine-class specificity inside the five buckets — operator
  hint text stays informative even after callsites switch to the
  new bucket classes.

## [0.3.0] - 2026-05-25

This entry backfills the v0.3.0 release, which shipped without a changelog section. v0.3.0 spans phases P14 through P27 (a phase, written `P<NN>`, is eawf's unit of shipped value — one reviewable delivery), so the notes below summarise that arc rather than enumerating every wave.

### Added
- **Multi-harness support: Codex and OpenCode join Claude Code (P14).** eawf can now dispatch agent work to three coding-agent runtimes, not just Claude Code. The OpenCode plugin ships as untyped `.js` to avoid a TypeScript build step; Goose / Aider / Cursor / Cline were deferred to v0.4 to keep the phase scoped.
- **Skills surface: `/research` flags, `/blitz`, and the registry seam (P15).** The research skill gained depth flags, the auto-chained follow-up skill `/blitz` (it recurses into `/research` when more than one unknown remains, guarded by a recursion-depth env var), and the skill registry that later phases render plugin trees from.
- **Artifact chassis and estimation (P16).** Durable research / plan / audit / decision / incident markdown gained the renderer-owned chassis sections (`Summary` / `References` / `Provenance` / `Scrub`) with dense `[N]` citation markers backed by typed `Citation` rows, plus the effort-unit (EU) estimation surface.
- **Typed agent reports (P18).** Every agent session that reaches a terminal handoff now emits a typed `agent_end` report body validated against `AgentReportBody`, with a closed verdict vocabulary (`pass` / `pass-with-followups` / `fail` / `blocked`) and role-specific store kinds.
- **Roadmap-driven planning + commit hygiene (P19).** The `eawf roadmap propose` / `revise` / `apply` flow lands phases one at a time as first-class `PLANNED -> ACTIVE -> CLOSED` state records, and `eawf wave claim` enforces dependency + sibling-ordering gates. Commit-prefix linting enforces the `[P<NN>-W<NN>]` taxonomy.
- **TUI, metrics, and operator UX (P20).** The terminal UI, the EU velocity / burn metrics, and the operator-facing status surfaces landed.
- **Profiles, MCP/sandbox, and domain bodies (P21).** Per-domain configuration profiles, the MCP server grant + sandbox-policy models, and the domain-specialist skill bodies were added.
- **The C-series kernel rebuild (P22–P27).** The bulk of v0.3 is a ground-up rebuild against an internalised design spec (the `C0N` cluster codes name the spec sections): the bootstrap + state + config + URN + id + validate kernel (P23, `C01`), the `eawfd` daemon keystone with JSON-RPC + smart-spawn + `portalock`-backed atomic writes (P24, `C02`), the spec-infrastructure + runtime-dispatch + VCS-convention contracts (P25, `C03`/`C07`/`C08`), the CLI + workflow/skills/agent/runtime surfaces + the operator TUI (P26, `C04`/`C05`/`C06`), and the observability + operations layer — the quality audit DSL, telemetry, and release/migration ops (P27, `C09`/`C10`).

### Changed
- **The daemon (`eawfd`) became the sole canonical mutator of `state.json`.** Read access stays free, but every mutation now proxies through the daemon over JSON-RPC, falling back to a direct `portalock` write only when the daemon is unavailable (CI / one-shot / recovery shell). This is the structural backbone the later trust + fleet phases build on.
- **PR cadence ratified as one-PR-per-phase with rebase-merge.** Decisions D07 (rebase-and-merge, never squash) and D10 (keep one-PR-per-phase) were ratified during this arc, so per-wave `[P<NN>-W<NN>]` history survives on the trunk and each phase reviews as one coherent story.

## [0.2.0] - 2026-05-11

### Added
- Phase 7: state-CLI completeness — `audit set-verdict` mutator (B028),
  `backlog set-priority` mutator + CLI (B026), v0.1 retro-stamp of audits
  A01..A07, D08 deferring PR-cadence policy revisit to v0.2 ship.
- Phase 8: orchestration core — subagent prompt renderer (B025), wave
  land cherry-pick CLI (B027), CI-fix loop wave plan + parser (B040),
  wave review CLI + findings parser (B041), budget-consume CLI.
- Phase 9: backlog cleanups — insort dedup refactor, `Wave.blocks`
  reverse-index invariant validator, cross-platform absoluteness check
  in worktree path-fix, surface discarded delta in budget-consume
  rollback error.
- Phase 10: multi-runtime — `eawf skill render` CLI, `state.mcp_grants`
  table + grant/revoke CLI, `claude-agent-sdk` dispatch adapter,
  dual-runtime envelope ship-gate demo.
- Phase 11: workflow ergonomics — `planning.auto_plan` +
  `flow.auto_accept` config defaults, SKILL.md bodies prompting
  plan-mode + AskUserQuestion gates, P11 skill registry tests.
- Phase 12: backlog cluster — decision graph render (B029), memory GC
  + tiered memory (B030+B037), doc-drift linter (B031), PR-body
  renderer + project wiki (B032+B038), skill eval harness with golden
  envelopes (B033), sandbox/permission policy table + CLI (B034),
  file-impact graph (B039).
- Phase 13: feature cluster + v0.2 ship-gate — end-to-end golden
  scenarios under `golden_scenarios` pytest marker (B009), self-eval
  semantic scoring with 0.85 threshold gate (B042), audit-check DSL
  skeleton with 5 check kinds + YAML runner (B019, D02 resolved),
  session-level plugin-mode hooks (`SessionStart`/`Stop`/`PreToolUse`
  /`PostToolUse` on Bash `git commit`/`git push`) wired into
  `eawf plugin package claude` (B015), user-scope install probe via
  `eawf doctor --user-scope` + `eawf plugin update claude --check`
  (B017).

### Changed
- Backlog hygiene: 13 shipped items (B025, B027, B029-B034, B037-B041)
  closed against their resolving commits and audit references.
- `docs/architecture/plugins.md` § hooks rewritten: deferred-to-v0.2
  paragraph replaced with the session-level event map; eawf-internal
  lifecycle events (wave/iter/phase/audit) stay fired by the state
  CLI through `eawf hook run`.
- `docs/architecture/profiles.md` + `docs/policy/fixed-decisions.md`:
  quant/ml stub note refreshed — the audit-check DSL skeleton landed
  in P13 W04; profile bodies (B006/B007) still pending for v0.3.
- `README.md`: dropped "(hooks deferred to v0.2 — see B015)"
  parenthetical from the local-marketplace install section.
- Decisions: D09 defers B005 PyPI publish to v0.3, D10 keeps the
  one-PR-per-phase cadence through v0.2 close, D11 locks P13 scope
  at the 7 declared waves.

### Fixed
- `PrBodyNotFound` N818 lint silenced after P12 close (P12-CORE).
- Pre-commit hook drift on `state.json` line-numbers absorbed by
  `.secrets.baseline` refresh in P10/P11 carry-over.
- Various per-phase PR-review must-fixes (P08/P09/P10).

### Known limitations (rolled forward to v0.3)
- `state.project.version_target` setter is not yet exposed by the
  state CLI; the field stays unset for v0.2. Tracked as B048.
- True user-scope plugin auto-update (background process) deferred;
  P13 W07 ships the probe (`doctor --user-scope` + `plugin update
  --check`) only.
- Quant + ML profile bodies (B006/B007) stay catalog-stubs in v0.2;
  the DSL is functional, but the profile bodies have not been
  authored. v0.3 work.

## [0.1.0] - 2026-05-10

### Added
- Phase 0: repo bootstrap — pyproject, uv-managed deps, `src/eawf` package
  skeleton, pytest + Hypothesis scaffolding, GitHub Actions CI matrix
  (macOS-26/15, Ubuntu-24.04/22.04), pre-commit hooks (ruff, detect-secrets,
  standard hygiene), and pre-built but gitignored `AGENTS.md` + `.claude/`
  agent-driven-dev assets.
- Phase 1: typed state model, ID grammar, URN parser, atomic JSON writer
  with sibling lockfile, JSONL envelope store with 9 kind payloads, JSON
  Schema export, `eawf validate --strict` with schema + invariant gates.
- Phase 2: estimation engine, scope discipline, evidence/audit guards,
  rollback timestamping, structured CLI error envelopes, portalocked
  serialisation across mutators, lifecycle event-first ordering.
- Phase 3: instrument probe + `eawf doctor` skeleton, profile composition
  (11 profile bodies), AGENTS.md + CLAUDE.md rendering with managed
  regions, init wizard (interactive + `--no-input`), workspace + repo +
  clone-repo commands, render-output envelope JSON ⇄ markdown,
  `eawf sync` + extended doctor checks.
- Phase 4: skill engine + typed envelope header/footer + per-skill body
  schemas, six core skill subclasses (research/prep/audit/ship/review/polish),
  four meta skills (init/roadmap/differentiate/flow), hook router + runner
  + Claude payload translator, `eawf plugin install/update/doctor claude`,
  `eawf cc statusline`, `eawf skill list/run` with in-process registry.
- Phase 5: worktree CLI (create/list/merge-back/cleanup), `flow resume`
  with drift detection, memory authoritative + sync views, MCP install/
  update/remove with `owner=eawf` gate, `plan show` CLI + plan_view
  renderer + JSON Schema export, questionary+rich wizard (replacing
  Textual), framework self-apply via `eawf init`, AGENTS.md framework
  rules ported to profile render_blocks.
- Phase 6: spec internalization into `docs/`, packaged CC plugin landing
  README, `eawf plugin package claude` emitting installable plugin tree,
  v0.2 roadmap (B019..B043, D02..D06) recorded in `state.json`.

### Fixed
- Path traversal, fsync, TTY block, header validator (P04 review).
- Subsystem hardening across worktree/flow/mcp/status/clone (P05).
- Wizard UX repair + checkpoint envelope hardening (P05).
- JSON-injection escape, except-parens, count drift in plugin packager
  (P06 W05 review).
- CC plugin manifest: drop rejected skills/agents directory keys (P06 W05).

## References

| Ref | Source |
| --- | --- |
| [1] | Keep a Changelog: https://keepachangelog.com/en/1.1.0/ |
| [2] | Semantic Versioning: https://semver.org/spec/v2.0.0.html |
