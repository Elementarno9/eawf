# Fixed policy decisions

*Index of v0.1 invariants that downstream design / implementation may not relitigate without an ADR.*

These decisions are **fixed** for v0.1. Changing one is a `[CORE]`
schema bump on the long-running phase branch and requires a recorded
decision (`eawf decision add`).

## Distribution and runtime

- v0.1 is a private production-hard local release; do not publish to
  PyPI or npm.
- npm wrapper is deferred entirely in v0.1.
- Python 3.14+ is the v0.1 runtime floor; `uv` is the primary dev /
  package runner with pip-compatible package metadata.

## Naming

- Repo, Python package, import module, and canonical command are
  `eawf`; display name is Eä.
- `ea` is the only optional alias. If `ea` collides, omit it and
  render all persistent runtime commands with `eawf`.
- `src/eawf` is the source package path.

## Schemas

- `schema_version` is the string `"1.0"` for current config / state /
  store schemas.
- Full strict Pydantic schemas (with `extra="forbid"`) and full command
  I/O matrix are required before implementation.
- Lifecycle IDs use two-digit padding by default (`P01`, `P01-I01`,
  `P01-I01-W01`).

## `.ea/` and `.claude/`

- `.ea/` is committed when the repo does not gitignore it.
- `.claude/` follows repo `.gitignore`; Eä must not assume it is
  committed.

## Defaults

- New installs default to **ask** before commit, push, PR, plugin
  install, MCP install, destructive action, or unmanaged overwrite.

## State placement

- `eawf init` inside a repo defaults to repo-owned `.ea/state.json`;
  if a workspace exists, workspace `.ea/state.json` indexes / links
  repo state.
- Workspace links to repo states; repos do not link back by default.
- Workspace detection uses `.ea/state.json` with
  `scope_kind: workspace`; no `workspace.yaml` marker exists.

## Stores

- All nonlocal `.ea/stores/*.jsonl` are committed by default when
  project policy allows; scratch / local stores and large noisy blobs
  remain gitignored.
- Secret / privacy scan always runs before ship / checkpoint commits
  involving `.ea/stores/*.jsonl`; findings block by default.
- `events.jsonl` is audit-log only, not source of truth or a replay
  guarantee.
- `memory.jsonl` is authoritative for memory; markdown memory files
  are views.

## Lock and write integrity

- `portalocker` is the v0.1 cross-platform lock backend, using sibling
  lockfiles.
- All store and state writes acquire lock + read-modify-write + atomic
  rename + release.

## UI

- Textual is the default TTY interactive installer / config / dashboard
  UI; non-TTY command paths remain scriptable
  (`--json` / `--plain` / `--no-input`).

## Generated assets

- Generated assets use both inline managed-region markers and
  `.ea/indexes/generated.json` (sidecar manifest).
- `eawf sync` remains user-facing and updates Eä-managed generated
  assets from config / state / templates.

## Runtime adapters

- Claude Code user + project installs are the only v0.1 harness
  targets; OpenCode and other harnesses are deferred.

## Profiles

- Core, Python, and Research are functional v0.1 profiles.
- Quant and ML ship as catalog stubs in v0.1 (functional in v0.2 once
  the audit-check runner exists).
- Catalog stubs `re`, `game`, `apps`, `infra`, `docs`, `robotics` ship
  with no body in v0.1.

## Migration

- Non-Eä migration automation is deferred. v0.1 may detect existing
  workflow artifacts during init but must not import, rewrite, disable,
  or clean them automatically.

## Cross-references

- State entities — `docs/architecture/state-model.md`.
- AGENTS.md / CLAUDE.md policy — `docs/policy/agents-claude-md.md`.
- No-PLAN.md policy — `docs/policy/no-plan-md.md`.
- CLI surface and global flags — `docs/architecture/cli-surface.md`.
- Project / global install — `docs/architecture/installation.md`.
