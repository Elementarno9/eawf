---
name: review
description: Code review of an open PR or local diff. Surfaces issues with severity tags; no scope creep, no praise.
argument-hint: "[<PR# | commit-range>]"
user-invocable: true
disable-model-invocation: false
---

# /review

## Canonical algorithm

1. Resolve target: PR number → `gh pr diff <PR>`; commit range → `git diff <range>`; default → `git diff main...HEAD`.
2. Walk the diff hunk by hunk. For each hunk, read enough surrounding context to make a judgment.
3. Apply rules in order: correctness > security > clarity > style.
4. Tag findings: 🔴 blocker, 🟠 must-fix, 🟡 should-fix, 🔵 nit.
5. Check artifact chassis and dense references when reviewing docs or promoted artifacts.

## Pre-flight checklist

- [ ] Read the success criteria for the phase/wave the diff belongs to.
- [ ] Verify any quantitative claim against `Read`/`grep`.
- [ ] Verify markdown artifacts keep `Summary`, `References`, `Provenance`, and `Scrub` sections.

## Decision surfaces

When the final verdict is ambiguous (e.g. one 🟠 finding the operator might choose to defer), surface `approve | request-changes | comment-only` through `AskUserQuestion` rather than picking silently.

## Output contract

Skill envelope with a flat findings list grouped by file and an aggregate verdict (`approve | request-changes | comment-only`).
