---
name: graduate-research-code
description: Model-only playbook for promoting spike/research code into a typed, tested, maintained module.
argument-hint: ""
user-invocable: false
disable-model-invocation: false
---

# graduate-research-code

A model-only playbook for promoting throwaway research/spike code into a
maintained module. There is no slash command; the model invokes this when
a notebook or scratch script has proven its idea and must now meet
production conventions.

## When to reach for it

- A spike under `.ea/local/` (or a notebook) demonstrated a result that a
   real feature now depends on.
- The exploratory code lacks types, tests, and error handling but the
   algorithm is settled.
- A research artifact is being promoted to `.ea/artifacts/` and its code
   needs to move out of scratch.

## Canonical procedure

1. Separate the kept algorithm from the exploratory scaffolding (plotting,
   ad-hoc prints, hard-coded paths). Only the algorithm graduates.
2. Re-home it into the proper package layer with `from __future__ import
   annotations`, full type hints, and module-level `logger`.
3. Replace inline constants and machine paths with parameters or config;
   scrub any PII or local paths before the code is committed.
4. Add the test debt the spike skipped: boundary cases, error paths, and
   `pytest.approx` / `assert_allclose` for any numerics.
5. Back every quantitative claim the graduated code makes with an audit-
   recorded artifact so the verify-before-claim rule holds.

## Guardrails

- A spike brief that ratifies a decision promotes from
   `.ea/local/research/` to `.ea/artifacts/research/` in the same commit
   that lands the decision (spike-workflow rule).
- Do not graduate code whose verdict is still open — promote the brief and
   the decision first.
- The graduated module obeys the same lint, type, and coverage gates as
   any other source file; no exceptions for "it was research".
