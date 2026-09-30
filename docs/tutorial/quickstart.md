# Quickstart

*Turn an ordinary Git repository into an Eä-managed project in two commands.*

This page assumes `eawf --version` already exits 0. If it does not, start at [Install](install.md). When the two commands below have run, continue with [First workflow](first-workflow.md).

## The two commands

Run these from the root of the repository you want to manage. Each one exits `0` on a clean bootstrap, and a test replays this exact block in a fresh temporary repository so the page cannot drift away from the CLI:

<!-- eawf:quickstart -->

```bash
eawf init --quick
eawf status
```

## What each command does

`eawf init --quick` is the non-interactive bootstrap. It detects profiles from the files already in the repository, infers a project code from the directory name, writes the managed block into `.gitignore`, and renders the runtime plugin tree. Drop `--quick` for the interactive wizard, or use the scripted form when you want to pin every answer yourself:

```bash
eawf --no-input init --project-code DEMO --project-title "Demo Project" --profiles core,python
```

The scripted form is the one to paste into CI: `--no-input` fails closed instead of prompting, and it requires an explicit `--project-code`.

The tree `eawf init` writes is born at authority epoch 2, the only epoch that takes writes after the flag day: work is planned with the milestone, batch and task verbs, never the retired phase, iter and wave verbs.

`eawf status` reads the ledger back and prints the current position. A zero exit here is the proof that the bootstrap landed.

## What initialization writes

- `.ea/state.json` — the epoch-1 project document, kept readable and frozen.
- `.ea/epoch2-opt-in.json` — the declaration that opts the tree into epoch 2, pinned to a backup of the skeleton init wrote.
- `.ea/generations/` — the epoch-2 generation the tree reads from, its selection pointer and the epoch marker.
- `.ea/config.yaml` — enabled profiles, runtime adapters, acceptance gates, and layered project configuration.
- `AGENTS.md` — the generated agent contract for the repository.
- `CLAUDE.md` — a shim pointing Claude Code at `AGENTS.md`, written when that adapter is enabled.

Caches, scratch files, and secrets stay under the ignored `.ea/local/`, `.ea/cache/`, `.ea/tmp/`, and `.ea/secrets/` paths. Commit `.ea/state.json`, `.ea/config.yaml`, `.ea/epoch2-opt-in.json` and `.ea/generations/`; leave the rest ignored.

## Check the bootstrap

```bash
eawf validate --strict .ea/state.json
eawf doctor
```

`validate` checks the state document against the schema and the lifecycle invariants. `doctor` checks the environment around it. Fix anything either one reports as a blocker before planning work.

## Next

- [First workflow](first-workflow.md) — plan, execute, and close one unit of tracked work.
- [Concepts](../concepts.md) — the nouns the workflow uses.
- [Profile picker](profile-picker.md) — choosing the profile set for a repository.
- [Troubleshooting](troubleshooting.md) — recovering from a non-zero exit.
