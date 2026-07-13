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
- **The `turn_duration` row lands after the hook reads the transcript.** The first live run captured real tokens and cost but a **zero duration** for exactly that reason. It is why the duration for a turn is counted at the *next* capture, and why a wave must cross a Stop boundary between claim and close (see "Four measures of one duration" below — the duration source was subsequently redefined three more times).

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

## Four measures of one duration

The number called `api_duration_ms` was redefined four times inside this one iter. Each definition looked right until its own data arrived, and three of the four turned out to be the operator's wall clock wearing a disguise. The version is now stamped on every snapshot (`RuntimeBaseline.measure_version`), because a counter is comparable only against a baseline taken under the same definition — and a redefinition that RAISES the figure is otherwise indistinguishable from a productive week.

| Version | Definition | Why it was wrong |
| --- | --- | --- |
| 1 | Whole-session wall-clock span, first row to last | Counts the operator reading, thinking, and sleeping. Inflated roughly 4x on this session's own transcript: 1102.7 min recorded against 284.2 min of work. |
| 2 | Summed gaps between rows, dropping gaps over a 15-minute ceiling | The ceiling is a guess. A long tool run is dropped; a 14-minute nap is kept. |
| 3 | Summed per-turn spans: everything inside a turn, nothing between turns | Still the wall clock. A turn *contains* the stall at a tool-permission prompt, where the agent waits on a human. This one booked **12.9 hours of the operator asleep at a prompt** as agent runtime. |
| 4 | Claude's own `turn_duration` figure, per completed turn | Current. Claude measures its own turn and excludes the approval stall — a distinction nothing in the transcript can reconstruct, because an approval stall and a long-running tool have the identical shape. |

Measure 4 deliberately has **no span fallback for the turn still in flight**: it reports zero for an unmeasured turn rather than a guess. The `turn_duration` row lands after the Stop hook has read the transcript, so a turn is counted at the *next* capture, not its own. That is why a wave must cross a Stop boundary between claim and close — not ceremony, but the structure of the measurement.

## What a counter reset means

`RuntimeCarry.counter_resets` counts the times a wave's counter source was re-originated: a truncated transcript, or a change to what the duration measures. Each reset **drops the runtime measured before it, permanently** — cumulative counters carry no history to reconstruct.

So a reset is the recorded REASON a wave may close with less runtime than it really spent, or with none. Without the count, that close is indistinguishable from a capture path that silently did nothing — which is the failure this whole iter exists to make impossible. The zero-runtime close gate reads the count and lets an honestly-reset wave close; the exemption is *pending-only* (it lapses the moment a capture reports after the reset), so a stale reset cannot pardon a capture path that dies forty turns later.

The v3 -> v4 change re-originated W43, W44 and W45 with `counter_resets: 1` rather than banking a 13-hour phantom delta. That is the mechanism working, and it is visible on the rows.

## What these actuals are worth: nothing, for calibration

Every EU figure this iter recorded measures the repair of the measure, not the work. **Decision `D-I25-EU-EXCLUDE`** records the exclusion in state; this section says why. The rows stay exactly as written — state reflects what was measured, and the pre-reset runtime cannot be re-derived.

- **W26 and W28 over-attribute by roughly 4x.** Captured under measure 1 (wall clock), and their baselines predate the duration fix, so each differenced against a zero origin and charged the whole session span (`elapsed_eu=1.754`, 13.0M tokens) to itself.
- **W34 is a zero from a process error**, not a capture failure: it was closed in the same turn it was claimed, so no Stop boundary fired and no capture ever ran for it.
- **W35, W36 and W39-W42 are truncated by a measure re-origin.** Each closed on a floor, not a measure — the runtime accrued before the re-origin is gone.
- **The fan-out duplicates are the largest distortion.** One `runtime.capture` is written to every wave in `current.active_wave_ids`, and each wave then differenced the whole session. W35 and W36 both recorded 0.3769 EU / $3.10; W39 through W42 all recorded 0.0419 EU. The runtime is real — it is one session's, counted N times. W46 fixed it forward (the snapshot records `shared_wave_count`, the delta divides by it, the divisor stays on the row), and the six waves closed after it each recorded 346,670 ms of a 2,080,022 ms session, six ways. It cannot retro-split a row already closed.
- **From W47 on, the row says so itself.** `ActualSummary.calibration_excluded` is set at close when the wave's runtime was re-originated or shared, so a calibration consumer skips it reading `state.json` alone rather than reading this document. Every wave closed in the last two cycles carries it.

Two distortions below are NOT defects and are not excluded — they are properties of what is being billed:

- **Cost is position-dependent within a session.** Prompt-cache reads are billed per request against the *current* context size, which grows through a session. A wave claimed late bills more cache-read cost for the same work than one claimed first. The figure is honest — that money is really spent — and the cure is shorter sessions, not a different number.
- **`actual_tokens` excludes cache reads.** A cache read re-counts the same context on every request, so its volume tracks session position rather than work; counting it as effort makes cross-wave calibration meaningless. Cache reads stay billed in `actual_cost_usd` and visible per-class on the runtime snapshots.

## References

- `stop-payload-keys.json` — the real Stop payload's key set + value types, recorded live off the hook (values redacted: they carry local paths, session ids, and assistant prose)
- `state-rows.json` — the runtime baselines, latests, carries, session attempts, and actuals for P30-I25-W25 through W31, as written by the daemon
- `tests/integration/test_eu_capture_e2e.py` — the end-to-end test encoding this run, driven from the recorded Stop payload and a scrubbed real session transcript
- `.ea/local/research/2026-07-13-p30-i25-eu-capture-fix.md` — the survey that found the two original breaks
- Decision `D-I25-EU-EXCLUDE` (in `.ea/state.json`) — the typed record that the P30-I25 actuals are excluded from calibration, and why
- `src/eawf/runtime/runtimes/claude/transcript_counters.py` — `MEASURE_VERSION`, whose comment block is the canonical definition of all four measures

## Provenance

Recorded by the P30-I25-W29 session on 2026-07-13, against the interactive Claude Code path on branch `feature/eawf-v0.6-p30`. The waves attested here (W25, W26, W27, W28, W30, W31) are the fix itself, measured by the capture path they repair.

Amended the same day after the iter audit (W34-W36 repairs). The live proof stands -- capture lands, the close gate passes on measurement, the attribution is real -- but the magnitudes in `state-rows.json` predate the EU-basis correction and overstate effort. Treat them as proof that capture WORKS, not as calibration data.

Amended again on 2026-07-14 after the third and fourth repair cycles (W43-W48). Two things changed that this document had asserted: the duration measure (now Claude's own per-turn figure, version 4 of four) and the attribution of a shared session (now divided among the waves that shared it, with the divisor on the row). The claim that "duration stays monotonic" was removed: it was true of the old max-of-two-sums aggregator, and the honest replacement is the measure version, which makes a redefinition a recorded fact rather than something inferred from which way a number moved. The six waves closed under measure 4 (W43-W48) are the first EU figures in this repo taken on a bounded, non-wall-clock measure and split by concurrency -- and they are still excluded from calibration, because a wave that measures its own repair is not a reference class.

## Scrub

The Stop payload is reduced to its key set and value types; every value is dropped. The state rows are the daemon's own output and carry the same session ids already committed in `.ea/state.json`. No machine paths, hostnames, emails, or tokens are present.
