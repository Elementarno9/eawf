---
name: extract-module
description: "Model-only refactoring playbook for splitting a multi-concern file into layered modules."
argument-hint: ""
user-invocable: false
disable-model-invocation: false
---

# extract-module

A model-only refactoring playbook. There is no slash command; the model invokes this when a single file has grown to host several unrelated concerns that deserve their own module.

## When to reach for it

- One file mixes layers — schema models, business logic, and CLI glue — that the architecture keeps separate elsewhere.
- The file's imports fan out across many subpackages, signalling it does too much.
- Two clusters of functions never call each other and share no state.

## Canonical procedure

1. Draw the dependency graph inside the file: which functions call which, which share module-level state. Disjoint clusters are extraction candidates.
2. Choose the cluster with the fewest inbound edges from the rest of the file — it moves with the least churn.
3. Create the new module in the layer it belongs to (schema, logic, CLI) per the CLI-is-dispatch / separation-of-concerns rules.
4. Move the cluster, then fix imports; keep public names stable so callers outside the file change only their import path.
5. Re-export from the original module only if a published API depended on the old location; otherwise update call sites directly.

## Guardrails

- Respect the layering: CLI imports logic, logic imports schema, never the reverse.
- Avoid circular imports — if the split would create a cycle, the seam is wrong; find a different cluster.
- Each module keeps `from __future__ import annotations` and its own `logger = logging.getLogger(__name__)`.
