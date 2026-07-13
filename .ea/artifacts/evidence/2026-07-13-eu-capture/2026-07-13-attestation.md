# Operator attestation — interactive EU capture, live Stop boundary (2026-07-13)

## Summary

`elapsed_eu` had been `0.0` on every wave ever closed through a normal Claude Code session, ever since the capture path shipped.

This attestation records the live proof that it no longer is. Five waves (P30-I25-W26 through W31) were claimed inside a real Claude Code session, crossed a real Stop boundary, and closed in a later turn — with the daemon's zero-runtime close gate passing on **measured** runtime rather than an operator waiver.

The prior state of the world, for contrast: 1217 waves, of which 10 had a runtime baseline and 4 a runtime latest; of 781 actuals, exactly 3 recorded `elapsed_eu > 0`, and all 3 were headless spawns that bypass the interactive path entirely.

## Attested facts

- **The production Stop payload carries no counters.** A real payload was recorded off the live hook (`stop-payload-keys.json`, keys + types only): `background_tasks`, `cwd`, `effort`, `hook_event_name`, `last_assistant_message`, `permission_mode`, `prompt_id`, `session_crons`, `session_id`, `stop_hook_active`, `transcript_path`.
- **There is no `cost` block and no `usage` block in it.** The parser's `cost`-mapping precondition could therefore never be satisfied, and the hook reported `runtime.capture skipped: no cost block` on every session it ever ran.
- **Capture now lands, sourced from the session transcript.** The Stop hook aggregates the JSONL the payload points at: deduplicated per-class token tallies, the billed model id, and a duration; cost is derived by pricing those tokens through the same Decimal table a headless spawn is billed against.
- **A wave closed across a real Stop boundary records positive EU with attribution.** P30-I25-W31 (the last wave closed under the fully-corrected accounting): `elapsed_eu=0.9206`, `actual_tokens=61619`, `actual_cost_usd=8.92`, `harness="claude-code"`, `model="claude-opus-4-8"`. Its `SessionAttempt` carries the same delta over a real claim-to-capture span (27 minutes), so the wave-detail metrics tab derives nonzero EU too.
- **Zero hand-edited state.** Every mutation went through the daemon. W25 is the one waived close in the set (`--no-runtime`, operator session): it was claimed *before* its own fix existed, so its claiming session left no baseline to measure against — an honest zero, recorded as such rather than back-filled.
- **The duration source is the transcript timestamp span, not the `turn_duration` row.** The first live run captured real tokens and cost but a **zero duration**: Claude Code writes the `turn_duration` row *after* the Stop hook has already read the transcript. Since `elapsed_eu` derives from duration under the default `API_DURATION` basis, EU would still have landed at zero.
- **Duration stays monotonic.** The aggregator reports the max of the turn-duration sum and the row timestamp span. Both grow with the transcript, so a later capture can never report a smaller duration and the close-time delta never goes backwards.

## What the iter audit changed after this run

A fresh-context audit of the whole iter returned **FAIL** and was right on three counts. The rows in `state-rows.json` were recorded BEFORE those repairs, so they carry the defects below. They are left as written -- rewriting a closed actual means hand-editing state -- and named here instead.

- **The EU in these rows is wall clock, not work.** The duration source was the transcript's first-to-last row span, which counts the operator reading, thinking, and sleeping. On this session's own transcript that span read 1102.7 min against 284.2 min of actual work: **EU here is inflated roughly 4x**. W34 replaced it with the summed working gaps (idle gaps dropped), which is bounded and matches the agent-runtime basis the code always claimed. Every actual recorded before W34 overstates effort.
- **The cross-session rebase was wrong in three ways** (W36): returning to an earlier session double-counted it without bound, a resumed session's pre-wave work was charged to the wave, and a session that ended without a capture lost its runtime while still counting as folded. None of it shows in these rows -- every capture here came from ONE session, so the multi-session mechanism was never exercised live. Its evidence is unit tests, not this run.
- **The configured audit ceiling did nothing** (W35). W32 set `verify.juror_wall_clock_seconds: 1800` and threaded it into the spawn, but the repo config overlay silently dropped every `verify:` leaf except `odr_blocking`, so the close auditor kept spawning at the 600s default. The config line was an idle contract. The watchdog would have aborted a longer audit at 900s regardless.

## The basis change stranded two waves (W37)

Fixing the EU basis broke the waves carrying the fix, and the failure was unrecoverable — worth recording, because the same trap waits for any future change to a cumulative counter.

W35 and W36 were CLAIMED against baselines recorded under the OLD (wall-clock) basis: `api_duration_ms = 65,988,667`. Once W34 landed, their next capture measured the same session under the new basis and read `19,284,744` — a **backwards counter**, which `_counter_delta` raised on. The baseline lives on disk, so every retry of the close compared against it and raised again. No operator action could clear it: **the two waves were permanently unclosable, stranded by the very fix they carried.** The daemon proved it live:

```
LifecycleError: runtime counter api_duration_ms decreased: baseline=65988667 latest=19284744
```

W37 makes a regressed counter a *re-origin* at capture rather than a raise at close (a wave that under-reports its runtime is a bad measurement; a wave that can never close is a broken workflow). Verified live — the same capture that used to crash the daemon now logs `reorigin_on_counter_reset ... re-originating` and succeeds.

The cost is honest and permanent: **W35 and W36 lost the runtime they had accrued under the old basis** (it cannot be reconstructed), so their actuals under-report. W34's own actual is `elapsed_eu=0.0` for a different reason — it was closed in the same turn it was claimed, with no Stop boundary between, so no capture ever fired for it. That zero is an operator process error, not a capture failure.

## Known distortions in this data

Recorded rather than quietly corrected, because they are visible in `state-rows.json` and would otherwise read as measurement error:

- **W26 and W28 over-attribute.** Their baselines were captured before the duration fix (W30) landed, so each carries `api_duration_ms: 0` as its origin and its close differenced against a zero — charging the *whole session span* (`elapsed_eu=1.754`, 13.0M tokens) to each wave instead of its post-claim slice. Their actuals are left as written; rewriting a closed actual would mean hand-editing state.
- **Capture fans out to every active wave.** A `runtime.capture` stamps `runtime_latest` on every wave in `current.active_wave_ids`, so two waves worked concurrently in one session each record the session's delta. This is by design (the runtime cannot attribute a turn to one of several open waves) but it means concurrent waves double-count against each other.
- **Cost is position-dependent within a session.** Prompt-cache reads are billed per request against the *current* context size, which grows through a session. A wave claimed late therefore bills more cache-read cost for the same work than one claimed first. The figure is honest — that money is really spent — and the cure is shorter sessions, not a different number.
- **`actual_tokens` excludes cache reads,** for the same reason. A cache read re-counts the same context on every request, so its volume tracks session position rather than work; counting it as effort makes cross-wave calibration meaningless. Cache reads stay billed in `actual_cost_usd` and visible per-class on the runtime snapshots.

## References

- `stop-payload-keys.json` — the real Stop payload's key set + value types, recorded live off the hook (values redacted: they carry local paths, session ids, and assistant prose)
- `state-rows.json` — the runtime baselines, latests, carries, session attempts, and actuals for P30-I25-W25 through W31, as written by the daemon
- `tests/integration/test_eu_capture_e2e.py` — the end-to-end test encoding this run, driven from the recorded Stop payload and a scrubbed real session transcript
- `.ea/local/research/2026-07-13-p30-i25-eu-capture-fix.md` — the survey that found the two original breaks

## Provenance

Recorded by the P30-I25-W29 session on 2026-07-13, against the interactive Claude Code path on branch `feature/eawf-v0.6-p30`. The waves attested here (W25, W26, W27, W28, W30, W31) are the fix itself, measured by the capture path they repair.

Amended the same day after the iter audit (W34-W36 repairs). The live proof stands -- capture lands, the close gate passes on measurement, the attribution is real -- but the magnitudes in `state-rows.json` predate the EU-basis correction and overstate effort. Treat them as proof that capture WORKS, not as calibration data.

## Scrub

The Stop payload is reduced to its key set and value types; every value is dropped. The state rows are the daemon's own output and carry the same session ids already committed in `.ea/state.json`. No machine paths, hostnames, emails, or tokens are present.
