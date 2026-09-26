# Epoch-2 cutover rehearsed on a pinned full clone of this repository

## Summary

This repository was cut over to epoch 2 on a throwaway clone pinned at `e2d6a5df8b599a30aa08d5306335ac5381a3d0c6`, through the operator verbs the live cut (W13) runs, with a separate home, registry, backup directory and daemon. The plan is applicable, the apply selects, a second apply writes nothing and reports the same manifest, and the rollback puts every restorable surface back at its restore-point digest.

The digest the live cut has to match at this revision:

| Digest | Value |
|---|---|
| manifest | `d3760c43a9f489189e8404a13228165ae4a207d6388307b4402593d260f3f900` |
| approval | `952f79afe93e17e311d45aab26a268f02be693ee66999ea499304509dc7645dc` |
| source | `5ac847491638a463eb013c5187557cf201694e27c6b7ae0ba21887da821b0849` |
| idempotence | `cd53ae583aaf8bf9e18853b1ca67389dca1b804ee2f36cef5afc51c615be3f24` |
| generation | `gen-d3760c43a9f48918` |

These values belong to this revision's committed corpus. Any later state commit changes the source, so W13 re-runs the rehearsal at its own frozen revision and matches that run's manifest digest, not this one. `cutover-rehearsal.json` is the machine record: every command with its exit code, every envelope field quoted here, and the defects.

## What the rehearsal did

| Leg | Result |
|---|---|
| stage | 20 committed sources at the pinned revision; a second staging is byte-identical |
| plan | applicable, 6,228 source rows in 38 collections, 5,908 target rows, 0 unresolved rows, 0 operator assignments, 40 declared Track assignments |
| apply | applied `gen-d3760c43a9f48918`, 12 journal rows from `fence_cleared` to `maintenance_exited`, the fence naming the verified backup |
| re-run | `already-selected`, 0 journal rows, same manifest digest, all 202 target files byte-identical |
| rollback | `surfaces_restored`: state, config, audit and event ledgers back at their restore-point digests; epoch 1, no generation on disk; boundary back to `plan_only` |
| second cycle | re-applied after the rollback: same generation, same manifest digest |

The target rows are 3,245 legacy records, 1,805 Tasks, 614 Runs, 94 decisions, 87 Batches, 39 Milestones, 21 incidents, and one each of project, sandbox policy and Track outcome.

## Defects

**D-01, fixed in this wave: released locks blocked the cutover.** The quiescence probe refused on every `*.lock` file under `.ea/locks/`. The lock primitive keeps its lease file on release and only empties it, so any tree that has taken the worktree-registry or plugin-sync lock carries these files for good. The live repository has two. The probe now passes a zero-byte lease that no process holds, and still refuses a lease with a holder record in it or an empty one that is still locked.

**D-02, open: the generation comes from HEAD, not from the live document.** `--stage-to` reads git objects, and the re-census inside the apply checks only the staged snapshot against the plan, so nothing compares the corpus with the working-tree document.

On the clone, the reconcile retired 37 worktree rows and the daemon's boot sweep closed sessions. The restore point captured those changes; the generation did not. At the live cut, anything the daemon wrote after the last state commit would be dropped without a warning. Until a guard lands, the runbook commits state immediately before staging.

**D-03, open: the refusal's hint is misleading.** A `migration_not_quiescent` refusal suggests running `eawf validate`, which clears no holder.

**D-04, open: the refusal never names the fix.** It lists at most 20 holders and never mentions `eawf worktree reconcile`, the verb that clears the 37 managed-worktree rows the committed document still carries.

## Runbook for W13 (corrected)

On the live repository, in this order:

1. Run `eawf worktree reconcile --dry-run`, then `eawf worktree reconcile`. At this revision, 37 worktree rows were stale. Close the operator session.
2. Commit state (D-02), then keep the tree quiescent. Wait for the commit to land before starting the daemon's next write.
3. Re-run this rehearsal on a clone at the new HEAD and note its manifest digest. `uv run pytest tests/integration/kernel/migration/test_v07_rehearsal.py -k pinned_clone -q` clones HEAD and asserts the four legs.
4. Run `eawf backup create --note 'pre epoch-2 cutover' --json` and keep `ts` and `digest`.
5. Write `.ea/epoch2-opt-in.json` with `opt_in`, `declared_by`, `purpose`, `backup_ts` and `backup_digest`. No `epoch2-disposable-canary.json` may sit alongside it.
6. Run `eawf workspace show EAWF`. If it is missing, run `eawf workspace add EAWF --home EAWF --title "eawf"`.
7. Run `eawf --json migrate epoch2 --stage-to <empty dir outside .ea> --workspace-key EAWF --project-key EAWF`. Both keys are required. Check that `revision` is the frozen HEAD.
8. Run `eawf --json migrate epoch2 --plan --snapshot-root <dir> --allowlist tests/fixtures/migration/allowed_legacy_symbols.txt --workspace-key EAWF --project-key EAWF --repository-key EAWF --default-track-key TRK-EAWF-CORE`. Expect `applicable: true` and 0 unresolved rows. `manifest_digest` must equal the step-3 rehearsal's.
9. Apply: add `--apply --plan-digest <approval digest> --target-root .ea` to the same flags. Expect `applied` and 12 journal rows.
10. Run the same apply again. Expect `already-selected`, 0 journal rows and the same manifest digest.
11. Run `eawf --json migrate epoch2 --rollback-boundary --target-root .ea`. Expect `marker_written`, with both windows `open`.

## References

| # | Reference |
|---|---|
| 1 | `.ea/artifacts/evidence/2026-09-dev4-cutover-rehearsal/cutover-rehearsal.json` |
| 2 | `tests/integration/kernel/migration/test_v07_rehearsal.py` (the `pinned_clone` tests) |
| 3 | `tests/integration/kernel/migration/_live_corpus.py` (clone, quiesce, opt-in and registry helpers) |
| 4 | `src/eawf/kernel/migration/epoch2/quiescence.py` (D-01) |
| 5 | `src/eawf/kernel/migration/epoch2/apply.py` (the re-census D-02 is about) |
| 6 | `src/eawf/runtime/lock/portalock.py` (the release that keeps the lease file) |

## Provenance

Produced on 2026-09-26 by wave P35-I04-W10. The clone was made with `git clone --no-local` and detached at the pinned revision. Every eawf call ran from the wave's worktree source, with `HOME` and `EAWF_RUNTIME_DIR` pointing at scratch directories and `EA_STATE` unset. The clone's daemon spawned into that runtime directory and was stopped, and the scratch tree was deleted afterwards. No eawf command addressed the live repository or the operator's home.

## Scrub

This directory records no absolute path, machine name or account identifier. Scratch locations appear as `<clone>`, `<tmp>` and `<repo>` placeholders. Digests cover file contents and repository-relative paths only.
