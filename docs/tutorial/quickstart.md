# Quickstart

*Turn an ordinary Git repository into an Eä-managed project in three commands.*

This page assumes `eawf --version` already exits 0. If it does not, start at [Install](install.md). When the three commands below have run, continue with [First workflow](first-workflow.md).

## The three commands

Run these from the root of the repository you want to manage. Each one exits `0` on a clean bootstrap, and a test replays this exact block in a fresh temporary repository so the page cannot drift away from the CLI:

<!-- eawf:quickstart -->

```bash
eawf init --quick
eawf phase open --auto --title "Bootstrap the first tracked delivery"
eawf status
```

## What each command does

`eawf init --quick` is the non-interactive bootstrap. It detects profiles from the files already in the repository, infers a project code from the directory name, writes the managed block into `.gitignore`, and renders the runtime plugin tree. Drop `--quick` for the interactive wizard, or use the scripted form when you want to pin every answer yourself:

```bash
eawf --no-input init --project-code DEMO --project-title "Demo Project" --profiles core,python
```

The scripted form is the one to paste into CI: `--no-input` fails closed instead of prompting, and it requires an explicit `--project-code`.

`eawf phase open --auto` allocates the next free `P<NN>` and makes it the current phase. Titles pass a clarity gate, so an imperative noun-phrase like the one above is accepted while a bare word like `Bootstrap` is not.

`eawf status` reads the ledger back and prints the current position. A zero exit here is the proof that the bootstrap landed.

## What initialization writes

- `.ea/state.json` — the committed project ledger, and the only source of truth for project state.
- `.ea/config.yaml` — enabled profiles, runtime adapters, acceptance gates, and layered project configuration.
- `AGENTS.md` — the generated agent contract for the repository.
- `CLAUDE.md` — a shim pointing Claude Code at `AGENTS.md`, written when that adapter is enabled.

Caches, scratch files, and secrets stay under the ignored `.ea/local/`, `.ea/cache/`, `.ea/tmp/`, and `.ea/secrets/` paths. Commit `.ea/state.json` and `.ea/config.yaml`; leave the rest ignored.

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
