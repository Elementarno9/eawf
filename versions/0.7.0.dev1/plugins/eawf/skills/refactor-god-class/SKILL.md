---
name: refactor-god-class
description: "Model-only playbook for splitting a multi-responsibility class into single-purpose collaborators."
argument-hint: ""
user-invocable: false
disable-model-invocation: false
---

# refactor-god-class

A model-only refactoring playbook. There is no slash command and no CLI verb; the model invokes this while editing a module whose single class has accreted too many responsibilities.

## When to reach for it

- One class owns parsing, validation, persistence, and presentation.
- The class exceeds ~300 lines or has more than ~7 public methods that cluster into distinct concerns.
- Tests for the class need elaborate setup because one method depends on state another method mutates.

## Canonical procedure

1. Map responsibilities. List each public method and tag it with the one concern it serves (parse, validate, compute, persist, render).
2. Find the seams. Group methods sharing the same tag and the same private state; each group is a candidate collaborator.
3. Extract the lowest-coupling group first into its own class with a constructor that takes only the state it needs (no back-reference to the god class).
4. Replace the in-class call sites with delegation to the new collaborator; keep the god class's public API stable for one step so callers do not churn.
5. Repeat per concern. When the god class is a thin coordinator, decide whether it survives as a facade or dissolves into its callers.

## Guardrails

- One concern per extraction commit; never move two seams at once.
- Preserve behaviour: run the existing tests after each extraction before moving the next group.
- Honour the project conventions — `extra="forbid"` on any new Pydantic model, single-responsibility per the AGENTS.md engineering rules, and no speculative abstraction (YAGNI).
