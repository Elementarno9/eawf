# eawf — Eä Workflow

Agent-driven development framework. v0.1 in active development.

The full v0.1 spec and implementation plan live as external companion docs that
are not vendored in this repository. Internal architecture references land under
`docs/architecture/` once Phase 4 ships them.

## Install (dev)

```bash
uv sync
```

## Quickstart

```bash
uv run python -m eawf --version    # → 0.1.0.dev0
uv run pytest                      # run the test suite
uv run pre-commit run --all-files  # lint + format + secrets scan
uv run mypy src/                   # type-check
```

## Status

v0.1 in active development. The state engine, lock layer, JSONL store, schema
validation, install wizard, profile composition, render layer, and a typed
skill envelope are landed; the CLI surface tracks `eawf-v0.1-plan.md`.

## License

MIT — see `LICENSE`.
