<!-- BEGIN EAWF:managed id=non-negotiable-rules version=1.0 hash=ed0f53e26790524b -->
## Non-negotiable rules (core)

1. CLI is dispatch; library implements.
2. Strict config validation: every YAML/JSON path uses Pydantic v2 with
   ``ConfigDict(extra="forbid")``.
3. State CLI is the only mutator of ``state.json``.
4. f-strings only; full type hints; ``uv run`` for all Python invocations.
5. Pre-commit before every commit; hook failures are root-caused, never
   ``--no-verify``'d.

<!-- END EAWF:managed id=non-negotiable-rules -->
