---
name: differentiate
description: Recommend the cheapest experiment that discriminates between two or more candidate paths.
argument-hint: "<candidate-id>"
user-invocable: true
disable-model-invocation: false
---

# /differentiate

## Canonical algorithm

1. Take a candidate path and the alternatives under consideration.
2. Identify the discriminating signal (`what would change my mind?`).
3. Recommend the cheapest experiment that produces the signal.

## Pre-flight checklist

- [ ] Read-only — no state mutations.

## Output contract

Skill envelope listing the discriminating experiments and the recommended next move.
