# Eä documentation

Internalized v0.1 spec — design intent for the `eawf` framework, distilled
into present-tense reference material that lives alongside the code. The
source tree (`src/eawf/`) is the implementation; this directory is the
design intent. When the two drift, the source tree wins —
`AGENTS.md` rule "verify before claiming" applies (read source +
grep call sites before quoting behaviour).

## Architecture

- **[Overview](architecture/overview.md)** — naming, framework repo
  structure, project-local `.ea/` skeleton, commit policy.
- **[State model](architecture/state-model.md)** — top-level state
  fields, core records, ID grammar, estimation model, large-entity
  handling, validation invariants.
- **[CLI surface](architecture/cli-surface.md)** — direct-verb command
  surface, global flags, command-group inventory, runtime stack
  rationale.
- **[Workflow](architecture/workflow.md)** — high-automation lifecycle,
  required skills, skill algorithms, worktree contract, `/init` and
  `/flow` DAGs.
- **[Profiles](architecture/profiles.md)** — profile system,
  v0.1 profile bodies (`core`, `python`, `research`, `quant`-stub,
  `ml`-stub), composition / merge algorithm.
- **[Plugins](architecture/plugins.md)** — Eä CLI as source of truth;
  Claude adapter as plugin; OpenCode adapter spec (deferred);
  Superpowers integration policy; hooks and MCP catalog.
- **[Memory](architecture/memory.md)** — `memory.jsonl` authoritative
  store; markdown views are generated; sync, schema, efficiency rules.
- **[Skill envelope](architecture/envelope.md)** — uniform skill output
  envelope, per-skill body schemas, JSONL record envelope, event
  payload, config schema sections.
- **[Statusline](architecture/statusline.md)** — Claude Code statusline
  design, modules, glyph / color modes, performance budgets, install
  prompt.
- **[Installation](architecture/installation.md)** — `eawf init`
  project install, `eawf global install` user install, migration
  policy, presets.

## Policy

- **[AGENTS.md / CLAUDE.md](policy/agents-claude-md.md)** — the
  CLAUDE.md shim rule, AGENTS.md generation, render-layer note (compose
  before render), core rule modules.
- **[No PLAN.md / DECISIONS.md / BACKLOG.md](policy/no-plan-md.md)** —
  state lives in `state.json` and JSONL stores; markdown is generated
  / curated, not source of truth.
- **[Fixed decisions](policy/fixed-decisions.md)** — index of v0.1
  invariants (distribution, naming, schemas, defaults, lock backend,
  UI stack, profile shipping status) that downstream design may not
  relitigate without an ADR.

## Reference

- **[Enums](reference/enums.md)** — canonical `StrEnum` values for
  every state field (status, kind, priority, severity, risk,
  confidence).
- **[Exit codes](reference/exit-codes.md)** — canonical exit codes
  emitted by every `eawf` CLI handler plus `--json` envelope shape.
- **[Hook events](reference/hook-events.md)** — `HookEvent` shape,
  per-event payloads, idempotence key, Claude Code mapping table,
  `eawf hook run` CLI surface.
- **[Lockfile semantics](reference/lockfile-semantics.md)** —
  `portalocker` sibling-lockfile contract, atomic-write protocol,
  stale-lock recovery.
- **[URN namespace](reference/urn-namespace.md)** — `urn:eawf:v1:*`
  format rules, kind catalog, query / fragment components, agent
  usage guidance.
