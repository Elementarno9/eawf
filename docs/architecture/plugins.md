# Plugin architecture

*Eä CLI is source of truth; runtime adapters are plugins.*

## Decision

**Eä CLI remains source of truth; plugins are runtime adapters.** A
plugin renders runtime-native files (skills, agents, hooks, settings)
from the same composed profile result the CLI uses, so multiple runtimes
stay synchronized without one becoming authoritative.

## Claude Code adapter (v0.1)

Project / user install renders:

```text
.claude-plugin/plugin.json      # if packaged as plugin
.claude/skills/**/SKILL.md
.claude/agents/*.md
.claude/hooks/*.{sh,ps1,js}
.claude/commands/*.md           # runtime-supported command files
```

Responsibilities:

- expose Eä skills,
- expose Eä agents,
- run hooks,
- inject state summary at session start,
- provide `/ea:*` commands if the runtime supports them.

Source: `src/eawf/runtimes/claude/`. Plugin lifecycle commands:
`eawf plugin install claude`, `eawf plugin update claude`,
`eawf plugin doctor claude`.

Generated assets update only Eä-owned files or managed regions
(`<!-- BEGIN EAWF:managed ... -->` / `<!-- END EAWF:managed ... -->`).
Hash mismatch or unmanaged conflicts raise an error with a diff and
repair instructions; `eawf sync` fixes managed-region drift when the
prior managed hash matches.

## OpenCode adapter (deferred to v0.2)

OpenCode supports JS / TS plugins in `.opencode/plugins/` or npm plugins
in config. Eä would render:

```text
.opencode/plugins/ea.ts
.opencode/agents/*.md
.opencode/skills/**/SKILL.md
opencode.json or managed generated section
```

Responsibilities (when shipped): bridge events like
`tool.execute.before/after`, `session.compacted`, `session.idle`,
`permission.*`; inject state context into compaction; expose Eä custom
tools; render agents with OpenCode `mode`, `permission`, `model`,
`prompt`. v0.1 ships the adapter spec only — no installable code path.

## Why plugin integration helps

- Hooks become runtime-native.
- Skills / agents are discoverable without repeated prompts.
- State context can be injected automatically.
- Permissions can be aligned with Eä policy.
- User-scope install can update all repos consistently.

## Why plugin integration is not enough

Plugins cannot own:

- project scaffolding,
- schema migrations,
- `.ea/state.json` validation,
- profile composition,
- cross-runtime rendering,
- the user install wizard,
- git-aware drift management.

Therefore: plugin = adapter, CLI = product.

## Superpowers integration

Drop the Superpowers plugin entirely from Eä installation. Do not offer
the full plugin, the selected install, or the bootstrap. Best practices
are internalized into Eä core rules instead:

1. **Fresh-context review** before ship / merge.
2. **Verification before completion**: no done claim without evidence.
3. **Systematic debugging**: root cause before fix; after repeated
   failed fixes, re-question assumptions.
4. **Safe parallel agents**: parallelize independent investigation;
   coordinate implementation with wave claims.
5. **Worktree isolation** for non-trivial execution.
6. **Plan / spec before large edits**, but not generic PLAN.md.
7. **TDD where applicable**, not universal hard law.

Generated AGENTS.md expresses these as Eä rules, not Superpowers
references.

## Hooks

Eä-provided hooks (rendered by the Claude adapter when enabled):

| Hook | Trigger | Purpose | Required tools | Outcome | Risks / fallback |
|---|---|---|---|---|---|
| `state-validate` | After any `eawf` state / store mutation | Validate schema, refs, locks, URNs | Eä only | State remains parseable | If failed, rollback write and record repair event |
| `generated-drift` | After `eawf render`, AGENTS / adapter edits, or pre-ship | Detect generated file drift | Eä only | Reproducible runtime files | Advisory by default; `eawf sync` fixes |
| `pre-ship-check` | Before `/ship` commit / push / PR | Ensure audit / evidence / memory review exists | `git`; optional `gh` | No unevidenced ship | Blocks unless override policy allows |
| `post-edit-lint` | After code file edit by runtime hook | Run scoped lints / checks based on profile | profile tools; optional `uv`, `npm`, `docker` | Faster feedback | Skip if tool missing; report `not_configured` |
| `secret-scan` | Before commit / ship | Prevent secrets in committed files | Eä regex; optional semgrep rules | Safer commits | False positives ask user |
| `worktree-gate` | Before 2+ write-capable agents or risky wave | Create / assign worktrees and file scopes | `git` | Isolated parallel writes | If `git` missing, disable parallel writers |
| `memory-capture` | Session end, `/ship`, `/polish` | Promote useful memories, mark stale ones | Eä only | Better future context | Ask before prune / delete |
| `statusline` | Runtime statusline render / prewarm | Show live state / git / context health | Eä; optional `git`, `gh` | Better orientation | Fall back to compact / plain status |
| `session-restore` | Runtime session start | Detect interrupted sessions and suggest resume | Eä / Python stdlib | Less lost work | Disabled if transcripts unavailable |
| `runtime-hook-router` | Runtime Pre / Post tool event | Route runtime hook events to enabled hooks through Eä native dispatcher | Eä native preferred | One hook entrypoint | If runtime lacks hooks, features degrade to explicit skill / CLI calls |

Hook design rules:

- Hooks are fail-open for advisory checks and fail-closed only for
  state corruption, secrets, destructive actions, or protected-branch
  VCS policy.
- Hooks emit structured events to `.ea/stores/events.jsonl` when they
  block, degrade, or skip due to missing tools.
- Deterministic checks belong in hooks; reasoning-heavy decisions
  belong in skills.
- Eä replaces shell `dispatch.sh` with native `eawf hook run <event>`,
  so `jq` / bash / GNU-tool dependencies disappear from core. Preamble
  injection is optional for runtimes that support it.

## MCP catalog

First-party catalog kept intentionally small for MVP:

| MCP | Source | Profiles | Why add | Outcome | Effort | Risks |
|---|---|---|---|---|---|---|
| `context7` | external | core / apps / python / js | Current library docs lookup | Fewer stale API assumptions | Low | External availability; doc mismatch |
| `playwright` | external | apps / frontend / docs | Browser automation, screenshots, UI checks | UI evidence for audit / review | Medium | Heavy deps, flaky browsers, write / click risk |
| `zotero` | external | research / docs / ml / quant | Search / read research library, metadata, citation keys | Stronger citation-backed `/research` | Medium | Local library privacy, write tools must default off |
| `custom` | user-defined | any | User adds local / team MCP server once | Extensible workflow without Eä release | Varies | Unknown auth / write / security risk |

Deferred MCPs (`ghidra`, `x64dbg`, ...) need dedicated MCP repos, safety
wrappers, read / write capability metadata, and RE profile policies
before catalog inclusion.

Defaults:

- `context7` recommended for most programming profiles.
- `playwright` optional for browser / UI / docs profiles.
- `zotero` optional for research-heavy profiles; write tools off by
  default.
- Custom MCPs default to disabled until the user explicitly trusts
  them.
- Secrets are always referenced by env / secret manager, never
  committed (`${ENV:NAME}` syntax).
- Eä renders config and runs doctor checks; external MCP server install
  remains explicit unless the user chooses managed install.

`eawf mcp add / install / update / remove` only mutates entries with
`owner: eawf`. Non-Eä MCP entries are never overwritten.

## Cross-references

- Hook event schema and Claude routing — `docs/reference/hook-events.md`.
- Statusline — `docs/architecture/statusline.md`.
- Project / global install — `docs/architecture/installation.md`.
- Profile-recommended hooks / MCPs — `docs/architecture/profiles.md`.
