# Source map

A one-line-per-package index of `src/eawf/`, grouped by the architectural
layer each package belongs to. The layer grouping mirrors the target
super-package structure (kernel → workflow → runtime → surfaces →
observability → platform); a package's layer is a stable property even
while the on-disk path is flat.

For the narrative architecture, read `overview.md`; for the CLI surface,
`cli-surface.md`; for the state entities, `state-model.md`.

## Entry points

| Console script | Target | Purpose |
|---|---|---|
| `eawf` / `ea` | `eawf.cli.app:main` | Operator CLI (Typer dispatch). |
| `eawfd` | `eawf.runtime.daemon.main:main` | Background daemon — sole canonical mutator. |
| `python -m eawf` | `eawf.__main__` | Module entry; re-exports `cli.app:main`. |

## Kernel — typed state, storage, config, validation

| Package | Role |
|---|---|
| `state` | Typed entities, IDs, URNs, atomic writes; owns schema + mutations. |
| `store` | JSONL store envelope + per-kind models + compaction. |
| `config` | Layered configuration subsystem (built-in / global / workspace / repo / local). |
| `validate` | Schema layer + invariant layer over `state.json`. |
| `spec` | Typed spec models for phases, iters, waves, audits. |
| `migrations` | State-schema migration chain runner + steps (`eawf migrate`). |

## Workflow — lifecycle, evidence, skills, dispatch

| Package | Role |
|---|---|
| `lifecycle` | Project / subproject / phase / iter / wave lifecycle helpers. |
| `evidence` | Goals, outcomes, hypotheses, audits, decisions, backlog. |
| `skills` | Eä workflow skills (`/research`, `/prep`, `/audit`, `/ship`, `/flow`, …). |
| `agents` | Subagent spec + role library for dispatch. |
| `agent_report` | Typed agent-report storage helpers. |
| `audit_dsl` | Declarative audit-check DSL + check-kind registry. |
| `dispatch` | Subagent prompt-rendering + dispatch envelope. |
| `pr_review` | PR-review automation. |
| `estimation` | EU calculator, segments, recovery. |

## Runtime — daemon, harness adapters, execution substrate

| Package | Role |
|---|---|
| `daemon` | eawfd daemon — single canonical writer of `state.json` + stores. |
| `runtimes` | Harness adapters (Claude / Codex / OpenCode). |
| `mcp` | MCP server install / update / remove primitives. |
| `sandbox` | Sandbox / permission policy enforcement. |
| `session` | Agent-session start / checkpoint / close / recover. |
| `lock` | portalocker wrapper, sibling lockfiles, stale recovery. |
| `budget` | Per-wave token-budget policy + service surface. |
| `ci_loop` | CI-fix loop subsystem. |
| `worktree` | Worktree create / merge-back / cleanup. |
| `hooks` | Eä hook router + runner (`eawf hook run`). |
| `vcs` | Version-control helpers. |

## Surfaces — operator-facing rendering

| Package | Role |
|---|---|
| `cli` | Typer dispatch + command handlers. |
| `tui` | Textual TUI operator surface. |
| `render` | Managed-region markers, manifest, drift detection, doc render. |

## Observability — telemetry, logging, diagnostics

| Package | Role |
|---|---|
| `telemetry` | Vendored row models, pricing snapshot, projection. |
| `logging` | Structured-logging support for library modules. |
| `doctor` | Install / runtime diagnostics + doc-verify. |
| `bench` | Performance bench harness (`eawf bench`). |
| `eval` | Skill-eval semantic-scoring layer. |

## Platform — install, packaging, durable assets

| Package | Role |
|---|---|
| `profiles` | Profile composition subsystem. |
| `registry` | Read-only helpers for `~/.eawf/registry.json`. |
| `install` | Init wizard, global install, instrument probe. |
| `templates` | Jinja2 template payloads bundled with the wheel. |
| `artifacts` | Typed artifact helpers. |
| `memory` | Authoritative `memory.jsonl` + state-cache. |
| `scrub` | Scrub helpers for artifact prose (PII / path hygiene). |
| `lint` | Custom static-analysis rules for the codebase. |
| `backup` | Manual snapshot backup surface. |
| `docs` | Documentation-generation surface for the reference site. |

## Resource packages (no logic)

| Package | Contents |
|---|---|
| `schemas` | Bundled JSON Schema files (`state`, `config`, `skill-output`, `plan-view`). |
| `_data` | Bundled data resources (service-manager unit templates). |
