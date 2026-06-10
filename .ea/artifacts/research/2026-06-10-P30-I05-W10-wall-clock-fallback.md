# P30-I05-W10 Wall-Clock Fallback Spike

## Summary

Verdict: do not add an automatic `now - claimed_at` wall-clock fallback as a follow-up wave in P30. The state sample has 217 P29 waves with `claimed_at` and missing runtime counters; using closed-at wall clock for those rows would add 79.74 EU total, with median 0.10 EU, p90 0.92 EU, and max 4.06 EU, but that interval measures calendar time rather than captured agent runtime [1][2]. Existing `eu_basis=wall_clock` already means captured `total_duration_ms`, so overloading it for missing cost blocks would blur a measured basis with a degraded estimate [1]. Keep the W08 refusal plus W09 `--no-runtime` waiver path: missing runtime should be explicit and attributable instead of silently converted to a pseudo-runtime [3].

next: continue P30-I05 flow into audit after W10 closes.

## Findings

| Item | Result | Evidence |
| --- | --- | --- |
| Sample size | 217 P29 waves have `claimed_at` and missing `runtime_baseline` or `runtime_latest` | State query over `.ea/state.json` [2] |
| Idle overcount | Closed-at wall-clock fallback sums to 79.74 EU across the sample; max single-row fallback is 4.06 EU | Derived from `closed_at - claimed_at` with the configured 30-minute EU divisor [2] |
| Basis meaning | Current `EuBasis.WALL_CLOCK` converts captured `total_duration_ms`, not lifecycle calendar time | `compute_runtime_delta` and `_runtime_basis_eu` [1] |
| Close behavior | W08/W09 now make missing runtime explicit through refusal or operator waiver | Daemon close gate and no-runtime waiver path [3] |

## Decision Matrix

| Axis | Pick | Rationale |
| --- | --- | --- |
| Add follow-up wave | No | The degraded estimate would look like measured elapsed EU while counting idle time [2] |
| Use existing `wall_clock` enum | No | It already maps to captured total duration; reusing it for `claimed_at` would make one enum value mean two measurement families [1] |
| Default close behavior | Keep refusal plus waiver | The operator sees missing runtime immediately and can record a human waiver when recovery is intentional [3] |

## Critical Contracts

Do not reinterpret `EuBasis.WALL_CLOCK` as lifecycle wall clock. If a future operator explicitly wants this fallback, add a separately named degraded basis and mark the resulting actual as estimated, not captured.

## Open Follow-Ups

None for P30-I05. The current path is complete: W08 refuses silent zero-runtime close, W09 supplies the human-attributed waiver, and W10 rejects an automatic wall-clock fallback.

## References

[1] `src/eawf/workflow/lifecycle/wave.py` - runtime-delta and EU-basis conversion semantics.

[2] `.ea/state.json` - sampled P29 wave `claimed_at`, `closed_at`, `runtime_baseline`, and `runtime_latest` fields.

[3] `src/eawf/runtime/daemon/methods/state.py`, `src/eawf/surfaces/cli/commands/lifecycle_wave.py`, and `src/eawf/surfaces/cli/commands/lifecycle.py` - zero-runtime close enforcement and no-runtime waiver plumbing.

## Provenance

scope: `urn:eawf:v1:spec:EAWF/P30/P30-I05/P30-I05-W10`

session: `codex-p30-i05-w10`

starting_commit: `6ad3b093`

store_record: none

## Scrub

Repo-relative references only. No local absolute paths, hostnames, external customer names, credentials, or personal emails.
