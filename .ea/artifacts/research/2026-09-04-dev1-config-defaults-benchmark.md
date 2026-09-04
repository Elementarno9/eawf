# Dev1 configuration defaults: four measured, seven carried unmeasured

> Status: benchmark artifact · Scope: the eleven numeric defaults of the v0.7 configuration contract · Method: measurement over repository state, agent-session records, CI release-run history, and one instrumented probe of the shipped heartbeat ticker · `verified_at_commit`: `2f523398` · Date: 2026-09-04

## Summary

The v0.7 configuration contract fixes eleven numeric defaults and holds each one as a *proposal* default until it is remeasured, with a recorded benchmark required before any of them changes [7]. Four of the eleven are read by a dev1 gate and are measured here; the other seven are read by no dev1 gate and are recorded as unmeasured at their proposal value rather than carried as though somebody had checked them.

All four measured defaults hold. The release-train timeout of 1800 seconds sits above a measured p90 of 1123.0 seconds over 46 publication runs and above the slowest run observed, 1190.0 seconds [2][3][4]. The retry limit of 2 is the tightest value that would not have burned a release: of those same 46 runs, 41 finished on the first attempt, 3 needed a second and 2 needed a third, so a limit of 1 would have failed two publications outright. The runtime wall default of 2400 seconds is an order of magnitude above the p90 agent-session wall of 208.6 seconds [1]. The lost-grace default of 30 seconds is 2.0 refresh intervals of the shipped lock heartbeat, whose observed cadence has a p90 gap of 15.007 seconds [5], and it is half the 60-second staleness threshold the same runtime already enforces [6], so it tolerates exactly one missed beat.

The measured and unmeasured sets partition the eleven exactly. That partition is not left to review: the loader rejects any parse whose two sets are not disjoint and do not cover the contract keys [8], and the gate exercises both the real artifact and the rejection paths [9].

## Measured defaults

Sample size, p50 and p90 for the four defaults a dev1 gate reads. Timeout figures are wall seconds per run; the retry figure is a count of attempts; the grace figure is seconds between successive heartbeat writes.

| Configuration key | Proposal default | Source | Sample size | p50 | p90 | Verdict |
|---|---|---|---|---|---|---|
| `release.target_timeout_seconds` | 1800 | wall seconds per publication run across the three release-train workflows [2][3][4] | 46 | 45.5 | 1123.0 | holds; p90 is 62 percent of the default and the slowest run seen, 1190.0 s, is 66 percent of it |
| `release.target_retry_limit` | 2 | attempts needed per publication run across the same 46 runs [2][3][4] | 46 | 1.0 | 2.0 | holds exactly; 41 runs took 1 attempt, 3 took 2 and 2 took 3, so a limit of 1 would have burned two releases |
| `runtime.default_wall_seconds` | 2400 | wall seconds of closed agent sessions recorded in repository state [1] | 12 | 40.2 | 208.6 | holds with wide headroom; p90 is 8.7 percent of the default and the longest session ran 625.3 s |
| `runtime.lost_grace_seconds` | 30 | seconds between successive heartbeat writes of the shipped lock ticker under an instrumented hold [5] | 12 | 15.005 | 15.007 | holds; 30 s is 2.0 observed refresh intervals, one missed beat, against the 60 s staleness threshold already enforced [6] |

Three caveats belong with these numbers rather than under them. The timeout sample is whole-run wall time, which is a wider quantity than the publication step it bounds, so it is a conservative upper bound rather than a tight one. The wall sample counts only the 12 agent sessions recorded as closed; 46 of the 68 session rows are stale, and their end timestamp records a daemon restart orphaning the session rather than the run ending, so including them would measure the restart and not the work [1]. The grace sample is a probe of the shipped ticker at its default cadence, not history, because no heartbeat history is retained; the cadence it measures is the constant the grace has to clear [5].

## Unmeasured defaults

The seven defaults no dev1 gate reads, carried at their proposal value. Each is a number the contract still asserts; none is a number this benchmark checked.

| Configuration key | Proposal default | Status | Reason |
|---|---|---|---|
| `planning.max_revision_attempts` | 3 | unmeasured | bounds plan revision loops; no dev1 gate reads it and no revision-loop history is recorded to measure |
| `runtime.global_capacity` | 8 | unmeasured | bounds concurrent dispatch; no dev1 gate reads it and dispatch concurrency is not exercised at dev1 |
| `runtime.per_provider_capacity` | 4 | unmeasured | per-provider slice of the same concurrency bound; no dev1 gate reads it |
| `runtime.max_task_attempts` | 3 | unmeasured | bounds task retries; no dev1 gate reads it and no task-attempt history is recorded |
| `delivery.max_repair_tasks` | 2 | unmeasured | bounds repair tasks per integration boundary; no dev1 gate reads it |
| `delivery.max_audit_cycles` | 2 | unmeasured | bounds audit cycles per integration boundary; no dev1 gate reads it |
| `activity.query_page_size` | 100 | unmeasured | pages the activity query surface; no dev1 gate reads it and the surface carries no measured query load |

## Packet annotation

The configuration contract preamble in packet file 00 currently reads that exact numeric defaults remain proposal defaults until remeasured at dev1 and that changing them requires an amendment and a recorded benchmark [7]. The amendment this benchmark obliges is one added sentence, offered here verbatim so the packet edit is a transcription rather than a judgement:

> Four of these defaults are read by a dev1 gate and were remeasured against this artifact: `release.target_timeout_seconds`, `release.target_retry_limit`, `runtime.default_wall_seconds` and `runtime.lost_grace_seconds`, each holding at its proposal value. The remaining seven are read by no dev1 gate and stay unmeasured proposal defaults; the obligation to remeasure them travels to the checkpoint whose gates first read them.

The packet lives outside this wave's file scopes, so applying that sentence is a separate edit; the sentence is fixed here so that edit carries no discretion.

## Method

Release figures come from the run history of the three workflows that publish a release target: the tag-and-notes workflow [2], the package publication workflow [3] and the plugin publication workflow [4]. Every run each workflow has ever recorded was taken, with no filtering by conclusion, so failed and cancelled publications are in the sample; duration is the interval between a run starting and its final update, and attempts is the run's final attempt number. Wall figures come from the agent-session rows of repository state, restricted to sessions carrying both a start and an end timestamp and a closed status [1]. Grace figures come from holding the shipped lock for 185 seconds at the default refresh cadence and recording the interval between successive heartbeat writes [5]. The p50 is the median and the p90 the nearest-rank value throughout.

## References

[1] `.ea/state.json` — the `agent_sessions` rows: 68 sessions, 12 closed with both timestamps, 46 stale, 10 active.

[2] `.github/workflows/phase-release.yaml` — the tag-and-notes publication workflow; 12 recorded runs.

[3] `.github/workflows/release.yaml` — the package publication workflow; 18 recorded runs.

[4] `.github/workflows/plugin-release.yaml` — the plugin publication workflow; 16 recorded runs.

[5] `src/eawf/runtime/lock/portalock.py` — the heartbeat ticker and its 15-second default refresh cadence, the subject of the grace probe.

[6] `src/eawf/runtime/lock/stale.py` — the 60-second staleness threshold the runtime already enforces against that cadence.

[7] `.ea/local/research/2026-08-04-v07-proposal/00-authority-scope-and-decisions.md` — the configuration contract carrying the eleven numeric proposal defaults and the remeasurement obligation.

[8] `src/eawf/platform/artifacts/config_defaults_benchmark.py` — the loader that parses these two tables into typed rows and enforces the partition.

[9] `tests/unit/platform/artifacts/test_dev1_config_defaults_benchmark.py` — the gate over this artifact and over the loader's rejection paths.

## Provenance

- kind: research
- slug: 2026-09-04-dev1-config-defaults-benchmark
- wave: P31-I01-W40
- `verified_at_commit`: `2f523398`
- method: CI run history over three release workflows, repository-state agent-session records, and one instrumented probe of the shipped heartbeat ticker; no state mutation was performed by this wave
- p50 is the median; p90 is the nearest-rank value
- gates: the two selections named in reference 9

## Scrub

- status: clean
- references: repo-relative only
- credentials, PII, machine-specific paths, hostnames: none
