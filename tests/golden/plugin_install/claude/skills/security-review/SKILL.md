---
name: security-review
description: Run the security-audit DSL against a closed scope and emit findings.
argument-hint: "--spec=<path> [--cwd=<dir>]"
user-invocable: true
disable-model-invocation: false
---

# /security-review

## Canonical algorithm

1. Load the caller-supplied audit spec (a YAML file of declarative
   checks) via the audit-check DSL; a missing/unreadable `spec_path`
   degrades to `status=needs_user`.
2. Dispatch every check through the DSL runner against the target `cwd`
   (defaults to the process working tree).
3. Fold the pass/fail tally into the body. The terminal status is `ok`
   when every check passes and `failed` when any check fails (failing
   check names surface as repair commands).

When the active profile is `security`, this skill is a required gate
for `phase close`.

## Pre-flight checklist

- [ ] `spec_path` points at a readable declarative audit spec.
- [ ] The scope under review is closed.

## Decision surfaces

A missing or unreadable `spec_path` degrades to `status=needs_user`,
which routes the operator to an `AskUserQuestion` prompt for the audit
spec rather than running an empty check set.

## Output contract

Skill envelope with `header.skill = "/security-review"`. Body carries
scope_id, spec_path, checks_run, and the per-check findings.
