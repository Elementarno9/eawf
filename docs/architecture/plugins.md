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
`eawf plugin doctor claude`, `eawf plugin package claude`.

## Two install modes

The Claude Code adapter exposes two distinct emission paths. They are
designed to coexist — pick the one that matches the audience.

| Mode | Command | Output | Audience |
|---|---|---|---|
| Repo install | `eawf plugin install claude` | `<repo>/.claude/skills/`, `agents/`, `hooks/`, `settings.json` | Per-repo workspace wiring; pairs with `.ea/state.json` and the runtime hook router. |
| Plugin package | `eawf plugin package claude` | `<target>/.claude-plugin/plugin.json` + `<target>/skills/` + `<target>/agents/` + optional `marketplace.json` + optional `README.md` | Standalone Claude Code marketplace install via `/plugin marketplace add <path>` then `/plugin install eawf@eawf`. |

### Repo install mode

Writes per-repo assets under `<repo>/.claude/`. This is the path the
Eä `eawf init` wizard wires up. It carries hook scripts (event router
glue) and patches `settings.json` with an `__eawf_managed` namespace so
the per-repo plugin doctor can detect drift. Source of truth:
`src/eawf/runtimes/claude/plugin_install.py`.

### Plugin-package mode

Writes a self-contained tree at `<target>/` that Claude Code can install
through its plugin marketplace surface. The output deliberately differs
from the repo-install layout:

- **No `.claude/` prefix.** The tree IS the plugin; CC mounts it under
  `~/.claude/plugins/...` automatically when the user runs
  `/plugin install`.
- **Session-level `hooks.json` + `hooks/`.** See "Session-level plugin
  hooks" below.
- **No `settings.json`.** A marketplace plugin must not write into the
  user's CC settings; that surface is owned by the user.
- **No `.ea/`.** Project state lives in the user's repo, not in the
  plugin.

Source of truth: `src/eawf/runtimes/claude/plugin_package.py`. The
emit is byte-stable: re-running with the same inputs produces a
byte-identical tree (covered by the W05 idempotence test).

### Session-level plugin hooks (B015 resolved in v0.2)

The packaged tree includes a `hooks.json` manifest at the plugin root
plus a `hooks/<event>.sh` wrapper per subscribed event. The split
between what CC sees and what stays eawf-internal is deliberate:

| Layer | Surface | Events |
|---|---|---|
| CC plugin manifest (`hooks.json`) | What Claude Code can observe reliably | `SessionStart`, `Stop`, `PreToolUse(Bash)`, `PostToolUse(Bash)` filtered to `git commit` / `git push` |
| State CLI (`eawf hook run`) | Workflow-internal lifecycle the state writer controls | `wave_open`/`wave_close`, `iter_open`/`iter_close`, `phase_open`/`phase_close`, `pre_audit`/`post_audit` |

The six session-level entries in `hooks.json` cover the events Claude
Code emits regardless of which skill, agent, or slash command is
driving the session. The workflow-internal events stay fired from
inside the state CLI because CC's `UserPromptSubmit` matcher cannot
observe slash-command sub-skill dispatch (e.g. `/flow` runs sub-skills
internally without re-emitting their slash prompts) and agent calls
to the state CLI never trigger a prompt at all. A manifest-level
subscription to those events would be lossy in both directions, so the
state writer keeps ownership.

Each `hooks.json` entry resolves to
`${CLAUDE_PLUGIN_ROOT}/hooks/<event>.sh`, the same wrapper script the
repo-install path emits — payload synthesis, CLI dispatch, and exit
codes are byte-identical between the two install modes. The mapping
table lives in `src/eawf/runtimes/claude/hook_map.py` and is the
single source of truth for which events surface in the CC plugin
manifest.

Tracked as backlog item B015; resolved in P13 W05.

Generated assets update only Eä-owned files or managed regions
(`<!-- BEGIN EAWF:managed ... -->` / `<!-- END EAWF:managed ... -->`).
Hash mismatch or unmanaged conflicts raise an error with a diff and
repair instructions; `eawf sync` fixes managed-region drift when the
prior managed hash matches.

## Codex adapter

Renders a native Codex CLI plugin under
`<plugin_root>/.codex-plugin/plugin.json` (the canonical Codex manifest
file). Skills, agents, and hooks live in subdirectories of the same
plugin root; an `[plugins.eawf] enabled = true` table is patched into
the scope-correct `config.toml` between
`# ---- __eawf_managed begin/end ----` markers so user-authored TOML
elsewhere stays untouched. A sidecar `.codex-plugin/.eawf-managed.json`
carries the hash registry the `plugin doctor` command checks.

| Scope | Plugin root | Config patched |
|---|---|---|
| `project` (default) | `<workspace>/.codex/plugins/eawf/` | `<workspace>/.codex/config.toml` |
| `user` | `~/.codex/plugins/eawf/` | `~/.codex/config.toml` |

Source: `src/eawf/runtimes/codex/`. Plugin lifecycle commands:
`eawf plugin install codex [--scope ...]`,
`eawf plugin update codex [--scope ...]`,
`eawf plugin doctor codex [--scope ...]`,
`eawf plugin package codex [--target ...]`.

### Codex marketplace package

Per the Codex Build-plugin reference, dropping a plugin directory under
`~/.codex/plugins/<name>/` does **not** auto-load it — Codex requires
marketplace registration before discovery. The `install codex` command
writes the plugin tree at the scope-correct location and toggles
`[plugins.eawf] enabled = true` in `config.toml`, but discovery still
needs a marketplace step.

`eawf plugin package codex [--target ./build/eawf-codex-marketplace]`
emits a self-contained marketplace tree:

```text
<target>/
  marketplace.json
  plugins/
    eawf/
      .codex-plugin/plugin.json
      skills/<name>.md
      agents/<role>.md
      hooks/<event>.sh
```

`marketplace.json` lives at `.agents/plugins/marketplace.json` per the
Codex Build-plugin reference (root-level `marketplace.json` is rejected
by `codex plugin marketplace add`). It carries the Codex marketplace
schema (`name`, `interface.displayName`,
`plugins[].{name,source,policy,category}`) with
`source: {source: "local", path: "./plugins/eawf"}`. The operator then
runs:

```bash
codex plugin marketplace add ./build/eawf-codex-marketplace
```

`marketplace add` registers the marketplace and auto-installs its plugins. Codex has no separate `plugin install` subcommand — only `plugin marketplace {add,upgrade,remove}`. The `[plugins.eawf] enabled = true` block that `eawf plugin install codex` writes to `~/.codex/config.toml` activates the plugin once Codex discovers it. After registration, Codex caches the plugin under `~/.codex/plugins/cache/eawf/eawf/<version>/` and loads it from there.

## Install from the committed marketplace

Eä ships its own marketplace pointers in the repo, so an operator installs the plugin straight from GitHub without first cloning or running `eawf plugin package`. Each runtime reads a different committed pointer file, and the tag-triggered `plugin-release.yaml` workflow keeps the published artifacts those pointers reference up to date (Claude -> npm, Codex -> the `plugins-dist` branch). The canonical repo is `https://github.com/Elementarno9/eawf`.

### Claude Code flow

`.claude-plugin/marketplace.json` declares the `eawf` marketplace whose single `eawf` plugin resolves from the `@elementarno/eawf` npm package. Add the marketplace by repo slug, then install the plugin from it:

```text
/plugin marketplace add Elementarno9/eawf
/plugin install eawf@eawf
```

`marketplace add` reads `.claude-plugin/marketplace.json` from the repo's default branch; `install` pulls the published `@elementarno/eawf` npm package the pointer names. This is the cross-workspace path — it does not need `--scope user` (which the Claude adapter rejects) because Claude Code mounts the marketplace plugin under `~/.claude/plugins/...` for every workspace.

### Codex flow

`.agents/plugins/marketplace.json` declares the `eawf` marketplace whose single `eawf` plugin resolves from a `git-subdir` source: the `./plugins/eawf` subtree on the `plugins-dist` branch of the same repo. Point `codex plugin marketplace add` at the repo (Codex reads the committed `.agents/plugins/marketplace.json`):

```bash
codex plugin marketplace add https://github.com/Elementarno9/eawf
```

`marketplace add` registers the marketplace and auto-installs its plugins; Codex has no separate `plugin install` subcommand (only `plugin marketplace {add,upgrade,remove}`). The `plugins-dist` branch is published by the tag-triggered `plugin-release.yaml` workflow, so the `git-subdir` source always resolves to the latest packaged Codex tree.

## OpenCode adapter

Renders the native OpenCode plugin file plus its hash sidecar under
OpenCode's plugin auto-discovery dir. The renderer does not push the
plugin file into the `plugins:[...]` array inside `opencode.json` —
that array is reserved for npm packages; auto-discovery handles local
plugins. `opencode.json` is patched only in its `mcp` block to leave
user-authored top-level keys untouched.

| Scope | Plugin file | Config patched |
|---|---|---|
| `project` (default) | `<workspace>/.opencode/plugins/eawf.js` + sidecar `.eawf-managed.json` | `<workspace>/opencode.json` |
| `user` | `$OPENCODE_CONFIG_DIR/plugins/eawf.js` (defaults to `~/.config/opencode/plugins/eawf.js`) + sidecar | `$OPENCODE_CONFIG_DIR/opencode.json` |

Source: `src/eawf/runtimes/opencode/`. Plugin lifecycle commands:
`eawf plugin install opencode [--scope ...]`,
`eawf plugin update opencode [--scope ...]`,
`eawf plugin doctor opencode [--scope ...]`.

## `--scope project|user`

Every runtime install command accepts `--scope project` (default) or
`--scope user`. Project scope writes under the active workspace and
pairs with `.ea/state.json` and the runtime hook router. User scope
writes under the runtime's user-config root and applies to every
workspace the user opens with that runtime.

| Runtime | `--scope user` supported | Notes |
|---|---|---|
| `claude` | rejected (`InvalidInput`, exit 3) | use `eawf plugin package claude` + Claude Code's `/plugin install` for cross-workspace installs. |
| `codex` | yes | writes under `~/.codex/plugins/eawf/`; patches `~/.codex/config.toml`. |
| `opencode` | yes | writes under `$OPENCODE_CONFIG_DIR/plugins/eawf.js` or `~/.config/opencode/plugins/eawf.js`. |

A project-scope install of `codex` or `opencode` warns when a
user-scope eawf install of the same runtime already exists (the
runtime would load two `eawf` plugins with undefined precedence).
`--force` overrides the warning; `--no-input` mode refuses without
prompting. The doctor commands additionally surface legacy
workspace-root paths (`<ws>/plugin.js`, `<ws>/.codex/{skills,agents,hooks}/`)
under `legacy_paths` so an operator can prune them manually per the
AGENTS.md deletion rule.

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
- Hooks emit structured events to `.ea/store/event.jsonl` when they
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
