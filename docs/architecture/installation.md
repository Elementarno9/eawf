# Installation

*Project install (`eawf init`), global install (`eawf global install`), and migration policy.*

## Project install: `eawf init`

`eawf init` is detailed enough to produce useful project / subproject
state immediately. It runs an interactive wizard on TTY and supports
`--no-input` for CI / scripts.

### `eawf clone-repo` flow

`eawf clone-repo <git-url> --code <PROJECT_CODE> [--workspace
<WORKSPACE_CODE>] [--path <dir>] [--profile ...]` combines clone +
init.

Algorithm:

1. Resolve destination path: explicit `--path`, else workspace root +
   repo slug from URL.
2. Refuse if destination exists unless `--existing` or `--force` policy
   is explicit.
3. Run `git clone <url> <path>`; record remote URL as metadata, not
   secret.
4. Detect project title / slug / default branch / languages / tools.
5. Run repo init in the cloned repo: create `.ea/config.yaml`,
   `.ea/state.json`, `.ea/schema.json`, `.ea/acceptance.yaml`.
6. Set `project.code=<PROJECT_CODE>` and repo URN
   `urn:eawf:v1:repo:<PROJECT_CODE>`.
7. If a workspace is provided / found, register
   `{code, absolute_path, state_urn:
   urn:eawf:v1:repo:<PROJECT_CODE>}` in
   `urn:eawf:v1:workspace:<code>`.
8. Render selected runtime files (`AGENTS.md`, CLAUDE shim,
   skills / agents / hooks) without enabling plugins unless policy
   says yes.
9. Run `eawf validate --strict` and `eawf doctor`.
10. Print next actions: `eawf`, `/research`, `/prep`.

Fallbacks:

- Clone auth fails: stop, show auth hint, write no state.
- Repo already has `.ea/`: run migration / audit mode; do not
  overwrite.
- Workspace registration fails: keep repo initialized standalone and
  write a pending registration task.
- Validation fails: keep files, mark repo `needs_setup`, show repair
  commands.

### Step 0: instrument probe

Before any other step, Eä probes available instruments and caches the
result at `.ea/instrument-probe.json` (override path with
`EA_INSTRUMENT_PROBE`).

Probe order:

1. `git` (always hard for VCS-based projects)
2. `<runtime>` — `claude` per selected adapter
3. `uv` (hard when `python` profile composed)
4. `gh` (soft; PR open / update degrades to manual hint when missing)
5. `npm`, `jq`, `semgrep`, `pre-commit`, `docker`, `rtk`, `caveman`
   (soft; profile-declared)

Behavior:

- **Hard tool missing** → abort with install hint; do not write any
  state.
- **Soft tool missing** → record `not_configured`, continue, surface in
  `eawf doctor` and in skill output envelope `header.instrument_probe`.
- **Cache** invalidates on shell session change or explicit
  `eawf doctor --reprobe`.

Hard requirements compose strictest-wins per the profile merge
algorithm.

### Steps 1–12 (summary)

The full wizard covers:

1. **Repo detection**: git root, default branch, existing AGENTS /
   `.claude/` / `.ea/`, language / tooling, tests / lint / typecheck /
   build commands, remote repo metadata if `gh` available.
2. **Workspace / state placement**: repo-owned + workspace link
   (recommended), repo-only, or workspace-only.
3. **Project identity**: code, title, description, primary domains,
   default branch, status.
4. **Subprojects**: optional multi-workstream split (`COLLAR`,
   `PLATFORM`, ...).
5. **Profile selection**: detected / recommended profiles
   (`core + python + research` typical).
6. **Lifecycle depth**: simple / standard / multi-workstream.
7. **Goals / outcomes** seed (optional).
8. **Commands and gates**: detect tests / lint / typecheck / build,
   acceptance checks; write `.ea/acceptance.yaml`.
9. **Runtime / plugin integration**: Claude Code plugin choice
   (skills, agents, hooks, slash commands).
10. **MCP selection**: per-MCP secrets / env handling; never write
    secrets to committed files.
11. **Write plan and confirmation**: list of files to create / modify /
    skip, then ask `apply this plan? Y/N/edit`.
12. **Validation**: `eawf validate --strict` + `eawf doctor`; print
    next commands.

### Required write ordering (Step 11)

1. `.ea/config.yaml` — profile composition result baked in.
2. `.ea/state.json` — minimal core only; optional keys materialized per
   composed profiles.
3. `.ea/schema.json` + `.ea/acceptance.yaml`.
4. **Profile compose pass** — resolve `requires` graph, deep-merge
   rules / agents / hooks / MCPs, persist conflict decisions.
5. `AGENTS.md` — rendered from composed profile result, never from raw
   selected list.
6. `CLAUDE.md` — hardcoded `@AGENTS.md\n` shim.
7. `.claude/skills/`, `.claude/agents/`, `.claude/hooks/` — rendered
   from composed profile result.
8. `.gitignore` append for `.ea/local/**`, `.ea/cache/**`,
   `.ea/tmp/**`, `.ea/secrets/**`.
9. Workspace registration if linked.

Profile composition MUST complete before AGENTS.md or any plugin /
skill / agent file is rendered. A render pass that finds `composition`
absent or stale aborts with `state.health: needs_setup`.

### Installation recommendation presets

- **Minimal**: Eä CLI, `rich/textual`, no MCPs, generated AGENTS /
  CLAUDE only.
- **Recommended**: Minimal + Claude adapter, core skills, agents, state
  hooks, `git`, `uv`, optional `gh`.
- **Full**: Recommended + profile MCPs, statusline, session restore,
  semgrep, pre-commit, docker, memory hooks.
- **High-security**: Recommended + read-only MCPs only, no auto-push,
  strict secret scan, optional semgrep / pre-commit.
- **Research**: Recommended + Zotero MCP optional, citation / memory
  audit checks.
- **App / UI**: Recommended + Playwright MCP optional, npm / docker
  checks if detected.

## Global install: `eawf global install`

`eawf global install` configures user-scope tools and runtime plugins.

### Step 1: platform check

Detect:

- OS: macOS / Linux / Windows / WSL2,
- shell: zsh / bash / fish / pwsh,
- package managers: brew / apt / winget / scoop / npm / uv,
- Claude Code installed / authenticated,
- GitHub CLI auth,
- Git, GitHub CLI (`gh`), uv, npm / node, jq, semgrep, pre-commit,
  docker, rtk, caveman.

### Step 2: install scope

```text
User install mode:
  1. Minimal: eawf CLI only
  2. Recommended: eawf + runtime plugins + token / security tools
  3. Full: recommended + notifications + dotfile sync helpers
```

### Step 3: runtime plugins

Claude Code plugin enables global skills, global agents, hooks,
commands, and state context injection. OpenCode is deferred to v0.2.

### Step 4: optional tools

Prompt with platform warnings:

| Tool | Purpose | Recommended | Required when | Install? |
|---|---|---|---|---|
| git | VCS / worktrees / clone | yes | VCS workflows | [x] |
| gh | GitHub PR / check automation | optional | GitHub PR automation | [ ] |
| uv | Python tool / env runner | yes | Python dev / profile tools | [x] |
| npm | Node tooling | optional | Node profiles | [ ] |
| jq | Shell hook JSON parsing | optional | copied shell hooks | [ ] |
| semgrep | static / security checks | optional | security profile | [ ] |
| pre-commit | repo hook framework | optional | repos using pre-commit | [ ] |
| docker | container / browser checks | optional | infra / browser acceptance | [ ] |
| rtk | token / output compression | optional | RTK hook / profile | [ ] |
| caveman | terse token-saving style | optional | caveman profile | [ ] |

Superpowers is **not** offered as an install option. Best practices are
internalized into Eä core (see `docs/architecture/plugins.md`).

### Step 5: global defaults

```text
Preferred command: eawf / ea
Install command aliases: ea or none
Default profiles for new repos:
Default runtimes:
Default hooks:
Default MCP policy:
Token-saving mode default: off / lite / full
Notification target:
```

### Step 6: secrets policy

```text
Secret manager:
  [ ] 1Password CLI
  [ ] env files only
  [ ] sops/age
  [ ] none
```

Eä never requires 1Password but can support it. Non-env secret
backends are deferred beyond v0.1.

### Step 7: command alias resolution

`eawf global install` must resolve the user-facing command before
rendering docs / plugins / hooks / statusline.

Rules:

1. Canonical installed console script is `eawf`.
2. Detect whether `ea` already exists on PATH and whether it points to
   Eä.
3. If `ea` is free, recommend `preferred_command: eawf` and alias
   `[ea]`.
4. If `ea` collides, recommend `preferred_command: eawf` and no alias;
   do not overwrite existing `ea`.
5. User may choose `eawf` only with no aliases.
6. Store choices in `~/.ea/config.yaml`.
7. `eawf sync` re-renders generated commands using
   `cli.preferred_command`.

Config:

```yaml
cli:
  canonical_command: eawf
  preferred_command: eawf     # eawf | ea
  install_aliases: []         # [ea] only when non-colliding and user-approved
  omit_ea_alias: true         # true when `ea` collides or user declines it
```

### Step 8: apply and doctor

Run with the configured preferred command:

```bash
<preferred_command> doctor
```

## User-scope install

The user-scope flow installs the `eawf` CLI under the operator's home
directory via `uv tool` so a single binary serves every repo. Per-repo
plugin assets (skills, agents, hooks under `.claude/`) are still rendered
by `eawf plugin install claude` inside each project — the user-scope
install only ships the dispatcher.

### Install

```bash
# From a local clone (D09: PyPI publication deferred to v0.4):
uv tool install --from . eawf

# Once published on PyPI (post-v0.4):
uv tool install eawf
```

`uv tool install` creates an isolated venv under `~/.local/share/uv/tools/`
and exposes `eawf` (and the `ea` alias if not colliding) on PATH.

### Per-repo plugin assets

After the user-scope binary is on PATH, render the plugin tree inside
every repo that wants Eä integration:

```bash
cd <repo>
eawf plugin install claude
```

This writes `.claude/skills/`, `.claude/agents/`, `.claude/hooks/`, and
patches `.claude/settings.json` with the managed namespace.

### Drift check: `eawf plugin update claude --check`

`plugin update claude --check` runs the renderer in dry-mode: every
managed file is rendered and diffed against the on-disk payload, but no
bytes are written. The exit envelope reports which files *would* change
so the operator can preview before applying.

```bash
eawf plugin update claude --check    # preview — no writes
eawf plugin update claude            # apply
```

The check still raises `IntegrityViolation` (exit code 8) if a hand-edit
is detected — the same surface as a real update — so the dry-mode is
safe to wire into CI.

### Version drift check: `eawf doctor --user-scope`

`eawf doctor --user-scope` probes `uv tool list` for an `eawf` entry and
compares its version against the running binary's `eawf.__version__`.
The probe is additive — it appends a `user_scope` check to the doctor
envelope without changing the base check set.

Outcomes:

- `ok` — installed user-scope version matches the running binary.
- `warn` (stale) — versions differ; the message suggests
  `uv tool upgrade eawf`.
- `info` — `uv tool list` ran cleanly but no eawf entry appears (the
  operator has not run `uv tool install --from . eawf`).
- `warn` (uv missing) — `uv` is not on PATH; the probe degrades
  gracefully instead of crashing.

The probe MUST NOT run when `--user-scope` is absent, so the default
`eawf doctor` invocation stays cheap.

### Install from the committed marketplace

Instead of cloning and running `eawf plugin install`, an operator can install the runtime plugin straight from the repo's committed marketplace pointers. The canonical repo is `https://github.com/Elementarno9/eawf`; each runtime reads its own pointer file (`.claude-plugin/marketplace.json` for Claude Code, `.agents/plugins/marketplace.json` for Codex), and the tag-triggered `plugin-release.yaml` workflow keeps the published artifacts current.

Claude Code — add the marketplace by repo slug, then install from it:

```text
/plugin marketplace add Elementarno9/eawf
/plugin install eawf@eawf-local
```

Codex — point `plugin marketplace add` at the repo; `marketplace add` registers the `eawf-local-codex` marketplace and auto-installs its plugin (Codex has no separate `plugin install`):

```bash
codex plugin marketplace add https://github.com/Elementarno9/eawf
```

See `docs/architecture/plugins.md` for the marketplace-pointer schemas and how the `plugin-release.yaml` workflow publishes each runtime's artifact.

## Migration into existing workflow projects

Migration automation is **deferred beyond v0.1**. Experience with fresh
Eä projects comes first.

v0.1 may detect existing workflow artifacts during init (`AGENTS.md`,
`.claude/skills`, custom workflow state files, `PLAN.md` /
`DECISIONS.md` / `BACKLOG.md`, hooks, commands, custom state CLIs) but
must not import, rewrite, disable, or clean them automatically.

Allowed v0.1 behavior:

- report detected non-Eä workflow artifacts,
- warn about likely conflicts,
- install Eä in non-destructive mode using managed regions / files only,
- ask before overwriting any generated target,
- preserve all existing reports, briefs, incidents, milestones, data
  manifests, run links, and domain docs.

Deferred: all `eawf migrate *` commands.

Decision: do not implement non-Eä migration commands until enough real
Eä usage exposes stable patterns.

## Cross-references

- AGENTS.md / CLAUDE.md generation policy — `docs/policy/agents-claude-md.md`.
- Profile composition — `docs/architecture/profiles.md`.
- Plugin / hook / MCP catalog — `docs/architecture/plugins.md`.
- Statusline install prompt — `docs/architecture/statusline.md`.
- Workflow DAGs — `docs/architecture/workflow.md`.
