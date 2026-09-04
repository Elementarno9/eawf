# Dev1 turn cost: 25 completed units, verification a fifth of the spend

> Status: baseline artifact · Scope: the first turn-cost measurement of this repository and the tolerance it is frozen under · Method: the completed-unit producer run over the priced rows of `state.actuals` · `verified_at_commit`: `7db9c303` · Date: 2026-09-04

## Summary

This is the first turn-cost measurement this repository has taken, and the regression tolerance it will be checked against was fixed at `0.20` before it was taken, not after [8][9]. Everything else here follows from one decision about the sample: the corpus is 25 completed units, and the artifact says so in the same breath as it reports the numbers, because a percentile stated without its sample invites the reading that it covers the whole project [9].

Over those 25 units the median unit costs 3.007518 USD and the ninetieth-percentile unit costs 8.924265 USD; the median unit runs 346.67 seconds of agent runtime and the ninetieth-percentile unit runs 3157.581 seconds. The verification-cost question this measurement exists to answer has a number: 23.307666 USD of the 112.546178 USD spent across the corpus was verification-class work, or 20.71 percent, against 89.238512 USD of execution-class work. Two of the 25 units were verification runs and 23 were execution runs, so the split is carried by a small tail and should be read as a first observation rather than a rate.

The sample is smaller than the row count of the collection it comes from, and every step of the narrowing is stated below rather than folded away. `state.actuals` holds 878 rows. 850 of them carry a cost of exactly zero along with no harness and no model, so they name no runtime/model tuple to be measured under and are not summable as zero either — an unpriced row and a zero-cost row are different facts [3]. Of the 28 rows that do carry a cost, three carry no agent role, and the producer refuses an unlabelled run rather than charging it to execution [4]. That leaves 25.

The count of rows excluded for unsettled reasoning-token accounting is **zero, and it is zero for two independent reasons**. The exclusion set is empty: no supported runtime is listed in it today [5]. And the collection contains no row that could be listed — every harness value in `state.actuals` is either absent or `claude-code`, so there is no codex row anywhere in it to exclude. Reporting a nonzero exclusion here would be reporting a filter that never ran.

## Sample

Every narrowing between the collection and the corpus, with where it happened. The producer's own exclusion counters are all zero because each exclusion here happened at corpus selection, before the producer saw a row; the two columns are kept apart so a zero on the record is never mistaken for "nothing was dropped".

| Bucket | Rows | Where it was dropped | Why |
|---|---:|---|---|
| `state.actuals` rows | 878 | — | the whole collection [1] |
| carrying no cost | 850 | corpus selection | cost is exactly zero with no harness and no model, so the row declares no runtime/model tuple; never summed as zero [3] |
| priced | 28 | — | `actual_cost_usd` greater than zero; harness `claude-code` throughout |
| priced but unlabelled | 3 | corpus selection | the wave carries no `agent_role`, and an unlabelled run raises rather than landing in either cost sum [4]; these are also the only `claude-fable-5` rows |
| **completed units measured** | **25** | — | closed waves on the single tuple `claude` / `claude-opus-4-8`, one run each |
| excluded `reasoning_summand_unsettled` | 0 | — | the exclusion set is empty [5] and no codex row exists in the collection to populate it |

The 25 units are 23 executor runs, one auditor run (`P30-I25-W29`) and one polisher run (`P30-I25-W38`). Auditor and polisher are the verification classes; executor is an execution class [4].

The roster below is the corpus, named row by row, so the measurement can be re-run over exactly these rows rather than over whatever the collection holds later. The gate reads it and rebuilds the record from it [11].

```json
{
  "fixture_id": "dev1-actuals-2026-09-04",
  "runtime": "claude",
  "model": "claude-opus-4-8",
  "state_row_count": 878,
  "priced_row_count": 28,
  "unpriced_row_count": 850,
  "unlabelled_priced_row_count": 3,
  "reasoning_summand_unsettled_row_count": 0,
  "unlabelled_priced_scope_ids": ["P30-I23-W28", "P30-I23-W45", "P30-I23-W46"],
  "corpus_scope_ids": [
    "P30-I25-W26", "P30-I25-W27", "P30-I25-W28", "P30-I25-W29", "P30-I25-W30",
    "P30-I25-W31", "P30-I25-W32", "P30-I25-W33", "P30-I25-W35", "P30-I25-W36",
    "P30-I25-W37", "P30-I25-W38", "P30-I25-W39", "P30-I25-W40", "P30-I25-W41",
    "P30-I25-W42", "P30-I25-W43", "P30-I25-W44", "P30-I25-W45", "P30-I25-W46",
    "P30-I25-W47", "P30-I25-W48", "P30-I25-W50", "P30-I25-W51", "P30-I25-W52"
  ]
}
```

## Record

The measurement itself, as a `TurnCostRecord` [2]. The model forbids unknown fields, so this block cannot be extended by hand without the gate rejecting it, and it deliberately carries no combined cost total: a consumer that wants verification plus execution has to add the two and can never mistake one for the whole.

```json
{
  "execution_cost_usd": "89.238512",
  "fixture_id": "dev1-actuals-2026-09-04",
  "harness_revision": "turn-cost-1",
  "model": "claude-opus-4-8",
  "p50_cost_usd": "3.007518",
  "p50_wall_clock_ms": 346670,
  "p90_cost_usd": "8.924265",
  "p90_wall_clock_ms": 3157581,
  "reasoning_summand_unsettled": false,
  "reasoning_summand_unsettled_run_count": 0,
  "runtime": "claude",
  "token_total": 35870107,
  "unattributed_run_count": 0,
  "unit_count": 25,
  "unpriced_run_count": 0,
  "verification_cost_usd": "23.307666"
}
```

Three fields need a caveat rather than a footnote.

`token_total` is a three-class total, not the four-class total the field is defined as. `state.actuals` records new tokens only — input plus output plus cache writes, with cache reads deliberately left out because a cache read tracks how deep into a session a wave sits rather than how much work it did [6]. The reads were still billed, so `p50_cost_usd`, `p90_cost_usd` and both cost sums include them; only the token figure is a floor.

`p50_wall_clock_ms` and `p90_wall_clock_ms` are agent runtime, not elapsed calendar time. The state row records effort units on the api-duration basis, so the millisecond figure here is that effort reconstructed at the 30-minute-per-unit default [7]. A unit that waited an hour for review does not show that hour.

Both cost sums are quantised to micro-USD before summing. The state row holds a binary float, and `10.079999999999984` is not a cost anybody incurred; rounding to the millionth of a dollar once, at the boundary, keeps every downstream percentile an exact `Decimal` and keeps a re-run byte-identical.

## Threshold

The tolerance was fixed at the packet default of `0.20` before this measurement was taken, and it is written here rather than passed at check time so the tolerance travels with the number it guards [2]. It is not derived from the spread observed above; deriving it would make every future result tolerable by construction, which is the failure `MEAS-034` exists to prevent [8][9].

```json
{
  "fixture_id": "dev1-actuals-2026-09-04",
  "harness_revision": "turn-cost-1",
  "model": "claude-opus-4-8",
  "p90_cost_usd": "8.924265",
  "p90_wall_clock_ms": 3157581,
  "runtime": "claude",
  "threshold": "0.20"
}
```

Checked against the record it was taken from, this baseline passes: no percentile rose, so neither can have crossed the tolerance [11]. The gate also inflates the record's p90 cost by 21 percent and asserts the comparison reds, so the tolerance is demonstrably able to fire rather than merely present [11].

## Why the corpus is `state.actuals` and not the live command path

`eawf bench turn-cost --fixture live` sources its corpus from the projected telemetry cache: it reads `telemetry_sessions` out of `.ea/telemetry.db` and joins those rows onto the closed waves in state [12]. That table holds zero rows in this repository, and the join therefore returns nothing to measure. Under `--check` that is a refusal and not a pass — a check that succeeds because there was nothing to measure is a false green — so the live path exits 1 before any comparison happens [13]. Driving the first baseline through it would have produced no baseline at all.

The measured cost of agent work is nevertheless recorded, just in the other collection: the daemon writes a per-wave rollup of tokens and priced spend into `state.actuals` at wave close [1][6]. The producer takes its runs as an argument rather than fetching them, so the caller chooses the corpus [2]; this artifact exercises that seam directly with the `state.actuals` rows and the gate rebuilds the record the same way [11].

That is a deliberate substitution and it has a cost worth naming. The baseline is stamped `dev1-actuals-2026-09-04` rather than `live`, and the comparability rule refuses to compare records across fixture ids [2], so a later `--fixture live` measurement will report `comparison_invalid` against this baseline rather than a phantom regression. That is the correct outcome: once telemetry rows exist they will describe a different quantity — per-session rows rather than per-wave rollups — and the live corpus deserves its own first measurement and its own frozen tolerance.

## Proposal numeric defaults this measurement touches

The configuration contract holds its numeric defaults as *proposal* defaults until a dev1 gate remeasures them [10][14]. This wave's producer reads the turn-cost block, and its record is the first evidence available against the runtime wall, the gate-tier budgets and the unit ceiling. Every row below carries the command that reproduces its measured value, so a reader can disagree by re-running rather than by argument. A row measured by the sibling configuration-defaults benchmark points at that artifact rather than restating its number [14].

`unmeasured` is a status, not a failure: it means no dev1 gate reads the default, so nothing at this checkpoint produces a sample, and the obligation to remeasure travels to the checkpoint whose gates first read it.

| Configuration key | Proposal default | Measured at dev1 | Sample | Re-runnable command | Verdict |
|---|---|---|---:|---|---|
| `measurement.turn_cost.regression_threshold` | 0.20 | 0.20, fixed before the measurement | 25 units | `uv run pytest tests/perf/test_dev1_baseline_artifact.py -k threshold_frozen` | held frozen by construction; deriving it from this spread is what `MEAS-034` forbids [9] |
| `measurement.turn_cost.percentiles` | [50, 90] | both emitted, nearest-rank | 25 units | `uv run pytest tests/perf/test_dev1_baseline_artifact.py -k record_and_sample_sizes` | holds; nearest rank returns an observed value, so the percentiles are exact `Decimal` |
| `measurement.turn_cost.unit` | completed_task | 25 completed units from 25 closed waves, one run each | 25 units | `uv run pytest tests/perf/test_dev1_baseline_artifact.py -k record_and_sample_sizes` | holds, but untested where it matters: no unit in this corpus carries more than one run, so the split-invariance the unit exists for is exercised only by the seeded fixture |
| `runtime.default_wall_seconds` | 2400 | p90 per-unit agent runtime 3157.6 s; p50 346.7 s | 25 units | `uv run pytest tests/perf/test_dev1_baseline_artifact.py -k record_and_sample_sizes` | different quantity, reported not ruled: the default bounds one run, this figure aggregates a whole wave. The per-session quantity the default actually bounds measured 208.6 s at p90 and holds [14] |
| `runtime.lost_grace_seconds` | 30 | 15.007 s p90 heartbeat gap | 12 gaps | `uv run pytest tests/unit/platform/artifacts/test_dev1_config_defaults_benchmark.py` | holds, measured by the sibling benchmark [14] |
| `runtime.global_capacity` | 8 | unmeasured | — | none exists | no dev1 gate reads it and dispatch concurrency is not exercised at dev1 [14] |
| `runtime.per_provider_capacity` | 4 | unmeasured | — | none exists | per-provider slice of the same unexercised bound [14] |
| `runtime.max_task_attempts` | 3 | unmeasured | — | none exists | no task-attempt history is recorded to measure [14] |
| `quality.gates.T0.deadline_seconds` | 30 | 0.62 s cold, 0.08 s warm, 0.08 s warm | 3 runs | `uv run pytest tests/perf/test_dev1_baseline_artifact.py` | holds for this wave's changed scope with three orders of magnitude of headroom; a whole-repository T0 selection is not measurable, see below |
| `quality.gates.T1.deadline_seconds` | 180 | unmeasured | — | none exists | no invocation selects T1 by tier; see below |
| `quality.gates.T2.deadline_seconds` | 600 | unmeasured | — | none exists | no invocation selects T2 by tier; see below |
| `quality.unit_test_seconds_ceiling` | 0.5 | slowest added test 0.03 s call, 0.09 s including setup | 3 tests | `uv run pytest tests/perf/test_dev1_baseline_artifact.py --durations=0` | holds for the tests this wave adds; the ceiling is unmeasured across the existing suite because no budget artifact records per-test durations |

The three tier budgets are unmeasurable at dev1 for a structural reason rather than a scheduling one. A tier budget is a claim about a tier, and measuring one requires an invocation that runs *that tier and only that tier*. No such invocation exists: the recipes that run tests select by path and by marker, not by declared tier, so timing any of them measures a directory rather than T1 or T2 [15]. There is also no budget artifact recording per-test durations, so the unit ceiling cannot be evaluated across the existing suite either — `tests/.budgets.json` is absent. Both gaps are the same missing deliverable, and until it lands a number recorded against these budgets would be a number about something else.

The T0 figure above is honest about the same limit: it is a genuine changed-scope selection, but the changed scope is two files. It says the tier's budget is not at risk from this wave, and it does not say the repository's T0 selection fits in 30 seconds.

## Method

The corpus is every row of `state.actuals` whose `actual_cost_usd` is greater than zero and whose wave carries an `agent_role`, which is 25 rows on the single tuple `claude` / `claude-opus-4-8` [1]. Each row becomes one run of one completed unit: the wave supplies the role and the closed status, `elapsed_eu` supplies agent runtime at the 30-minute-per-unit api-duration default [7], `actual_tokens` supplies the three-class token figure [6], and `actual_cost_usd` supplies the cost quantised to micro-USD. Runs are handed to the producer, which groups them by closed wave, folds each unit, and takes nearest-rank percentiles over the per-unit aggregates rather than over individual runs [3].

Rows are not filtered by date, by phase or by outcome; every priced, labelled row in the collection is in. The single-tuple restriction is the producer's own rule and not a choice made here: a percentile taken across models measures the model mix rather than the work, so a corpus spanning two tuples is refused [2]. The three rows that fall outside the tuple are also the three that carry no role, so the two restrictions happen to select the same 25 rows.

The gate re-runs all of this. It parses the roster, the record and the baseline out of this file, rebuilds the record from the named state rows, and asserts the rebuilt record equals the recorded one field for field [11]. A baseline that agrees only with itself proves nothing; this one has to agree with a re-run.

## References

[1] `.ea/state.json` — the `actuals` collection: 878 rows, 28 with a non-zero `actual_cost_usd` and 850 without; every `harness` value is `claude-code` or absent.

[2] `src/eawf/observability/bench/turn_cost.py` — the baseline model carrying its own threshold, the comparability refusal across fixture ids, and the single-tuple rule on a corpus.

[3] `src/eawf/observability/telemetry/turn_cost.py` — the record model, the completed-unit producer that takes its runs injected, and the rule that an unpriced run is counted rather than summed as zero.

[4] `src/eawf/observability/telemetry/cost_class.py` — the total role-to-cost-class map, and the refusal to attribute a run that carries no role.

[5] `src/eawf/observability/telemetry/turn_cost.py` — `REASONING_UNSETTLED_RUNTIMES`, empty: every supported runtime reports reasoning tokens as a subset of output.

[6] `src/eawf/workflow/lifecycle/wave.py` — the close-time runtime delta: `actual_tokens` excludes prompt-cache reads while `actual_cost_usd` bills them.

[7] `src/eawf/observability/telemetry/join.py` — the 30-minute effort-unit default the api-duration basis converts against.

[8] `.ea/local/research/2026-08-04-v07-proposal/00-authority-scope-and-decisions.md` — the ruling that the turn-cost producer is two deliverables and that its first baseline declares its sample and freezes its threshold before the second measurement.

[9] `.ea/local/research/2026-08-04-v07-proposal/67-measurement-and-instrumentation.md` — the rule that the regression threshold is recorded before the second measurement is taken, and the `measurement.turn_cost` block carrying `regression_threshold: 0.20`.

[10] `.ea/local/research/2026-08-04-v07-proposal/00-authority-scope-and-decisions.md` — the configuration contract preamble holding exact numeric defaults as proposal defaults until remeasured at dev1, and the `runtime` block of five defaults.

[11] `tests/perf/test_dev1_baseline_artifact.py` — the gate over this artifact: it validates the embedded record, rebuilds it from the roster, asserts the frozen tolerance and the passing check, and asserts an inflated p90 reds.

[12] `src/eawf/surfaces/cli/commands/bench.py` — the live corpus path, which reads `telemetry_sessions` out of the projected telemetry cache and joins them onto closed waves, and the `--check` branch that delegates to the comparison.

[13] `src/eawf/surfaces/cli/commands/bench.py` — the empty-corpus branch: under `--check` an unmeasurable corpus is a refusal that exits 1, never a pass. The cache at `.ea/telemetry.db` holds zero `telemetry_sessions` rows.

[14] `.ea/artifacts/research/2026-09-04-dev1-config-defaults-benchmark.md` — the sibling dev1 benchmark measuring four configuration defaults and recording seven as unmeasured.

[15] `justfile` — the test recipes, which select by path and by marker rather than by declared tier.
