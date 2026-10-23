# Workflow

*Agent-driven delivery lifecycle, parallel-Task discipline, and the Milestone pipeline.*

## Default lifecycle

```text
intake
  → research (/research, /spike, /campaign)
  → backlog: draft Tasks (/backlog)
  → PlanRevision: propose, approve, apply (/plan)
  → Milestone activation (/milestone activate)
  → dispatch: claim Tasks, start Runs, reach a candidate per Batch (/dispatch)
  → verification at one exact revision (/verify)
  → integration: seal, select, apply (/integrate)
  → Milestone acceptance (/accept)
  → release (/release)
```

The containment the lifecycle walks is Track, Milestone, Batch, Task; each agent's execution episode is a Run. A Milestone ships through one pull request, and each Batch lands as its own buildable commit on that pull request.

Parallel implementation is allowed only when:

- the Tasks are distinct and each is `PLANNED` and claimed by the actor that runs it,
- their write claims are disjoint, or one of them is marked exclusive and the others wait,
- the daemon's state lock is respected (every mutation goes through it),
- each Task carries typed success criteria with a gate that runs locally.

## Workflow skills

| Skill | Purpose |
|---|---|
| `/research` | Answer one question with a swift one-page investigation; no Campaign. |
| `/spike` | Build, test, independently verify and present a local proof of concept. |
| `/campaign` | Run a complete Campaign from definition through terminal synthesis. |
| `/backlog` | Add or list draft Tasks and propose their promotion through a plan. |
| `/track` | Create, show or retire a Track. |
| `/milestone` | Define, show, activate or cancel a Milestone. |
| `/plan` | Propose, approve and apply a PlanRevision. |
| `/dispatch` | Coordinate one Delivery Batch: bring its ready Tasks to a candidate. |
| `/verify` | Verify one Delivery Batch at one exact revision, as auditor or as reviewer. |
| `/integrate` | Prepare or execute one daemon-owned integration action on a Delivery Batch. |
| `/accept` | Decide a Milestone acceptance bundle: prepare, accept, reject or request repair. |
| `/release` | Prove, preflight, approve, publish, observe and recover a release. |
| `/attend` | Work the Attention queue: pending actions, open questions and open pauses. |
| `/decide` | Propose, supersede or show a Decision. |
| `/why` | Explain the provenance of an entity; read-only. |
| `/memory` | Search, write, promote or show memory entries. |
| `/test` | Design, add, repair or run a bounded test contract. |
| `/refactor` | Inspect or apply a bounded structural refactor. |
| `/mockup` | Build and compare operator-visible design options. |
| `/reflect` | Report where effort, time and money actually went, without mutating canonical state. |

The generated [skill catalog](../reference/autogen/skills.md) carries each skill's argument hint, its lanes and its operator-only actions.

## Stage algorithms (summary)

Every skill performs `Probe instruments → Resolve scope → Action → Envelope`. Each step writes evidence; failures degrade per the pipeline fallback ladder rather than fake completion.

### Plan

1. Gather the inputs: accepted research, decisions, draft Tasks, memory, current code and docs.
2. Propose a PlanRevision: one Milestone, its Batches and their Tasks, each Task with its intent, criteria, write claims and dependencies (`eawf plan submit` records it as `VALIDATED`).
3. The revision binds the state revision, the repository heads and the policy revision it was written over, and the digest of its own body.
4. A human principal approves the digest (`eawf plan approve`); an agent cannot approve.
5. The apply materialises the approved revision into its Milestone (`eawf plan apply`) and refuses when the body no longer matches the approved digest or a bound head has moved.

A `PLANNED` Milestone is re-planned in place through a child revision; an `ACTIVE` one only gains appended Batches or edits to `PLANNED` ones.

### Dispatch

1. Resolve the Batch and its ready frontier: `PLANNED` Tasks whose dependencies are met.
2. Claim each Task for the actor that will run it (`eawf task claim`); a claim is a lease that `eawf task release` hands back.
3. Start the Task under a Run (`eawf task start`); a parallel writer runs in its own worktree.
4. The Run files its report and evidence; `eawf task submit` records that its leased workspace is ready to integrate.
5. Stop at the requested boundary: an empty frontier, a candidate ready, or an item on the Attention queue.

### Verify

1. Bind the exact revision under review.
2. Run the declared gates (`--mode gates`), an audit, a review or a security pass.
3. Classify each finding by severity and record the verdict against that revision; a later commit needs a fresh verification.

### Integrate

1. Seal each Task's accepted report to its candidate (`eawf task seal`).
2. Select the candidates and integrate them into the Batch's next generation (`eawf batch integrate`).
3. Declare the Batch ready to merge, authorise the merge at the head it pinned, and reconcile what the host landed (`eawf batch ready`, `merge`, `observe-merge`, `reconcile`, `complete`).

### Accept and release

1. Prepare the Milestone's acceptance bundle and walk its acceptance journey.
2. The operator accepts, rejects or requests repair (`/accept`); a repair appends Batches to the Milestone.
3. A Release pins accepted Milestones, passes its preflight, is approved and published (`/release`).

## Worktree usage rules

Use worktrees when:

- two or more write-capable agents run in parallel,
- a Task is risky or large and should not dirty the root checkout,
- an emergency fix interrupts messy root state,
- a review or audit needs to test a PR merge ref or an alternate branch,
- generated artifacts may be noisy and isolated cleanup is useful.

Avoid worktrees when:

- the Task is read-only research,
- the edit is single-file or trivial,
- the repository uses submodules heavily and the project has not opted in,
- write claims overlap without an explicit merge owner.

Contract:

1. Branch each worktree from the Batch's branch head, never from the default branch.
2. Never create detached worktrees for commit-producing Tasks.
3. Keep worktrees under an ignored path (`.ea/worktrees/` in this repository).
4. Each worktree has one owning Task and Run, bounded by the Task's write claims.
5. Commit inside the worktree only after its checks and evidence pass.
6. Bring commits back per `worktrees.merge_mode`; the default is `cherry_pick`, in order, never a merge.
7. On conflict, preserve the worktree and record the blocker; do not force-remove.
8. Remove a worktree only after its commits have landed, and never with uncommitted or uncherry-picked work.
9. Remove only clean worktrees with `git worktree remove`; use `prune` only for stale metadata.
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

Default `cherry_pick` is correct for most Eä Tasks: the worktree branch is ephemeral, conflict resolution belongs on the Batch branch where reviewers will read it, and a rewritten SHA is acceptable because the source branch is torn down. A squash that replaces a bound commit is followed by `eawf batch rebind`, so every ledger row naming the old commit is superseded rather than edited.

## VCS, commit, PR, and merge policy

- A Batch commit reads `<type>: <summary>` with one `Batch: BAT-<NNNN>` trailer and one `Task: <KEY>` trailer per Task it delivers; a single-Task commit may carry its `Task:` trailer alone.
- Each push ends with one `state: <summary>` commit touching only committed state paths.
- Before a push, the branch is squashed to one buildable commit per Batch (`eawf batch squash`), code with its tests.
- A Milestone pull request carries a Summary of three to six bullets and a Test plan checklist; review is addressed by appending Batches, and the merge keeps each Batch commit.

Merge / rebase rules live in `.ea/config.yaml` under `vcs:` and `worktrees:`. Force push is forbidden on protected branches, and main and tags are never rewritten.

## Pipeline fallback rules

Every skill follows the same fallback ladder:

1. **Retry cheap deterministic failure once** after refreshing state / config.
2. **Classify failure**: missing context, missing tool, failing check, merge conflict, auth / secret, ambiguous user intent, external service, schema / state corruption, destructive-risk gate.
3. **Write evidence**: append event / log / pending artifact with command, error, scope, attempted fixes.
4. **Degrade mode** when safe: full TUI → static Rich → plain text; web + repo research → repo-only; auto-fix → report-only; worktree merge → preserve worktree and blocker.
5. **Ask the operator** through a pending action or an open question only when blocked by a missing secret or auth, a destructive choice, repeated failed attempts, an ambiguous product decision, or a conflict requiring semantic judgment; an unattended run records the question and continues on the independent remainder.
6. **Never fake completion**. A Run that cannot finish ends `FAILED` or `SUSPENDED` with its reason, and a report carries a `fail` or `blocked` verdict with the next valid commands.

## Cross-references

- Skill envelope schema — `docs/architecture/envelope.md`.
- Profile composition rules — `docs/architecture/profiles.md`.
- Hook events — `docs/reference/hook-events.md`.
- State entities and lifecycle invariants — `docs/architecture/state-model.md`.
