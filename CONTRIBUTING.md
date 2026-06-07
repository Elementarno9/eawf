# Contributing

## Development Setup

Clone the repository, install dependencies, and run the CLI from the checkout:

```bash
git clone <repo> && cd eawf
uv sync
uv run eawf --help
```

Install from the local checkout when you need the command available as a uv tool:

```bash
uv tool install --from . eawf
```

## Local Harness Setup

Use one Claude Code setup mode at a time. Running both modes at once makes Claude Code see duplicate skills, agents, and hooks.

Project-local mode renders assets into the working repository:

```bash
eawf plugin install claude
eawf doctor
eawf plugin doctor claude
```

Marketplace mode builds a portable Claude Code plugin package:

```bash
eawf plugin package claude --target ./build/eawf-plugin
```

Then install it in Claude Code:

```text
/plugin marketplace add ./build/eawf-plugin
/plugin install eawf@eawf
```

Codex and opencode render directly into the workspace root:

```bash
eawf plugin install codex
eawf plugin install opencode
eawf plugin doctor codex
eawf plugin doctor opencode
```

`plugin update` is currently Claude-only; codex and opencode update support ships in v0.4.

## Verification

Run the focused check for the area you changed, then run the full repository gates before committing:

```bash
uv run pytest
uv run mypy src/
uv run pre-commit run --all-files
```

## Project Rules

Keep generated runtime assets and user-local files out of commits. The Claude Code plugin tree (`.claude/` plus `CLAUDE.md`) and `.claude/settings.local.json` are machine-specific renders; regenerate them with `eawf plugin install claude` instead of committing them.

Commit `.ea/state.json`, `.ea/profile.yaml`, and `AGENTS.md` when workflow state or agent policy changes. State mutations should go through `eawf` surfaces rather than hand edits.
