# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
