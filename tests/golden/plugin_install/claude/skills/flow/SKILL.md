---
name: flow
description: "Run /research → /prep → /audit → /polish → /ship sequentially; review folds into /ship as the PR-review pass. Short-circuit on any non-ok status."
argument-hint: "<task-slug> [--auto-accept=<stage>[,<stage>...]] [--stop-after=<stage>] [--resume] [--args-per-step=<json>] [--caps=eu=..,usd=..] [--max-repair-cycles=<n>]"
user-invocable: true
disable-model-invocation: true
---

# /flow

## Canonical algorithm

1. Run `/research` → `/prep` → `/audit` → `/polish` → `/ship` sequentially. The PR-review pass is folded into `/ship` (it reads the remote review comments, addresses feedback by appending waves to the current iter, then bundles iter + phase close in the final pre-merge commit per the `iter-phase-close-timing` rule in AGENTS.md). The `/prep` stage claims and dispatches waves, so its claim path carries the operator gotchas: Run `uv run eawf dispatch resume` before EVERY claim batch. A shared-daemon test run or TUI mount leaks `dispatch_paused=true` into the live claim path; the pause gate is unconditional. If resume reports success but claims still reject, restart the daemon — the flag can persist in a stale process.
2. **Inter-stage gate (default).** After each step returns `status=ok`, check `flow.auto_accept.<stage>` (via `uv run eawf config get flow.auto_accept.<stage>`). When `false` (the default) and the stage was not listed in `--auto-accept`, ask the operator via `AskUserQuestion` whether to proceed — options: `proceed` / `skip-next` / `stop`. When `true`, advance without a prompt. Between a wave close and the next stage: After EVERY `eawf wave close`, commit the `[P<NN>] state:` bookkeeping (state.json + event store) BEFORE dispatching the next subagent — an inline subagent's checkout can revert uncommitted state, silently dropping the close.
3. On any non-`ok` status (`blocked`, `needs_user`, `failed`, `partial`), short-circuit with the failing step's repair commands.

## Options

- `--auto-accept=<stage>[,<stage>...]` — the inter-stage gate is executed by YOU (the model) against `flow.auto_accept.<stage>`; the flow engine reads only `stop_after` / `args_per_step` / `resume_from` and does NOT enforce auto-accept. Listing a stage advances past its gate without the operator prompt.
- `--stop-after=<stage>` — engine-parsed (`ctx.args["stop_after"]`); halt the pipeline after the named stage. Default none (full run).
- `--resume` / `--resume-from` — engine-parsed (`ctx.args["resume_from"]`); replay from the recorded checkpoint and refuse on drift. Default off.
- `--args-per-step=<json>` — engine-parsed (`ctx.args["args_per_step"]`); per-stage argument overrides. Default inherits the flow args.
- `--caps=eu=<f>,usd=<f>,tokens=<n>` — record a spend ceiling and halt the pipeline when actuals exceed it (honest-empty until EU capture lands); reuses `flow.budget.*`. Default uncapped.
- `--max-repair-cycles=<n>` — stop re-entering a failing stage past N cycles. Default `3`; config leaf `flow.max_repair_cycles`.

## Pre-flight checklist

- [ ] All upstream skills are installed.
- [ ] Per-stage `flow.auto_accept` flags reflect the operator's intended cadence (review existing values; default is "ask each time" for every stage).

## Decision surfaces

`/flow` is a long-running pipeline. Every operator-facing decision point — inter-stage gates, "abandon vs retry on `failed`", "merge order on `needs_user`" — MUST be raised through `AskUserQuestion` so the run stays unstuck without dropping the operator into free-text. Per-step skills already follow this rule; the flow merely propagates their `needs_user` envelopes verbatim.

## Output contract

Skill envelope whose body accumulates per-step envelopes plus the inter-stage gate decisions. Status is `ok` when every step passed (after any auto-accept or operator confirm), otherwise the first non-`ok` step's status is propagated.
