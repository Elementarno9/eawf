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
<!-- BEGIN EAWF:managed id=python-style version=1.0 hash=7a4e251ee4503a66 -->
## Python style (python profile)

- f-strings only; no ``%``-style or ``.format()``.
- Full type hints; ``from __future__ import annotations`` at the top of
  every module.
- ``uv run`` for all Python invocations — never ``.venv/bin/python``.
- Pre-commit before commit (``uv run pre-commit run --all-files``).

<!-- END EAWF:managed id=python-style -->
<!-- BEGIN EAWF:managed id=research-workflow version=1.0 hash=d1726bdcdd49c5d8 -->
## Research workflow (research profile)

Hypotheses, audits, and decisions are first-class state-resident entities.
Every claim about behaviour, performance, or correctness MUST be backed
by an audit-recorded artifact (notebook, log, dataset). Hypotheses use
the ``H<NN>-<NN>`` symbol; audits link to the hypothesis, the claim, and
the supporting artifact id. Decisions reference the audit that justifies
them so the evidence chain is reconstructible from ``state.json`` alone.

<!-- END EAWF:managed id=research-workflow -->
