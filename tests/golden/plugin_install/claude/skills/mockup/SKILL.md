---
name: mockup
description: Author 2-4 UI mockups as ASCII layouts and surface them as side-by-side AskUserQuestion option previews to compare.
argument-hint: "<surface-slug>"
user-invocable: true
disable-model-invocation: false
---

# /mockup

## Canonical algorithm

1. Take the UI surface under design and the operator's intent (the fields to show, the terminal width budget, the priority elements).
2. Author 2-4 concrete mockup variants as ASCII layouts. Use plain ASCII box characters (`+`, `-`, `|`) so the render stays lint-clean; do not use Unicode box-drawing glyphs.
3. Surface the variants as `UserQuestionOption.preview` strings on a single `UserQuestion` so the operator compares the layouts side-by-side and picks one. Each option's `preview` carries that variant's full multi-line layout; the `label` is the variant name.
4. Stop at the mockup. `/mockup` is advisory: it produces layouts and the comparison prompt, it does not write files or mutate state.

## Pre-flight checklist

- [ ] Read-only / advisory -- no state mutations, no file writes.
- [ ] 2-4 variants only (the `AskUserQuestion` cap; single-select).
- [ ] ASCII-only box art so the preview renders cleanly everywhere.

## Decision surfaces

The variant choice is the one operator-facing decision. Surface it through `AskUserQuestion`, one option per variant, with the layout in each option's `preview` box -- never bounce the operator to free-text.

## Output contract

Skill envelope whose body conforms to `MockupBody`: the authored `variants` plus the optional `UserQuestion` whose options carry the side-by-side `preview` layouts.
