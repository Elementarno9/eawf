# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
