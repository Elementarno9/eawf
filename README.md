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

Pre-Phase-1 bootstrap. The CLI is a stub; commands land in Phase 1 and beyond per
`eawf-v0.1-plan.md`.

## License

MIT — see `LICENSE`.
