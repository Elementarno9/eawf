# Native-path rehearsal in a disposable canary

## Summary

One Milestone was driven through the shipped native surfaces inside a disposable epoch-2 canary, under a runtime directory of its own, with no terminal attached and no provider process started. It did not complete. This directory records how far it got and what stopped it, step by step, plus a census of every epoch-2 record kind naming what writes it and what reads it.

The honest result in one line: **plan apply executes; dispatch, integrate, verify and accept all refuse, and the first step that could not execute is `/dispatch`.**

That is the deliverable. A rehearsal that stubbed the missing halves and reported a green walk would have recorded nothing, because the thing worth knowing about this path is exactly where it stops.

`rehearsal-manifest.json` is the machine record of the walk and the census. `isolation-record.json` is the production-root digest pair and the teardown.

## What the walk did

| # | Step | Result | Code | Driven through |
|---|---|---|---|---|
| 1 | plan apply | executed | — | `planning.plan_revision.submit` / `.approve` / `.apply` |
| 2 | dispatch | refused | `run_request_uncompilable` | `/dispatch` |
| 3 | integrate | refused | `integration_request_unnamed` | `/integrate apply` |
| 4 | verify | refused | `schema_validation_failed` | `/verify --mode all` |
| 5 | accept | refused | `transition_guard_failed` | `domain.milestone.open_review` |

**Plan apply is real.** Submit, approve and apply each committed, at canonical sequences 1, 2 and 3, and the apply materialised the Milestone, one Batch and one `PLANNED` Task. Both records then rendered through their own read verbs. This is the only step of the five that moved the tree.

**Dispatch stops before a provider.** `/dispatch` read the Batch and its one `PLANNED` Task and returned `needs_operator`, stopping on `dependency_proof_unreadable` and `run_request_uncompilable`: the read models carry no dependency edge, and nothing compiles the provider documents, binding set, authority capsule and rendered prompt a Run request names. No Run was opened, so no provider process was ever started.

**Integrate never assembles a request.** The apply branch named the six fields it would have had to invent — `base`, `branch`, `subject`, `subjects`, `exit_refs`, `diagnostic_ref` — and sent nothing. The select branch found no read model rendering a Batch's sealed candidate set. Only `show` executed.

**Verify reached its verb and was refused by it.** This is the one refusal that is not a missing half. See F-01 below.

**Accept is two guards away.** `domain.milestone.accept` from `PLANNED` is an illegal transition, so the Milestone was activated first; `domain.milestone.open_review` then refused on the guard `required_batches_completed`, because the Batch never completed — because the integrate step never executed. The Milestone therefore reached `ACTIVE` and stopped there.

One thing happened before the walk began. The plan binds a Track and a repository row, and no shipped verb admits either into an epoch-2 tree, so both were written into the canary's generation document directly. That is recorded as the walk's precondition rather than as a step, under `epoch2_record_admission_absent`.

## The record no producer writes

`pending_action`.

The acceptance verb is handed the reference of a sealed `PendingAction` receipt, and reads it from the tree rather than trusting what it was given. Nothing in epoch 2 writes a `PendingAction`.

So even a Milestone whose Batches had all completed could not be accepted: the approval it is taken against is a record with no producer. The projection layer already knows this — `UNWRITTEN_COLLECTIONS` names `pending_action` — which is what makes it a gap rather than a surprise.

## The census

One row per epoch-2 collection, forty in all. `written_by` names the registered daemon verbs that write it; `read_by` names the `projection.<route>.read` verbs that render it. An empty list is a real answer and the common one.

| Shape | Count | Records |
|---|---|---|
| a native verb writes it and a console route reads it | 7 | `artifact`, `batch`, `milestone`, `receipt`, `run`, `task`, `track` |
| a native verb writes it and nothing reads it | 1 | `plan_revision` |
| a console route reads it and nothing writes it | 7 | `campaign`, `claim`, `evidence`, `health_view`, `pending_action`, `release`, `sandbox_policy` |
| only the epoch-1 cutover projects it, and nothing reads it | 6 | `actual`, `audit`, `estimate`, `legacy`, `memory`, `track_outcome` |
| nothing writes it and nothing reads it | 19 | `campaign_finding`, `capability`, `current`, `decision`, `dispatch_paused`, `draft`, `event`, `fleet_run`, `hypothesis`, `incident`, `indexes`, `lease`, `open_question`, `permission`, `project`, `repository`, `telemetry`, `tool_authority`, `workspace` |

The `read_by` column is not asserted, it is derived: the suite recomputes it from the route-to-collection table and fails if the recorded census disagrees. The `written_by` column is curated from the write sites and is checked for registration only, so a verb that is renamed reds the suite and a verb that stops writing does not.

## Findings

**F-01 — the lifecycle skills cannot name themselves as a principal.** `/verify` and `/dispatch` pass `actor=self.name` into the `actor` parameter of `runtime.delivery.verify_batch` and `runtime.run.retry`. That parameter is a principal key, matching `^[A-Z][A-Z0-9-]{1,31}$`, which `/verify` and `/dispatch` do not. Both calls are therefore refused at the parameter boundary whatever the tree holds.

The rehearsal separated the skill from the verb. Called directly with an operator principal, the verification verb accepted the request shape and refused on the Batch having selected no integration generation, and the retry verb refused on the absent Run. Both verbs work; neither skill can reach them.

**F-02 — six producerless registers are not declared as such.** The census derives `campaign`, `claim`, `evidence`, `health_view`, `release` and `sandbox_policy` as collections a console route reads and nothing writes. `UNWRITTEN_COLLECTIONS` names `pending_action` alone. A console that draws those six as registers that were read and found quiet is making exactly the claim that declaration exists to prevent.

**F-03 — a Batch with no delivery renders as a conflict.** The `merge.conflict` route is bound to the `batch` collection, so its projection renders Batch rows rather than the conflict lines filed under them. `/integrate show` over the freshly applied Batch reported one conflict frame and returned `conflicted` on a blocked envelope, for a Batch that had received no delivery and held no conflict.

**F-04 and F-05** are the two already-known gaps the walk re-confirmed: nothing writes a `PendingAction`, and nothing admits a Track or repository row.

## Production-root isolation

`isolation-record.json` records one digest over the tracked `.ea` tree of the wave's own worktree, taken before the canary was provisioned and again after it was torn down, over 141 files. The two are equal.

The canary was registered in a scratch registry file rather than the operator's own, which was never addressed and holds no row under the canary's code. Teardown removed the canary's registry row, its runtime directory and its tree; the registry it was registered in holds no rows at all afterwards.

## References

| # | Reference |
|---|---|
| 1 | `.ea/artifacts/evidence/2026-09-18-canary-rehearsal/rehearsal-manifest.json` |
| 2 | `.ea/artifacts/evidence/2026-09-18-canary-rehearsal/isolation-record.json` |
| 3 | `tests/integration/workflow/release/test_dev3_canary_rehearsal_record.py` (reads 1) |
| 4 | `tests/integration/workflow/release/test_dev3_canary_isolation.py` (reads 2) |
| 5 | `src/eawf/platform/install/canary.py` (provisioning and teardown) |
| 6 | `src/eawf/workflow/skills/dispatch.py`, `integrate.py`, `verify.py` (the three lifecycle skills) |
| 7 | `src/eawf/kernel/projection/compute.py` (the route-to-collection table the census derives from) |
| 8 | `src/eawf/kernel/projection/registers.py` (the unwritten-register declaration F-02 is about) |

## Provenance

Produced on 2026-09-18 inside the rehearsal wave's own worktree. The canary was provisioned by `eawf init --epoch2-canary provision` against a fresh directory and a scratch registry, served by a daemon bound to the runtime directory the provisioning allocated, and torn down by `eawf init --epoch2-canary teardown`.

The three lifecycle skills were executed headless through the skill engine over the production daemon transport, so every call in the walk crossed the same JSON-RPC seam a console invocation would. The planning verbs and the Milestone verbs were called over that same transport.

No provider process was started at any point, and none would have been: the walk stops at `/dispatch`, which is the surface that would have opened one.

## Scrub

No absolute path, machine name, account identifier, credential or vendor session id appears in this directory. The canary root, its runtime directory and the scratch registry were allocated under a temporary directory and removed; none of their locations is recorded. The recorded digests are over file contents and repository-relative paths only, and the operator's own repository registry is described by three yes-or-no fields rather than by its contents.
