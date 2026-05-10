# CLI surface

*Direct verbs (no nested `eawf state ...`); `--json`, `--plain`, `--no-input`, deterministic non-TTY behavior.*

## Direct command surface

State is the core, so commands are direct: `eawf phase`, `eawf iter`,
`eawf wave`, not `eawf state phase`.

All commands support `--json`, `--plain`, `--no-input`, and deterministic
non-TTY behavior unless explicitly TUI-only. All mutating commands
acquire the relevant sibling lockfile and append an audit event to
`.ea/store/event.jsonl`.

Run `eawf <command> --help` for the authoritative flag list. Source of
truth: `src/eawf/cli/app.py` and the per-group handlers in
`src/eawf/cli/commands/`.

## Command groups (v0.1)

Each row is a CLI verb / group. Mutates → writes state.json,
JSONL store, or repo files. Errors → command fails with a non-zero exit
code (see `docs/reference/exit-codes.md`).

| Command | Inputs / options | Output | Mutates | Errors / blocks | Source |
|---|---|---|---:|---|---|
| `eawf` | none, TTY | interactive dashboard (deferred; v0.1 falls back to status / help) | no | non-TTY falls back to status / help | `cli/app.py` |
| `eawf status` | `--scope`, `--workspace`, `--json` | active scope, blockers, git, checks | no | no state found | `cli/commands/status.py` |
| `eawf init` | `--repo`, `--workspace`, `--minimal`, `--no-input`, project fields | `.ea/`, AGENTS / CLAUDE, optional plugin plan | yes | dirty / unmanaged overwrite, invalid answers | `cli/commands/init.py` |
| `eawf global install` | alias / statusline / plugin / MCP choices | user config + Claude user assets | yes | alias collision, settings conflict | `install/global_install.py` |
| `eawf workspace init` | `--code`, `--title`, path | workspace `.ea/state.json` | yes | existing non-workspace state | `cli/commands/workspace.py` |
| `eawf workspace add-repo` | code, path, state URN | repo entry | yes | path missing, duplicate code | `cli/commands/workspace.py` |
| `eawf workspace remove-repo` | code | removed repo entry | yes | unknown code | `cli/commands/workspace.py` |
| `eawf workspace validate` | `--strict` | validation report | no | linked repo invalid / missing | `cli/commands/workspace.py` |
| `eawf workspace status` | `--json` | multi-repo status table | no | no workspace state | `cli/commands/workspace.py` |
| `eawf repo init` | project fields, profiles | repo `.ea` files | yes | existing conflict | `cli/commands/repo.py` |
| `eawf repo link` | workspace code / path | workspace index link | yes | no workspace, duplicate | `cli/commands/repo.py` |
| `eawf clone-repo` | git URL, code, workspace, path | cloned + initialized repo | yes | clone / auth / path exists | `cli/commands/clone_repo.py` |
| `eawf state resolve` | cwd, `-w`, env | active state path / reason | no | ambiguous / missing state | `cli/commands/state.py` |
| `eawf validate` | `--strict`, `--workspace` | schema / ref / invariant report | no | schema / ref errors | `cli/commands/validate.py` |
| `eawf sync` | `--check`, `--dry-run`, `--fix` | drift / update report | yes if fix | hash conflict / unmanaged overwrite | `cli/commands/sync.py` |
| `eawf config get` / `set` / `validate` | key / value / scope | layered config | yes for `set` | invalid schema / secret value | `cli/commands/config.py` |
| `eawf project init` | code / title / domains | project record | yes | duplicate project | `cli/commands/lifecycle.py` |
| `eawf subproject add` / `switch` | code / kind / title | subproject record / pointer | yes | duplicate / unknown code | `cli/commands/lifecycle.py` |
| `eawf goal define` | id / title / scope / outcomes | goal record | yes | duplicate / invalid scope | `cli/commands/evidence.py` |
| `eawf outcome define` / `set` | id / scope / metric / threshold | outcome record / measurement | yes | invalid metric / missing audit | `cli/commands/evidence.py` |
| `eawf phase open` / `close` | id, title, scope, audit, checkpoint | phase record | yes | duplicate / no audit / open children | `cli/commands/lifecycle.py` |
| `eawf iter open` / `close` | phase or id, audit | iter record | yes | invalid parent / open waves | `cli/commands/lifecycle.py` |
| `eawf wave plan` / `claim` / `close` / `fail` | wave fields, commit, outcome, reason | wave records | yes | overlap, dirty state, no evidence | `cli/commands/lifecycle.py` |
| `eawf estimate` / `estimate update` | scope, source, confidence | estimate record / version | yes if create | invalid rollup | `cli/commands/estimation.py` |
| `eawf actual start` / `stop` / `recover` | scope, session, status | actual segment | yes | active segment exists / none | `cli/commands/estimation.py` |
| `eawf hypothesis define` / `verdict` / `list` | id / scope / metric / verdict / audit | hypothesis records | yes | vague thresholds / no audit | `cli/commands/evidence.py` |
| `eawf audit add` / `run` / `integrity` / `show` / `list` | id / scope / kind / report | audit records / artifacts | yes | report missing / check failure | `cli/commands/evidence.py` |
| `eawf research show` | id | research brief + peer review | no | unknown brief | `cli/commands/evidence.py` |
| `eawf incident open` / `close` / `view` | id / severity / title / root cause | incident record | yes | duplicate / no evidence | `cli/commands/evidence.py` |
| `eawf artifact add` / `show` | id / kind / uri / hash | artifact index | yes | missing file / hash mismatch | `cli/commands/evidence.py` |
| `eawf decision add` / `list` | id / scope / summary / rationale | decision record | yes | duplicate / missing rationale | `cli/commands/evidence.py` |
| `eawf backlog add` / `close` | id / title / priority / commit | backlog record | yes | duplicate / no evidence | `cli/commands/evidence.py` |
| `eawf memory add` / `promote` / `list` / `compact` / `render-context` / `view` / `stale` | scope / title / body / budget | memory entries | yes for mutators | over limit, no scope | `cli/commands/memory.py` |
| `eawf store compact` | kind / scope / budget | compacted JSONL store | yes | conflict | `cli/commands/store.py` |
| `eawf render-output` | `--format markdown` \| `json` (stdin JSON) | rendered envelope | no | invalid envelope | `cli/commands/render_output.py` |
| `eawf config profile enable` | profile id | enabled profile + materialized state keys | yes | unknown profile / conflict | `cli/commands/config.py` |
| `eawf session start` / `checkpoint` / `close` / `recover` | role / scope / runtime / status | session records | yes | invalid scope / open claims | `cli/commands/session.py` |
| `eawf worktree create` / `list` / `merge-back` / `cleanup` | wave / branch / path | worktree records | yes | dirty root / branch exists / conflict | `cli/commands/worktree.py` |
| `eawf mcp add` / `install` / `update` / `remove` | id / command / risk / env refs | Eä-owned MCP config | yes | non-env secret / unmanaged entry | `cli/commands/mcp.py` |
| `eawf plugin install claude` / `update claude` / `doctor claude` | target choices | Claude assets / settings | yes for install / update | settings conflict / hash conflict | `cli/commands/plugin.py` |
| `eawf plugin package claude` | `--target`, `--include-marketplace`, `--include-readme`, `--force`, `--dry-run` | standalone CC plugin tree (`.claude-plugin/` + `skills/` + `agents/` [+ `marketplace.json`] [+ `README.md`]) | no on `--dry-run`, otherwise yes (filesystem only; no state mutation) | non-empty foreign target → exit 8; unknown runtime → exit 3 | `cli/commands/plugin.py` |
| `eawf hook run` | event, JSON payload stdin | hook result / event | yes maybe | fail-closed / timeout | `cli/commands/hook.py` |
| `eawf cc statusline` / `cc statusline prewarm` | Claude JSON stdin / session+cwd | statusline text / cache | no for render, yes for prewarm | timeout, fallback on error | `cli/commands/cc.py` |
| `eawf doctor` | scope | install / project health | no | missing tools / config | `cli/commands/doctor.py` |
| `eawf update` | templates / schema source | update plan | yes after approval | version conflict | `cli/commands/sync.py` |
| `eawf skill list` / `skill run` | runtime / scope, skill, args | skill catalog / CI execution | yes maybe for `run` | unknown skill | `cli/commands/skill.py` |
| `eawf flow` | goal / budgets / resume | full ADD pipeline result | yes | budget / gate / blocker | `cli/commands/flow.py` |
| `eawf plan show` | scope | active generated plan / spec from state | no | no plan | `cli/commands/plan.py` |

## Global flags (every command)

- `--json`: emit envelope verbatim; agents and CI parse machine output.
- `--plain`: ASCII-only formatted output; no rich glyphs / colors.
- `--no-input`: deterministic non-interactive mode; rejects prompts.
- `--scope <urn-or-id>`: pin scope explicitly when active state is
  ambiguous.
- `-w / --workspace [<code>]`: prefer workspace state over repo state.
- `--save`: persist outputs to `.ea/artifacts/...` when applicable.
- `--dry-run`: show what would change, never mutate.

The short workspace flag is `-w`. `-W` is reserved for future use; in
v0.1 it is unbound (avoiding the conventional Python / GCC meaning of
"warnings").

## Plugin command policy

- User-facing v0.1 plugin lifecycle is `eawf plugin install / update /
  doctor <runtime>`.
- Rendering is an internal library operation used by plugin install /
  update and tests.
- Generated assets update only Eä-owned files or managed regions. Hash
  mismatch or unmanaged conflicts raise an error with a diff and repair
  instructions.
- OpenCode and other harness plugin commands are deferred beyond v0.1.

## CLI / TUI implementation stack

The v0.1 stack is `typer + rich` for the CLI surface and
`questionary + rich` for TTY-interactive prompts (install wizard,
configuration flows). Roles:

- **typer**: parses `eawf phase open --title X`, generates `--help`,
  shell completion.
- **rich**: non-TTY rendering for `eawf status --json`, `--plain`, piped
  output, tables in scripts.
- **questionary**: portable single-prompt TTY interaction backed by
  `prompt_toolkit`. Used for the `eawf init` wizard and short
  per-command prompts; falls back to `--no-input` for non-interactive
  callers.

Core runtime deps (clean install, source of truth: `pyproject.toml`):

| Package | Role |
|---|---|
| `typer` | command parsing, `--help`, shell completion |
| `rich` | non-interactive formatted output (`--plain`, `--json`, tables in scripts) |
| `questionary` (+ `prompt_toolkit`) | TTY-interactive prompts: install wizard, configuration flows |
| `pydantic` + `pydantic-settings` | strict config / state schemas |
| `platformdirs` | global / user config paths |
| `orjson` | fast JSON / JSONL read / write |
| `jsonschema` | emitted schema for non-Python consumers |
| `jinja2` | template rendering (AGENTS.md, plugin assets, CC settings) |
| `portalocker` | cross-platform sibling lockfiles |
| `pyyaml` | layered YAML config / state-extension loaders |

`InquirerPy`, `textual`, and `watchfiles` are NOT runtime deps — the
`typer + rich + questionary` trio covers the v0.1 surface and avoids
dual event loops.

## Bare `eawf` interactive dashboard

Running `eawf` with no args is the entry point for the future
interactive dashboard. v0.1 falls back to `eawf status --plain` or help
on every invocation; a richer multi-pane TUI (state, roadmap,
hypotheses, budgets, audits, PR / ship, memory, config, artifacts) is
deferred beyond v0.1. When materialised it will never mutate state
without explicit confirmation; it will watch `.ea/state.json`,
`.ea/config.yaml`, artifact indexes, git branch / status, and optional
runtime session files.

## Cross-references

- Exit codes — `docs/reference/exit-codes.md`.
- Hook events — `docs/reference/hook-events.md`.
- Enums — `docs/reference/enums.md`.
- Skill output envelope — `docs/architecture/envelope.md`.
- State entities — `docs/architecture/state-model.md`.
- Workflow lifecycle — `docs/architecture/workflow.md`.
