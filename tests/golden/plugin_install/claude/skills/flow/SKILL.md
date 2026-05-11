---
name: flow
description: Run /research → /prep → /audit → /ship → /review → /polish sequentially; short-circuit on any non-ok status.
argument-hint: "<task-slug> [--auto-accept=<stage>[,<stage>...]]"
user-invocable: true
disable-model-invocation: true
---

# /flow

## Canonical algorithm

1. Run `/research` → `/prep` → `/audit` → `/ship` → `/review` →
   `/polish` sequentially.
2. **Inter-stage gate (default).** After each step returns
   `status=ok`, check `flow.auto_accept.<stage>` (via
   `uv run eawf config get flow.auto_accept.<stage>`). When `false`
   (the default) and the stage was not listed in `--auto-accept`,
   ask the operator via `AskUserQuestion` whether to proceed —
   options: `proceed` / `skip-next` / `stop`. When `true`, advance
   without a prompt.
3. On any non-`ok` status (`blocked`, `needs_user`, `failed`,
   `partial`), short-circuit with the failing step's repair commands.

## Pre-flight checklist

- [ ] All upstream skills are installed.
- [ ] Per-stage `flow.auto_accept` flags reflect the operator's
      intended cadence (review existing values; default is "ask each
      time" for every stage).

## Decision surfaces

`/flow` is a long-running pipeline. Every operator-facing decision
point — inter-stage gates, "abandon vs retry on `failed`",
"merge order on `needs_user`" — MUST be raised through
`AskUserQuestion` so the run stays unstuck without dropping the
operator into free-text. Per-step skills already follow this rule;
the flow merely propagates their `needs_user` envelopes verbatim.

## Output contract

Skill envelope whose body accumulates per-step envelopes plus the
inter-stage gate decisions. Status is `ok` when every step passed
(after any auto-accept or operator confirm), otherwise the first
non-`ok` step's status is propagated.
