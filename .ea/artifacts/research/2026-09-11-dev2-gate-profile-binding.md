# The dev2 gate profile: twelve names, twelve evidence sources

> Status: binding record · Decision: `D45`, ACTIVE · Scope: the `0.7.0.dev2` checkpoint's preflight gates and the REL-037 admission legs · Evidence: the committed cutover rehearsal, the daemon-hosted close, the epoch-2 strictness census · Date: 2026-09-11

## Summary

A gate profile that names checks nobody computes is decoration. `dev1` proved the shape: eight gate names, each bound to exactly one evidence source, refused at load if any name is unbound [1]. `dev2` extends that to twelve — the `dev1` eight plus `migration`, `hosted_gate_runner`, `schema_strictness` and `waiver_count` — and this record is the table of what each of the twelve reads [2].

Two things are new rather than incremental.

The first is that the `dev2` configuration is **generated** from the train template and the rung, not overlaid onto the `dev1` file. The overlay is the obvious shortcut and it is silently wrong: the copy keeps `dev1`'s profile and `dev1`'s eight-name required list under the `dev2` version's name, so the new checkpoint runs the old profile's gates.

The loader already refuses that specific overlay, because the declared profile contradicts the train's [3]. Rendering removes the class of mistake rather than the instance, since the required list is read off the profile and exists in exactly one place [4].

The second is a fourth evidence kind. Eleven of the twelve gates read a readiness row, a named component of one, or a proof command run at the pinned revision. `waiver_count` reads none of those: waivers are counted per checkpoint rather than probed, so a thirteenth signal row would have needed a producer that cannot exist. It binds the waiver block the readiness receipt already carries beside the twelve rows [2].

## The binding table

| Gate | Kind | Reads | Green when |
|---|---|---|---|
| `version_consistency` | signal | `version_consistency` | Tag, request, package and configuration spell one version |
| `changelog_entry` | signal | `changelog` | The version's changelog section carries at least one entry |
| `dependency_inventory` | signal component | `dependencies.inventory` | The lock-derived inventory receipt agrees with the swept lock |
| `artifact_reproducibility` | signal | `artifacts` | Two clean builds produced the same digests |
| `security_review` | signal component | `dependencies.vulnerability` | A vulnerability report exists, is readable, and blocks nothing |
| `epoch1_stabilization` | proof command | `epoch1_stabilization_suite` | The suite runs green at the pinned revision |
| `telemetry_producer` | proof command | `telemetry_turn_cost_record` | The turn-cost producer emits a record over the live corpus |
| `front_door_journey` | proof command | `front_door_install_smoke` | A fresh tool-install of the published distribution runs the CLI |
| `migration` | signal | `migration` | Ten corpora rehearsed, every leg holds, the changelog states the outcome |
| `hosted_gate_runner` | proof command | `hosted_close_runs_the_gates` | A close with no attached session runs the gates in the daemon |
| `schema_strictness` | proof command | `epoch2_strictness_census` | Every model in the epoch-2 entity package forbids unknown keys |
| `waiver_count` | waiver block | `readiness:waivers` | Nothing is counted, or every counted waiver is explained and acknowledged |

Each name resolves to exactly one binding at load and to exactly one row on a computed sweep; the loader refuses a duplicate, a stray and an unbound name with three distinct typed codes [1][2][8].

## What makes `migration` provable

The row is green only when the whole declared corpus set was rehearsed and every leg of every rehearsal holds [5].

| Claim | Where it is read |
|---|---|
| Ten corpora, each with a committed record | The declared roster, cross-checked against the set the rehearsal iterates |
| Dry run reproducible | A second seal over the same bytes produced the same manifest digest |
| Apply published a generation across every recorded stage | The recorded stages equal the stages the cutover's own enum declares |
| Rerun inert | Nothing applied, no journal row appended, the tree byte-identical |
| Rollback complete | Outcome `surfaces_restored`, epoch 1, no generation left, every surface matching the restore point |
| Refusal reproducible and inert | The two refusing corpora refuse with one code on all three attempts and leave the target untouched |

A corpus with no record, a leg that was never rehearsed, and an unreadable record each report the row **unavailable**; a leg that ran and disagrees reports it **failed**. Both carry `migration_unproven`, so neither reads as a pass, and they are distinguished because the repairs differ: run the rehearsal versus fix the cutover [5].

Two claims the rehearsal itself corrected are not restated here. There is no `migration_reference_unresolved` code anywhere in the source: a dangling canonical reference raises `migration_fabrication_detected`, which is what the refusing corpus's record pins. And `close_attempts` is an explicit drop, so a close attempt does not import as an immutable legacy record — the closing `agent_sessions` row does [6].

## What `security_review` does with a missing report

The recorded risk for this wave is that `security_review` reads a vulnerability report and a stubbed or absent one passes the gate. It does not, and the three cases are separated [5]:

| Report state | Row | Gate |
|---|---|---|
| Never written | `unavailable`, naming the producing job and the receipt path | Not passing |
| Present but not a vulnerability report | `blocked`, naming the parse failure | Not passing |
| Present with a blocking advisory | `fail`, naming the advisory, package and upgrade | Not passing |

The only passing case is a report that was produced, parsed, and found nothing blocking.

## REL-037: one leg per rung that builds its surface

The measured-before-build rule assigns each probed surface to the rung that builds it, rather than demanding all three at the first covered rung [7].

| Rung | Contract | Surface |
|---|---|---|
| `0.7.0.dev2` | `MCT-26091101` | The importer, re-measured over the production corpus at the thousands scale band |
| `0.7.0.dev3` | `MCT-26081302` | Cross-provider conformance |
| `0.7.0.dev3` | `MCT-26081303` | Daemon RPC under concurrent dispatch |

The importer leg is a **revision**, not an edit: the 2026-08-13 contract measured a read-only census of the epoch-1 document and stays as it was recorded. The revision cites the committed corpus pin the rehearsal is judged against — 4,638 rows, multiplier 1.319 against a 1.60 ceiling, apply 3.849 s against a 30 s budget, residual document 396,720 B against a 1.6 MB bound — and its boundary names both what was measured and where it stops [7].

The `dev3` refusal names **every** missing contract in one message, with its operator-facing label and the exact promotion command. Naming only the first would send an operator back to the same wall once per contract, which is the round-trip the per-contract naming exists to remove [7].

## References

[1] `src/eawf/kernel/release/gate_binding.py` — the four evidence kinds, the profile gate sets, and the loader that refuses an unbound, duplicated or stray gate.

[2] `src/eawf/workflow/release/train.py` — the authored `dev2` binding table and the rendered `dev2` checkpoint configuration.

[3] `src/eawf/kernel/spec/release_config.py` — the checkpoint loader, including the profile-agreement check that refuses a version-and-channel overlay onto the `dev1` file.

[4] `src/eawf/kernel/release/checkpoint_template.py` — the train-wide template and the per-rung renderer.

[5] `tests/integration/workflow/verify/test_migration_signal.py` — the migration signal over the ten corpora, and the three `security_review` cases.

[6] `tests/integration/kernel/migration/test_v07_rehearsal.py` — the four-leg rehearsal that produces the committed records.

[7] `src/eawf/workflow/release/admission.py` — the REL-037 admission table and the exhaustive refusal.

[8] `tests/integration/workflow/verify/test_dev2_profile.py` — the deterministic gate over the twelve names, the rendering, and the refused overlay.

## Provenance

- kind: research
- slug: 2026-09-11-dev2-gate-profile-binding
- wave: P32-I01-W20
- method: binding record written alongside the implementation; every table row is asserted by a test named in the references, and no number here was estimated
- decision: `D45`, which assigns the `dev2` gate profile and the REL-037 legs; recorded there, documented here
- gates: the two suites named in references 5 and 8, plus the REL-037 integration test

## Scrub

- status: clean
- references: repo-relative only
- credentials, PII, machine-specific paths, hostnames: none
