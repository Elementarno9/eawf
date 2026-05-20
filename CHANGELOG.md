# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### BREAKING
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
