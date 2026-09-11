# Incident — the 0.7.0.dev1 version published to four targets with no Release record

## Summary

The `v0.7.0.dev1` tag was pushed and the publication pipeline ran against four targets, while the release machinery that is supposed to govern a checkpoint held no record of it at all. `eawf release show 0.7.0.dev1` reports `record: none (never opened)`, and the train still stands at `index=0` with the dev1 rung `status=open`. The version is therefore spent in the world and absent from the model.

Compounding it, the tag was force-moved at least twice *after* artifacts had already been published under it, so the four targets do not agree on which commit `0.7.0.dev1` denotes.

Verdict: **functional impact moderate, process impact high.** Nothing downstream is broken today — the npm leg is correct and the published wheel installs — but the version number is unusable for a clean checkpoint, and no gate noticed that a release had happened without a release record.

### What is live, and from which source

| Target | Holds | Built from | Matches the tag target |
| --- | --- | --- | --- |
| PyPI `eawf` | `0.7.0.dev1` wheel + source distribution, uploaded 2026-09-07T23:00 | a commit predating both later tag targets | No |
| npm `@elementarno/eawf` | `next` -> `0.7.0-dev.1`, published 2026-09-08T14:27 | the final tag target | Yes |
| plugins-dist branch | `versions/0.7.0.dev1` | the 2026-09-07 render | No |
| Source-host release `v0.7.0.dev1` | release exists, `prerelease: false`, zero assets | target `main` | No |

`latest` on npm correctly still points at `0.6.8`, so no consumer on the default channel was affected.

### Timeline

- **2026-09-07T16:02** — the source-host release for `v0.7.0.dev1` is created.
- **2026-09-07T23:00** — a wheel and a source distribution for `0.7.0.dev1` are uploaded to PyPI. This is the build that still occupies the filename today.
- **2026-09-08T12:53** — the tag is pushed again at a second commit. The Release workflow fails at the tag-chokepoint preflight, so its PyPI leg is skipped. The plugin workflow's plugins-dist leg refuses (the version directory already exists) and its npm leg fails.
- **2026-09-08T14:27** — the tag is pushed a third time at a third commit. Preflight passes, but the PyPI upload returns `400 File already exists` because the 09-07 upload occupies the filename under a different digest. plugins-dist refuses again. The npm leg succeeds.
- **2026-09-11** — inspection for W01 finds no dev1 Release record and the train still on its first rung.

Both 2026-09-08 pipeline failures are idempotency refusals working exactly as designed. They are a symptom, not the defect.

### Root cause

1. **The publication path does not require a Release record.** A pushed tag is sufficient to start the pipeline, so the record-keeping side of the checkpoint is advisory in practice. W24 closed having proved the record machinery against a git fixture standing on merged main; it never cut a live record, and nothing detected the gap.
2. **A tag was moved over already-published artifacts.** Once a filename exists on an immutable index, re-tagging cannot re-publish it. Moving the tag therefore guaranteed divergence between targets rather than correcting anything.
3. **`burn_release` has no caller.** The model implements the terminal burn at `src/eawf/workflow/release/publication.py:406` and exports it, but no CLI verb and no daemon method reach it, so the one transition that honestly describes this situation was unreachable when it was needed. This is the same built-without-a-producer shape the project has hit before.

### Disposition

The version is spent. Per the correction rule recorded on W01, a burned version is superseded by the next version and never republished. W01 therefore wires the burn to an operator surface, walks `REL-0.7.0.dev1` to the terminal `PARTIALLY_RELEASED` state so the train has a predecessor, and advances the train. W21 opens `REL-0.7.0.dev2` from that terminal record with `supersedes_release_ref` pointing back at dev1; its criteria need no change because they already require only a *terminal* dev1 record.

The `APPROVED` transition in that walk carries an operator approval of **the burn path**, not a retroactive approval of a release that was never approved. The reason field on the transition and this artifact both say so, so the record never claims an approval that did not happen.

### Follow-ups this incident does not close

- The publication pipeline should refuse a tag whose version has no open Release record; nothing enforces that today.
- The tag chokepoint should refuse a tag push whose version already has published artifacts under a different digest.
- `W01.file_scopes` still reads `.ea/artifacts` alone, which predates the discovery that the burn needs wiring; the wave's real surface includes the release workflow, its CLI command and its daemon method.

## Attempting the disposition proved the gap is total

The operator approved walking dev1 to the terminal burn. The attempt reached only `DRAFT`, and the walk is blocked in both directions.

`eawf release create 0.7.0.dev1` opened the record at `REL-0.7.0.dev1@0`. `eawf release preflight 0.7.0.dev1` then returned `ready=False` with `ancestry` red and `tree_cleanliness` red: this is a feature branch that has not merged, so `approve` refuses. That refusal is correct, and `PARTIALLY_RELEASED` sits behind it.

`CANCELLED` is the only other terminal state reachable from `DRAFT`, and it fails on two counts. Every edge into it carries the guard `NO_EXTERNAL_EFFECT`, documented as "No tag, upload or other external effect has" occurred -- which is false for dev1 on four targets. The guard would nonetheless pass, because it reads the record and the record knows nothing of the publication; the guard being satisfied is itself the defect. And there is no operator surface to cancel at all: `ReleaseStatus.CANCELLED` appears nowhere under `src/eawf/runtime` or `src/eawf/surfaces`, the same built-but-unreachable shape `burn_release` had.

So the live record has **no reachable, truthful terminal state** and stays at `DRAFT`. No gate was waived and no approval was fabricated to move it.

This is the strongest form of the third root cause. The release model assumes every publication passes through it, so it can neither describe nor dispose of one that did not. What it needs is an adoption path: a way to record that a version was published out of band, carrying the observed per-target facts, and to land it in a terminal state without asserting an approval or a readiness sweep that never happened.

## References

| Ref | What it anchors |
| --- | --- |
| `src/eawf/workflow/release/publication.py:406` | `burn_release`, the terminal burn; no caller in the tree |
| `src/eawf/workflow/release/lifecycle.py` | `RECOVERING -> PARTIALLY_RELEASED` is the only edge into the burn; `PARTIALLY_RELEASED` has no out-edges |
| `src/eawf/kernel/spec/release.py` | `PARTIALLY_RELEASED` documented as "Recovery exhausted; the version is burned"; `supersedes_release_ref` carries correction lineage |
| `src/eawf/workflow/release/train.py:95` | `V07_TRAIN`, the source-resident ladder |
| `INC-P32-01` | This incident's typed row |
| `P32-I01-W24` | The wave that proved the record machinery on a fixture without cutting a live record |
| `P32-I01-W01` | The wave that records the burn and advances the train |
| `P32-I01-W21` | Opens dev2 from the terminal dev1 record |

## Provenance

Compiled 2026-09-11 from read-only inspection of the four live publication targets, the source-host workflow run history for the `v0.7.0.dev1` tag, and the release model in the source tree. No write was made to any publication target. The registry and run-history readings are point-in-time and were taken before any state mutation for W01.

## Scrub

- status: clean

No absolute local paths, no machine hostnames, no credentials and no PII. References are repo-relative or eawf-internal ids. The only external names are the public package indexes the published artifacts actually live on, and the package and release identifiers are themselves public.
