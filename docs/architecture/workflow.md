# Workflow

*High-automation ADD lifecycle, parallel-wave discipline, and end-to-end DAGs.*

## Default lifecycle

```text
intake
  → research fanout
  → synthesis
  → state-backed spec / plan
  → approval gate if risky / large
  → wave planning
  → wave claims
  → parallel research / execution where safe
  → audit
  → fresh review
  → auto-fix loop when bounded
  → acceptance gate
  → commit / PR proposal or auto-commit if user selected auto-commit during installation
```

Parallel implementation is allowed only when:

- waves are distinct,
- file scopes are disjoint or explicitly coordinated,
- each agent has a wave claim,
- the state write lock is respected,
- acceptance checks are defined.

## Required workflow skills

| Skill | Purpose | Default automation |
|---|---|---|
| `/research [-f] [message]` | Research topic or current-iter unknowns; propose options, tradeoffs, risks, recommendation; peer-review findings; repeated calls extend same brief | Auto fanout, synthesize, review, update state / artifact refs; `-f` saves to configured research folder |
| `/prep [p##[-i##]] [-i]` | Plan current / selected iter; `-i` plans fixes after `/audit` or `/review` | Build DAG, define waves, acceptance checks, file scopes, worktree needs, wave / iter EU estimates |
| `/audit [scope]` | Full iteration audit: metrics / results, code quality, tests, docs / state evidence, integrity checks | Run configured checks, fanout reviewers, record `Audit` artifact / verdict |
| `/ship` | Commit / push / PR open-close controller | Respect auto-commit / auto-push; otherwise create pending-ship artifact and ask; review / promote / prune relevant memories before final state update |
| `/review` | Review active PR and post templated comment / review | Inspect diff / checks, run focused agents, post only when requested or policy allows |
| `/polish [-y]` | Whole-repo consistency audit across docs, memory, code, configs, state, agent / subagent memory | Parallel search, grouped cleanup table; promote useful memories, prune obsolete ones; `-y` auto-applies safe tasks |
| `/init` | LLM-guided Ea setup / enrichment for new / existing repo / workspace / subproject | Inspect context, propose identity / profiles / settings / subprojects, ask approval, run init / update CLI, validate; delegate roadmap to `/roadmap` |
| `/roadmap` | Goal / phase roadmap design for project / subproject | Research context, propose goals / outcomes / phases / iterations / audit gates / estimate envelopes, ask approval, apply state via CLI |
| `/differentiate` | Generate project / subproject-specialized agents from Eä baselines | Ask desired agent options, adapt roles / tools / prompts / scopes / checks, validate with `/agent-lint`, render agents |
| `/flow` | One-click resumable ADD iteration pipeline | Wraps the 6 core skills with budget gates and `flow.jsonl` checkpoints |

Optional skills (per profile / config): `/incident`, `/state`,
`/reconcile`, `/memory`, `/handoff`, `/agent-lint`, `/worktree`,
`/trace`.

## Skill algorithms (summary)

Every skill performs `Probe instruments → Resolve scope → Action →
Envelope`. Each step writes evidence; failures degrade per the pipeline
fallback ladder rather than fake completion.

### `/research [-f] [message]`

1. Probe instruments via `EA_INSTRUMENT_PROBE`; abort if hard
   requirement missing.
2. Resolve scope: explicit message, else active iter unknowns / blockers
   from state.
3. Detect continuation: if same scope has open research brief, load and
   extend.
4. Define questions: facts to verify, options to compare, risks to
   audit, decision needed.
5. Dispatch parallel read-only agents: repo / context search,
   external / source research, prior-art search, adversarial review.
6. Synthesize options: 2–4 solutions with tradeoffs, complexity,
   reversibility, dependencies, risks.
7. Review findings: cross-check citations, identify stale assumptions,
   contradictions, hallucination risk, missing data.
8. Recommend one path with confidence and fallback.
9. Write / update brief artifact if `-f` or `research.auto_save=true`;
   otherwise keep session output and optional state summary.
10. Record artifact / decision candidates / unresolved questions in
    state when policy allows.

### `/prep [p##[-i##]] [-i]`

1. Probe instruments.
2. Resolve planning mode: current iter by default; explicit phase / iter
   if supplied; `-i` means fix-plan for latest audit / review findings.
3. Load state, accepted research, decisions, backlog, memory, current
   code / docs, acceptance config.
4. Define objective and non-goals.
5. Build task DAG: task ID, deps, file scope, commands, evidence, risk,
   expected artifact.
6. Partition into waves: parallel only for disjoint / controlled scopes;
   assign worktree policy.
7. Estimate each wave and roll up the iter budget; do not recalibrate
   coefficients during the run.
8. Allocate IDs: `eawf iter open P13` auto-allocates next `P13-Ixx`;
   explicit `P13-I04` infers parent.
9. Write plan / spec artifact, state wave stubs, and estimate records.
10. Ask approval if `approval=ask`, risky, destructive, ambiguous, or
    budget exceeds threshold.

### `/audit [scope]`

1. Probe instruments.
2. Resolve scope: active iter by default; may target wave / phase / PR.
3. **Branch on profile composition**:
   - `research` profile enabled → `/audit --kind=evaluation` runs
     MLflow integrity (lookahead bias, MZ tautology, OOS overlap, IS /
     OOS gap), measures outcomes, sets hypothesis verdicts, writes
     evaluation artifact.
   - `research` profile not enabled → `/audit --kind=ship-gate` runs
     tests, lint, typecheck, build, security, docs links, scope-vs-spec
     drift, writes ship-gate artifact.
4. Build check plan from `.ea/acceptance.yaml`, profile rules, changed
   files, outcomes, hypotheses.
5. Run deterministic checks per the chosen audit kind.
6. Collect result metrics and compare to thresholds / baselines.
7. Dispatch fresh reviewers for code quality, docs / state consistency,
   memory drift, domain integrity.
8. Mark each finding: blocker, fix-now, follow-up, false-positive.
9. If `--fix-safe`, apply bounded safe fixes and rerun affected checks.
10. Write `Audit` artifact with commands, outputs, metrics, review
    findings, verdicts, estimate / actual telemetry, evidence IDs.
11. Update outcomes / hypotheses only from audit evidence; update
    actuals only from measured session / runtime data.

### `/ship`

1. Probe instruments.
2. Require current audit passed or explicit allowed exception.
3. Inspect git status / diff / log and state scope.
4. Review memory: extract durable lessons from session / agent memory,
   promote useful entries, mark stale / contradicted entries, prune only
   by policy.
5. Build pending-ship artifact: commit groups, messages, files,
   evidence, push / PR action, rollback notes.
6. Default new-install policy is ask before commit; if auto-commit is
   explicitly enabled and `--no-commit` is not set, commit using
   selected template.
7. Default new-install policy is ask before push; if auto-push is
   explicitly enabled and `--no-push` is not set, push safely.
8. PR action: open draft / ready, update body, close / merge only if
   configured gates pass.
9. Merge / close gates: CI green, required reviews, state valid, no
   unresolved blockers; force may bypass outcomes only with reason,
   never CI.
10. Record commits / PR / merge / audit artifacts and final
    estimate-vs-actual summary in state; close wave / iter / phase as
    requested.
11. Remove clean worktrees if policy says; preserve on conflict /
    failure.

### `/review`

1. Probe instruments.
2. Resolve PR from explicit flag or active branch.
3. Fetch PR metadata: base / head, commits, changed files, checks,
   comments, requested reviewers.
4. Review correct diff with merge-base / triple-dot semantics.
5. Dispatch focused agents by area / risk.
6. Check PR template completeness, state links, audit evidence,
   memory / docs drift, tests.
7. Produce findings table and recommendation: approve, comment, request
   changes, or fix locally.
8. If `--post`, publish templated comment / review; otherwise output
   draft.
9. If `--fix`, route through `/prep -i` or apply safe fixes then
   `/audit`.

### `/polish [-y]`

1. Probe instruments.
2. Snapshot repo / state; do not mutate before report.
3. Fan out read-only agents over code, tests, docs, configs, state,
   generated files, project memory, agent / subagent memory.
4. Find inconsistencies: stale docs, duplicate rules, broken links,
   orphan artifacts, invalid memories, obsolete generated files, naming
   drift.
5. Reconcile / merge findings into grouped cleanup tables by topic /
   scope / risk.
6. Memory pass: promote useful session / agent facts, compact long
   memories, mark stale / superseded, propose prune list.
7. Without `-y`, ask which groups to run.
8. With `-y`, apply safe groups only; unsafe / destructive tasks still
   ask.
9. Run affected checks and write polish report artifact.
10. State updates record decisions / backlog / memory changes; deletion
    requires recoverability and explicit reason.

### `/flow [goal] [budgets] [policy]`

`/flow` wraps the 6 core skills as a resumable controller. Algorithm:

1. **Start / Resume**: create or resume flow record in
   `.ea/stores/flow.jsonl` with goal, budgets, current pointers, stop
   conditions, policy, last safe checkpoint, and next action.
2. **Research loop**: run `/research` repeatedly within `time_budget`,
   `research_budget`, `agent_budget`, and `cost_budget` until unknowns
   are resolved, recommendation confidence reaches threshold, or
   marginal value stops improving. Use parallel read-only agents;
   `/research` **auto-invokes `/reconcile`** when subagent verdicts
   disagree. If `research_budget` is exhausted while confidence remains
   low, the loop does not silently continue: tag
   `flow.research_status=inconclusive` and ask the user explicitly with
   `extend budget | proceed-with-caveat | stop`.
3. **Plan**: run `/prep` to produce iteration plan, DAG, waves, checks,
   worktrees, acceptance criteria, risk register, and approval prompt.
4. **Approval gate**: ask with concrete options: approve, edit scope,
   research more, defer, stop.
5. **Execute**: dispatch waves; use worktrees for parallel writers;
   record wave claims and checkpoints before mutations; agents may ask
   only for major decisions / blockers. **Worktree teardown happens
   after each wave's close, before the next wave-group starts**, per
   `worktrees.merge_mode` in `.ea/config.yaml`.
6. **Audit**: run `/audit` (branches on `research` profile per the
   `/audit` algorithm); if major issues found, open fix loop with
   `/prep -i`, execute bounded fixes, and re-audit until pass, budget
   exhausted, or user chooses stop / backlog.
7. **Minor / stale gate**: ask the user fix-now, backlog, ignore with
   reason, or stop.
8. **Memory review**: run `/polish --memory-only`; promotion proposals
   not applied here are deferred to the next session, not silently
   dropped. The user can opt to skip via `--no-memory-review`.
9. **Ship**: run `/ship`. If `--ship=auto` is configured **and** the
   `research` profile is enabled, `/flow` force-degrades to
   `--ship=ask` because evaluation-kind audits warrant a human gate
   before publish.
10. **Close / resume marker**: record final state, pending follow-ups,
    memory promotions, stale markers, handoff, and safe resume command.

`/flow` is appropriate when scope is well-defined upfront and policy
gates are calibrated. The explicit sequence
(`/research → /prep → execute → /audit → /ship`) is appropriate when
verdicts may surprise mid-iter or scope shifts during execution. Mixed
use is fine: an explicit-skill detour during a `/flow` run does not
abort the flow; `/flow --resume` picks up from the last safe checkpoint.

## Worktree usage rules

Use worktrees when:

- two or more write-capable agents run in parallel,
- task is risky / large and should not dirty root repo,
- emergency fix interrupts messy root state,
- review / audit needs to test PR merge ref or alternate branch,
- generated artifacts may be noisy and isolated cleanup is useful.

Avoid worktrees when:

- task is read-only research,
- edit is single-file / trivial,
- repository uses submodules heavily and project has not opted in,
- file scopes overlap without an explicit merge owner.

Contract:

1. Create from current root branch:
   `git worktree add .worktrees/<wave-branch> -b <wave-branch>`.
2. Never create detached worktrees for commit-producing waves.
3. Add `.worktrees/` to `.gitignore` unless repo policy says otherwise.
4. Each worktree has one owning wave / session and file-scope claim.
5. Agent commits inside worktree only after checks / evidence pass or
   after pending-ship approval.
6. Merge back per `worktrees.merge_mode`. Default `cherry_pick`.
7. On conflict, preserve worktree, record blocker; do not force-remove.
8. **Worktree teardown follows wave close, before the next wave-group
   starts** — never tear down with uncommitted or uncherry-picked work,
   and never leave a worktree alive across wave-group boundaries.
9. Remove only clean worktrees with `git worktree remove`; use `prune`
   only for stale metadata.
10. Worktree policy and branch naming live in `.ea/config.yaml`.

### Merge mode

```yaml
worktrees:
  merge_mode: cherry_pick   # cherry_pick | rebase_then_ff
```

| Property | `cherry_pick` | `rebase_then_ff` |
|---|---|---|
| New SHA in target | yes | no |
| Linear log | yes | yes |
| Conflict resolution venue | target branch | source branch (rebase) |
| Source branch identity in `--contains` | lost | preserved |
| Step count | 1 | 2 |
| Best for | ephemeral worktree, throwaway branch | remote-tracked source, SHA-referenced commits |

Default `cherry_pick` is correct for most Eä waves: the worktree branch
is ephemeral, conflict resolution belongs in the parent feature branch
where reviewers will read it, and a rewritten SHA is acceptable because
the source branch is torn down.

## VCS, commit, PR, and merge policy

Recommended commit variants:

1. **Conventional Commits**: `<type>[optional scope]: <description>`.
2. **State-scoped ADD**: `[P##[-I##[-W##]]] <type>: <summary>` plus body
   bullets and evidence trailers. Default for state-first projects.
3. **Minimal solo**: `<type>: <summary>` plus `Refs:` / `Evidence:`
   trailers.
4. **Release / phase**: `release: <phase or version>` with outcome
   summary and PR / release evidence.

Required PR templates:

- **Iter PR**: Summary, Changes, Evidence, Test plan, Risks, State
  links, Follow-ups.
- **Phase / release PR**: Outcomes, Audits, Unmet outcomes / force
  reason, Migration notes, Rollback, Checklist.
- **Docs / research PR**: Claims changed, Sources, Review findings,
  Open questions.
- **Incident fix PR**: Root cause, Fix, Regression guard, Prevention,
  Verification.

Merge / rebase rules live in `.ea/config.yaml` under `vcs:` and
`worktrees:`. Force push is forbidden on protected branches; squash is
opt-in; `delete_branch_after_merge` is opt-in.

## Pipeline fallback rules

Every skill follows the same fallback ladder:

1. **Retry cheap deterministic failure once** after refreshing state /
   config.
2. **Classify failure**: missing context, missing tool, failing check,
   merge conflict, auth / secret, ambiguous user intent, external
   service, schema / state corruption, destructive-risk gate.
3. **Write evidence**: append event / log / pending artifact with
   command, error, scope, attempted fixes.
4. **Degrade mode** when safe: full TUI → static Rich → plain text;
   web + repo research → repo-only; auto-fix → report-only; worktree
   merge → preserve worktree and blocker.
5. **Ask user** only when blocked by missing secret / auth, destructive
   choice, repeated failed attempts, ambiguous product decision, or
   conflict requiring semantic judgment.
6. **Never fake completion**. Leave state as `blocked`, `failed`,
   `pending_ack`, or `needs_user` with next valid commands.

## End-to-end DAGs

### `/init` pipeline DAG

```text
/init [project|subproject] [flags]
│
├─ [Detect environment]  (read-only)
│   ├─ [Probe instruments]  (read-only, cached)
│   │   ├─ hard tools present → continue
│   │   └─ hard tool missing → abort + install hint
│   ├─ git root present?
│   │   ├─ yes → continue
│   │   └─ no → {asks: init git repo? Y/N}
│   ├─ existing .ea/?
│   │   ├─ yes + complete → enrich mode (preserve, ask before changing)
│   │   ├─ yes + partial → repair mode
│   │   └─ no → fresh init
│   ├─ existing AGENTS.md / CLAUDE.md?
│   │   ├─ has EAWF managed regions → safe to re-render
│   │   ├─ no markers + non-empty → wrap content as user-region on first generation
│   │   └─ absent → generate fresh
│   └─ language detection (pyproject.toml, package.json, ...) → propose profile set
│
├─ [Mode selection]  {asks: new | enrich | add-subproject | roadmap-handoff | repair}
│
├─ [State placement]  {asks once}
├─ [Project identity]  {asks once for code+title; auto-fills others}
├─ [Lifecycle depth]  {asks once if subprojects detected}
├─ [Subprojects]  {asks once if depth=multi-workstream}
├─ [Profile selection]  {asks once}
├─ [Roadmap delegation]  {asks once: do you want goals/phases now? Y/N}
├─ [Plugin install]  {asks per-runtime}
├─ [MCP selection]  {asks per-MCP recommended by profiles}
├─ [Acceptance commands]  (auto-detect, then asks confirm)
│
├─ [Plan + apply]  {asks ONCE for full file-change confirmation}
│   ├─ show: list of files to create/modify/skip
│   ├─ ask: apply this plan? Y/N/edit
│   └─ on Y:
│       ├─ write .ea/config.yaml + state.json + schema.json + acceptance.yaml
│       ├─ render AGENTS.md (after profile compose)
│       ├─ render CLAUDE.md = "@AGENTS.md"
│       ├─ render .claude/skills, agents, hooks (Eä-managed regions)
│       ├─ append .gitignore: .ea/local/**, .ea/cache/**, .ea/tmp/**, .ea/secrets/**
│       └─ register repo in workspace .ea/state.json if linked
│
└─ [Validate]  (always last)
    ├─ eawf validate --strict
    │   ├─ pass → continue
    │   └─ fail → keep files, mark needs_setup, print repair hints, exit 4
    └─ eawf doctor
```

Required write ordering inside `[Plan + apply]`:

1. `.ea/config.yaml` — profile composition result baked in.
2. `.ea/state.json` — minimal core only; optional keys materialized per
   composed profiles.
3. `.ea/schema.json` + `.ea/acceptance.yaml`.
4. **Profile compose pass** — resolve `requires` graph, deep-merge
   rules / agents / hooks / MCPs, persist conflict decisions.
5. `AGENTS.md` — rendered from composed profile result, never from raw
   selected list.
6. `CLAUDE.md` — hardcoded `@AGENTS.md\n` shim.
7. `.claude/skills/`, `.claude/agents/`, `.claude/hooks/` — rendered
   from composed profile result.
8. `.gitignore` append.
9. Workspace registration if linked.

Profile composition MUST complete before AGENTS.md or any plugin / skill
/ agent file is rendered.

### `/flow` pipeline DAG

```text
/flow "goal" [budgets] [--ship ask|auto|none] [--stop-after research|prep|audit|ship] [--resume]
│
├─ [Start/Resume]  flow record exists in .ea/stores/flow.jsonl?
│   ├─ yes → resume from last_safe_checkpoint (reconcile drift)
│   └─ no → create flow record {goal, budgets, current_pointers, policy}
│
├─ [Research loop]  ← may iterate
│   ├─ /research within remaining research_budget
│   ├─ peer-review red-team if research profile enabled
│   ├─ confidence ≥ threshold OR plateau OR budget exhausted?
│   │   ├─ confidence high → exit loop
│   │   ├─ budget exhausted + confidence low → {asks: extend / proceed-with-caveat / stop}
│   │   └─ subagents conflict → /reconcile (auto-invoked) → loop
│   └─ persist research artifact + decisions
│
├─ if --stop-after=research → mark paused, exit
│
├─ [Prep]  /prep → DAG, waves, file scopes, acceptance, EU estimates
│   └─ {asks: approve | edit-scope | research-more | defer | stop}
│
├─ if --stop-after=prep → mark paused, exit
│
├─ [Execute]  ← per wave-group, parallel-disjoint
│   for each wave-group:
│   ├─ for each wave (parallel): claim, optional worktree, dispatch executor,
│   │   post-commit hook → state validate, wave close, merge per merge_mode
│   ├─ after group: write last_safe_checkpoint
│   └─ on failure: critical → halt; non-critical → continue, surface in audit
│
├─ if --stop-after=execute → mark paused, exit
│
├─ [Audit]  branches on profile per /audit algorithm
│   ├─ research profile enabled → /audit --kind=evaluation
│   └─ otherwise → /audit --kind=ship-gate
│   ├─ verdict: pass → continue; major → /prep -i fix loop; minor → ask
│   └─ persist verdicts
│
├─ if --stop-after=audit → mark paused, exit
│
├─ [Memory review]  /polish --memory-only → promote / prune per policy
│
├─ [Ship]  pending-ship artifact built; ship policy applied
│   ├─ --ship=auto AND research profile enabled → force-degrade to ask
│   └─ --ship=ask → {asks: approve commit + PR | commit-only | defer | stop}
│
└─ [Close]  flow.status ∈ {done, blocked, abandoned, superseded}
```

Resume after kill: `/flow --resume` continues from
`flow.last_safe_checkpoint`. Drift detection on resume; long actions
write pending records before mutation and recovery records after.

## Cross-references

- Skill envelope schema — `docs/architecture/envelope.md`.
- Profile composition rules — `docs/architecture/profiles.md`.
- Hook events — `docs/reference/hook-events.md`.
- State entities and lifecycle invariants — `docs/architecture/state-model.md`.
