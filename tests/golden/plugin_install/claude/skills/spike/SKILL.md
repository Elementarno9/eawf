---
name: spike
description: "Read-only multi-axis direction investigation that unblocks /roadmap propose or /design: N rounds x M axis picks, optional postmortem + scope deltas. No state mutations."
argument-hint: "<spike-slug> [--final] [--from-briefs <path1,path2,...>] [--postmortem <phase-id>] [--rounds=<n>] [--axes-per-round=<m>] [--worktree]"
user-invocable: true
disable-model-invocation: false
---

# /spike

## Purpose

A *spike* is a time-boxed read-only investigation that produces **direction** — picks across many design axes — to unblock the next planning skill. Direction-only means no wave plan, no success criteria, and no EU estimates fall out of a spike; those belong to `/design` and `/roadmap propose`. Spike writes only under `.ea/local/`.

Three failure modes it prevents:

1. Single-verdict context loss. `/research` answers one question; a rebuild or phase-opening decision has many entangled axes. Spike runs them as multi-round AUQ batches in one session with a rolling matrix.
2. Silent scope creep. Mid-session picks can quietly expand or reduce surface vs prior briefs. Spike surfaces scope deltas as a mandatory section when prior briefs exist.
3. Postmortem-without-next-plan. Spike couples a postmortem (gap matrix + root causes + salvage matrix) to direction picks in the same brief so the rebuild plan inherits the lessons.

## When to invoke

Reach for `/spike` when multiple decisions block `/roadmap propose` and must be picked together to stay coherent, when a prior attempt shipped but missed its briefs and the next phase is a rebuild, or when direction picks span scope-expansions and -reductions that need explicit acknowledgement. Pick a neighbour instead when one unknown blocks progress and a single verdict lands it (`/research`), or when direction is locked and an interactive surface needs a full statechart
+ matrix + journey design (`/design`).

## Canonical algorithm

1. Resolve the slug from the argument or AUQ. Filename stem: `<YYYY-MM-DD>-<slug>.md`.
2. Frame the multi-axis unknown in one paragraph; list the prior briefs feeding the spike (`--from-briefs`). On `--postmortem <phase-id>`, declare the phase under postmortem.
3. Survey: read the cited prior briefs, read source on the verdict ladder (verify-before-claim), run `git log` for shipped-vs-spec drift. Optionally dispatch worktree subagents for independent investigative arms; the parent compiles the chunks.
4. Multi-round AUQ picks. Each round = 3-6 axes batched into one `AskUserQuestion` call. Decisions accumulate into the rolling matrix; round-close is gated on all axes in the batch answered.
5. Scope-delta surfacing. When picks diverge from prior briefs, record explicit expansion and reduction tables, each row citing the original brief line and the new pick.
6. Critical-contracts capture. When picks ripple into process changes (commit-prefix lint, success-criteria shape, schema migration, audit kind), surface them with enforcement + effect named.
7. Open follow-ups. Enumerate explicitly — "none" is rare. Label each with a next-action (next-spike, next-research, hypothesis-open, blocked-on-EU, blocked-on-demo).
8. Hand-off declaration. The Summary closes with a literal `next:` line naming the unblocked skill and its args.
9. Self-lint, then write (on `--final`) `.ea/local/research/<YYYY-MM-DD>-<slug>.md` with the sentinel `<!-- eawf-template: spike-brief -->` on line 1. Return the output envelope.

## Options

Spike is model-driven, so these are prose parameters the session honours, not engine-parsed flags:

- `--rounds <n>` — number of multi-axis AUQ rounds; parameterizes the default "3-6 axes per round" shape. Default is model-judged.
- `--axes-per-round <m>` — axes batched into each `AskUserQuestion` round. Default `3-6`.
- `--worktree` — make the execution-spike isolation branch explicit and mandatory: when passed, code that imports `eawf.*` runs in a dedicated worktree branch rather than the local PoC scope.
- `--final` / `--from-briefs <paths>` / `--postmortem <phase-id>` — see the canonical algorithm above.

## PoC allowance + execution-spike isolation

A spike MAY produce throwaway runnable artifacts to ground direction picks — smoke demos, probe scripts, config experiments — under `.ea/local/` only (`.ea/local/smoke/<slug>/`, `.ea/local/poc/<slug>/`, or `.ea/local/research/notes/<slug>/`), gitignored, with a manifest table + a "How to run the PoCs" section when >=1 is built.

A *direction spike* is read-only. An *execution spike* writes code to ground a verdict against a running artifact, so it needs isolation the read-only flow does not. Zero-internal-dep throwaway scripts stay in the local gitignored PoC scope. Code that imports `eawf.*` runs in a dedicated worktree branch off the current feature-branch HEAD (`feature/<symbol>-vX.Y-spike-<slug>`); on a green verdict the commits are cherry-picked into the feature branch (never merged), and on a rejected verdict the worktree is torn down. Do NOT commit spike code straight onto the shared feature branch — a pre-verdict commit interleaves with concurrent sessions and bakes unratified experiment into history.

## Brief chassis

Required sections (the writer rejects on missing): frontmatter (scope URN, `status=local-draft`, created date, agent); the sentinel on line 1; Summary (verdict rollup + direction bullets + the `next:` line); a decision matrix (>=1 round table when picks were made, else a Findings section); Open follow-ups (labelled); References (dense `[N]` rows, repo-relative + external URLs + Eä URNs); Provenance (`store_record=none (local-only spike)`, starting commit SHA, session slug); and Scrub (repo-relative paths only, no PII, placeholder names when project codes appear). Conditionally required: the PoC manifest + run guide when >=1 PoC was built, the postmortem arc when `--postmortem` is passed, and the scope-delta tables when `--from-briefs` cites prior briefs.

## Pre-flight checklist

- [ ] No state mutations; `state.json` untouched. PoCs under `.ea/local/{smoke,poc,research/notes}/` only (gitignored).
- [ ] If an execution spike that landed runnable code — code is isolated (worktree branch for internal deps, local PoC scope for zero deps) and ships a `.ea/local/` user test guide.
- [ ] Multi-round picks via `AskUserQuestion`, never free-text; recommended option first, labelled.
- [ ] Every direction pick carries a one-line rationale (`[N]` cite or "because X").
- [ ] Scope deltas surfaced when `--from-briefs` cited; critical contracts surfaced when picks ripple into process change.
- [ ] Open follow-ups enumerated with next-action labels; `next:` line present in the Summary.
- [ ] References dense `[N]`, repo-relative only; Provenance records the starting commit SHA + session slug; Scrub confirms no PII.
- [ ] Brief filename `<YYYY-MM-DD>-<slug>.md` under `.ea/local/research/`; sentinel `<!-- eawf-template: spike-brief -->` on line 1.

## Decision surfaces

Round axis picks, scope-delta acknowledgement, and the hand-off declaration are all surfaced through `AskUserQuestion` batches — the hand-off AUQ options name the unblocked skill (`/roadmap propose`, another `/spike`, `/research --final`, or `/design`).

## Output contract

Eä-rendered envelope (`OutputEnvelope`) with `header.skill = "/spike"`. Body carries the Summary + `next:` line, the round tables (decision matrix), the postmortem arc when `--postmortem`, the scope deltas when `--from-briefs` cited, the critical contracts when applicable, the labelled open follow-ups, and the References + Provenance + Scrub chassis tail. The footer records the persisted brief path + sentinel when `--final` was passed.
