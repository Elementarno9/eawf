# Quickstart

*Bootstrap a repository, inspect health, and start the first Eä workflow.*

This page is the shortest path from an ordinary Git repository to an
Eä-managed project. It uses the canonical `eawf` command name and keeps
examples non-interactive so they are safe to paste into CI or a local shell.

For the full command inventory, see the [CLI surface](../architecture/cli-surface.md)
and the generated [CLI reference](../reference/autogen/cli.md).

## 1. Install or run the CLI

For a one-off bootstrap, run the package without installing a persistent
command:

```bash
uvx eawf init --target . --project-code DEMO --project-title "Demo Project"
```

For regular use, install the tool and then run `eawf` directly:

```bash
uv tool install eawf
eawf --version
```

`eawf` is the stable binary name. The shorter `ea` alias is optional and is
only installed when the local environment has no command collision.

## 2. Initialize a repository

Run initialization from the repository root:

```bash
eawf init --target . --project-code DEMO --project-title "Demo Project" --profiles core,python
```

Initialization writes project state and generated runtime files. The important
outputs are:

- `.ea/state.json` — committed project ledger.
- `.ea/config.yaml` — selected profiles, runtime adapters, acceptance gates,
  and layered project configuration.
- `AGENTS.md` — generated agent contract for the repository.
- `CLAUDE.md` — shim that points Claude Code at `AGENTS.md` when that adapter
  is enabled.

Local caches, secrets, and scratch files stay under ignored `.ea/local/`,
`.ea/cache/`, `.ea/tmp/`, and `.ea/secrets/` paths.

## 3. Check project health

After initialization, run validation and the doctor:

```bash
eawf validate --strict .ea/state.json
eawf doctor
```

`validate` checks the state document and invariants. `doctor` checks tool
availability, config health, and install readiness. If either command reports
a blocker, fix that before planning work.

## 4. Plan a phase

Eä tracks work as a state-backed lifecycle:

```text
phase -> iter -> wave -> audit -> ship
```

Plan the next phase with the roadmap surface:

```bash
eawf roadmap propose --phase P01 --title "Add first tracked workflow"
eawf roadmap show --phase P01 --md
eawf roadmap apply P01
```

The proposed phase starts in `PLANNED` state. Use `roadmap revise` to add,
remove, or retitle pending waves before applying the plan.

## 5. Execute the next wave

Wave execution is usually driven by `/prep` and the runtime adapter. The state
CLI still exposes the underlying lifecycle operations:

```bash
eawf wave next-ready
eawf wave claim P01-I01-W01 --session operator-demo
```

Worktree-capable runs create per-wave branches and merge them back by
cherry-pick, preserving the long-running phase branch as the integration point.
Each wave should close only after its scoped files, checks, and evidence are
complete.

## 6. Audit and ship

Before committing phase closeout, run the configured checks:

```bash
eawf validate --strict .ea/state.json
eawf doctor
```

The normal endgame is:

1. Run `/audit` for the active iter.
2. Run `/polish` for consistency follow-ups.
3. Run `/ship` to prepare commit groups, PR text, and final state updates.
4. Close the iter and phase in the final state-bookkeeping commit once checks
   and PR review are green.

## Next reads

- [Concepts](../concepts.md) for the nouns used by the workflow.
- [Workflow](../architecture/workflow.md) for the full lifecycle.
- [Installation](../architecture/installation.md) for project, workspace, and
  global setup details.
