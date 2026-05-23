---
name: extract-function
description: Model-only refactoring playbook for pulling a coherent block out of a long function into a named helper.
argument-hint: ""
user-invocable: false
disable-model-invocation: false
---

# extract-function

A model-only refactoring playbook. There is no slash command; the model
invokes this to pull a coherent block out of a long function into a named
helper.

## When to reach for it

- A function spans more than one screen and reads as several phases.
- A comment introduces a block ("# now normalise the rows") — the comment
   is begging to become a function name.
- The same computation appears in two places (DRY).

## Canonical procedure

1. Identify the block and the minimal set of variables it reads (inputs)
   and the single value it produces (output). If it produces two, that is
   two extractions.
2. Name the helper for WHAT it returns, not how — `normalised_rows`, not
   `do_step_two`.
3. Lift the block into a pure function where viable: explicit args in,
   explicit value out, no hidden state mutation.
4. Replace the original block with a call; keep the surrounding function's
   shape so the diff is reviewable.
5. Give the new helper full type hints, a docstring with a `Raises:` block
   if it can raise, and boundary + error-path tests if it is public.

## Guardrails

- Stop and reconsider if the helper needs four or more parameters — that
   often signals a missing value object, not a missing function.
- Three similar lines are fine; do not extract a one-line helper used once
   (YAGNI / KISS).
- Use named arguments once arity reaches three (explicit-over-implicit).
