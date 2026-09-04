# Install

*Get the `eawf` command onto a machine, pick the right extras, and confirm it runs.*

This is the first page of the front door. It ends with a working `eawf --version`; [Quickstart](quickstart.md) turns that into an initialized repository, and [First workflow](first-workflow.md) carries the repository through a complete unit of tracked work.

## Requirements

| Requirement | Value | Why |
| --- | --- | --- |
| Python | 3.14 or newer | the floor declared once as `requires-python` in `pyproject.toml` |
| Package manager | `uv` | every documented invocation is `uv`-driven, and `uv` is the path CI exercises |
| Git | any recent version | the lifecycle is branch- and worktree-shaped, so a repository is the unit of work |

The Python floor is a single source of truth. `requires-python` in `pyproject.toml` sets it, the published wheel carries a matching `Programming Language :: Python` classifier, and the CI matrix pins the same interpreter. A test asserts the three agree, so this table cannot silently rot.

## Supported platforms

| Platform | CI runner | What CI runs there |
| --- | --- | --- |
| Linux | `ubuntu-24.04` | the full test suite on a real host, plus coverage and the pixel-diff oracle |
| macOS | `macos-26` | the full test suite on a real host |
| Windows | `windows-latest` | the named-pipe daemon transport, packaging, and a shared-CLI smoke run |

Linux and macOS are the two full-suite platforms: every test that is not explicitly marked `win32` executes there. Windows is supported for the daemon transport and the common CLI paths — the focused win32 job is the gate — but the full suite is not run on it, so treat Windows as a supported-with-a-narrower-net platform rather than an untested one.

## Install the CLI

For a one-off bootstrap, run the package without leaving a command behind:

```bash
uvx eawf --version
```

For regular use, install the tool:

```bash
uv tool install eawf
eawf --version
```

`eawf` is the stable binary name. A shorter `ea` alias is also installed when the local environment has no command collision.

To work on `eawf` itself, install from a checkout:

```bash
git clone <repo-url> eawf
cd eawf
uv sync
uv run eawf --version
```

## Optional extras

Two extras exist, and neither is needed for ordinary use:

```bash
# Windows only: pulls pywin32, which backs the named-pipe daemon transport.
uv tool install "eawf[windows]"

# Docs site build chain, needed only to run `eawf doc verify --strict` end to end.
uv tool install "eawf[docs]"
```

Without the `docs` extra the reference-page drift check still runs; only the strict site build degrades to a clean skip.

## Confirm the install

```bash
eawf --version
eawf doctor
```

`doctor` inspects tool availability, config health, and install readiness. Resolve anything it reports as a blocker before initializing a repository.

## Next

- [Quickstart](quickstart.md) — bootstrap a repository in three commands.
- [First workflow](first-workflow.md) — take that repository through one complete unit of tracked work.
- [Troubleshooting](troubleshooting.md) — what to do when a command exits non-zero.
- [Installation architecture](../architecture/installation.md) — project, workspace, and global install layers in detail.
