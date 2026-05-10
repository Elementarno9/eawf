# Architecture overview

*Naming, repository layout, and project-local `.ea/` skeleton for Eä v0.1.*

## Naming

Human name: **Eä**. Canonical terminal binary: **`eawf`**. Optional alias
when no command collision exists: **`ea`**.

The umlaut form `Eä` is reserved for human prose surfaces (README,
docs, slides). ASCII `Ea` is required everywhere a string may be parsed,
indexed, grepped, or rendered into a non-Unicode pipeline (PR titles,
commit messages, JSON output, log files, generated AGENTS.md text,
error messages). The console binary stays `eawf` regardless.

| Context                                                                  | Spelling     |
|--------------------------------------------------------------------------|--------------|
| README, docs prose, slides                                               | `Eä`         |
| PR title, commit message, JSON output, log files, AGENTS.md, errors     | `Ea` (ASCII) |
| Console binary, package, import, URN NID                                 | `eawf`       |

Rationale: machine-readable channels MUST stay ASCII because diff tools,
grep, log aggregators, and CI graders treat `ä` as either a multi-byte
token or a normalization-sensitive grapheme.

User-scope config stores `cli.preferred_command` (`eawf` or `ea`) and
`cli.install_aliases`. Generated docs/plugins/hooks/statusline use the
configured preferred command, never hardcoded `ea`. `eawf global install`
detects `ea` collisions; on collision, alias install is skipped.

Recommended install surfaces:

```bash
# Run without installing globally — bootstrap or check repo from clean machine
uvx eawf init

# Install canonical user-scope CLI — persistent `eawf` plus optional `ea` alias
uv tool install eawf
```

## Framework repository structure

The Eä tool repo is a normal CLI/product repo, separate from installed
project `.ea/` folders. Concrete v0.1 layout:

```text
eawf/
  pyproject.toml                 # uv-managed; py3.14
  uv.lock
  README.md
  CHANGELOG.md
  AGENTS.md                      # repo agent contract
  CLAUDE.md                      # @AGENTS.md shim
  .github/workflows/             # CI matrix
  src/eawf/
    cli/                         # typer dispatch + command handlers
    state/                       # Pydantic v2 models, schema, IDs, URN, writer
    store/                       # JSONL store envelope + per-kind models + compaction
    lock/                        # portalocker wrapper, sibling lockfiles, stale recovery
    profiles/                    # core/python/research YAML + composer
    render/                      # managed-region markers, manifest, drift, AGENTS.md, etc.
    runtimes/claude/             # Claude Code adapter (plugin install/update/doctor, statusline)
    skills/                      # /research, /prep, /audit, /ship, /review, /polish, /flow, /init, /roadmap, /differentiate
    install/                     # init wizard, global install, instrument probe
    memory/                      # memory.jsonl store + render-context + promotion
    mcp/                         # catalog + Eä-owned installer
    worktree/                    # create + merge-back + cleanup
    hooks/                       # `eawf hook run` runner
    doctor/                      # install/runtime diagnostics
    validate/                    # strict validation + invariants
    logging/                     # events.jsonl writer
    templates/                   # AGENTS.md.j2, CLAUDE.md.j2, hooks, commits, PRs
    schemas/                     # state.schema.json, config.schema.json, skill-output.schema.json
  tests/
    unit/ integration/ property/ golden/ fixtures/
  docs/                          # this directory
```

Design rules:

- `src/eawf/state/` owns schema and mutations.
- `src/eawf/render/` is pure/idempotent: same config + state produces
  same output.
- `src/eawf/runtimes/*` are adapters, not workflow source of truth.
- `profiles/*.yaml` are declarative and composable.
- Templates contain no secrets and no machine-specific paths.
- The CLAUDE.md shim is a hardcoded emit (`render_claude_shim() ->
  "@AGENTS.md\n"`), not a Jinja2 template.

## Project-local layout

Use **`.ea/`**, not `.add/`. Recommended project layout:

```text
.ea/
  config.yaml              # project Eä config: profiles, runtimes, policies, MCPs/hooks
  state.json               # authoritative project ledger (committed by default)
  schema.json              # pinned state schema version (committed)
  acceptance.yaml          # executable acceptance checks/gates (committed)
  mcp.yaml                 # MCP choices and constraints (committed if no secrets)
  permissions.yaml         # capability policy (committed)
  agents.yaml              # project agent registry (committed)
  hooks.yaml               # enabled hooks and policy (committed)
  stores/                  # JSONL stores
    research.jsonl         # research briefs/events
    audits.jsonl           # audit reports/results
    incidents.jsonl        # incident timelines/root cause records
    estimates.jsonl        # estimate versions + actual segments + recovery events
    memory.jsonl           # authoritative memory entries
    decisions.jsonl        # decision records
    events.jsonl           # append-only audit log
  artifacts/
    blobs/sha256/<hash>    # large payloads, command outputs, rendered files
    rendered/*.md          # optional generated human views (committed when curated)
  indexes/
    artifacts.json         # compact lookup cache, rebuildable
    generated.json         # sidecar manifest for managed regions
  local/                   # gitignored: machine-local generated files
  cache/                   # gitignored
  tmp/                     # gitignored
  secrets/                 # gitignored

.claude/                   # rendered if Claude Code adapter selected
  skills/                  # .claude/skills/**/SKILL.md
  agents/                  # .claude/agents/*.md
  hooks/                   # .claude/hooks/*.{sh,ps1}
  settings.json            # generated managed region only

AGENTS.md                  # generated by eawf — canonical agent contract
CLAUDE.md                  # always only "@AGENTS.md\n" shim
```

Committed by default when `.ea/` is not gitignored:

- `.ea/config.yaml`, `.ea/state.json` (or `.ea/state.ref.json`),
  `.ea/schema.json`, `.ea/acceptance.yaml`,
  `.ea/mcp.yaml` (without secrets), `.ea/permissions.yaml`,
  `.ea/agents.yaml`, `.ea/hooks.yaml`,
  `.ea/stores/*.jsonl` for non-local stores (memory, audits, decisions,
  research, incidents, estimates, events).

Always gitignored:

- `.ea/local/**`, `.ea/cache/**`, `.ea/tmp/**`, `.ea/secrets/**`,
  machine paths, tokens, API keys, auth state.

`.claude/` is not governed by Eä globally. If the repo gitignores
`.claude/`, do not commit it. If the repo tracks `.claude/`, generated
project skills/agents/hooks may be committed. Eä detects `.gitignore`
and follows repository policy.

Rationale: a committed `.ea/` makes workflow reproducible for agents
and other machines. Local secrets and runtime caches stay out of git.
Runtime folders such as `.claude/` remain repo-policy-dependent.

## Cross-references

- State entities and JSONL store kinds — see `docs/architecture/state-model.md`.
- CLI commands and global flags — see `docs/architecture/cli-surface.md`.
- Workflow lifecycle and DAGs — see `docs/architecture/workflow.md`.
- Profile composition — see `docs/architecture/profiles.md`.
- Plugin and adapter model — see `docs/architecture/plugins.md`.
- Memory model — see `docs/architecture/memory.md`.
- Skill output envelope — see `docs/architecture/envelope.md`.
- Statusline integration — see `docs/architecture/statusline.md`.
- Project / global install flows — see `docs/architecture/installation.md`.
