# Publication handoff — what the `0.7.0.dev2` cut proves, and what only the operator can finish

## Summary

The `0.7.0.dev2` checkpoint is cut but not published. This document separates the two, because the difference is the difference between a claim a test can settle and a claim only an outward-facing act can settle.

**Settled here.** The version module and the changelog agree at `0.7.0.dev2`; the changelog section names the migration and its limitations; the tag chokepoint passes over a checkout shaped like the cut commit after the merge. The dev2 Release opens as a distinct DRAFT only over a terminal `REL-0.7.0.dev1`, carries channel `dev`, `authority_epoch` 1 and empty `membership_refs`, and reaches `APPROVED` against a green twelve-gate dev2 sweep. The dev2 readiness reports `waiver_count` zero-or-explained, and a non-zero count with no explanation reds the `waiver_count` gate and denies the approval.

**Not settled here, and not fakeable.** `BAKED` requires three independent read-backs of three live registries. It cannot be reached from an unmerged branch: `ancestry` reds while the checkpoint commit is not an ancestor of `origin/main`, so the live record's approval is refused, and the tag push that starts the publication is an outward-facing act reserved to the operator. Nothing in this wave writes to `.ea/state.json`, opens a live Release record or contacts a registry.

### What the operator runs after the merge

Every step is run from the merged `main` checkout standing on the cut commit. The record document each step consumes is the JSON body the previous step emitted under `--json`.

| # | Command | What it records |
| --- | --- | --- |
| 1 | `uv run eawf release create 0.7.0.dev2 --json` | Appends the DRAFT record to the release-record collection, after checking that `MCT-26091101` is promoted and that `REL-0.7.0.dev1` is recorded at a terminal status. The reply's `supersedes_release_ref` names the burned dev1 record. |
| 2 | Pin the draft to `CANDIDATE` — no operator verb exists (see the gap below) | Sets `source_sha`, `source_tree_sha`, `manifest_ref` and `manifest_digest` on the record. Until this lands as a verb the candidate document is assembled by hand from the release manifest the build produced. |
| 3 | `uv run eawf release preflight 0.7.0.dev2 --json` | Recomputes the twelve-signal sweep over the merged checkout. Exits non-zero while any of the seven required dev2 rows is red, printing the whole repair list first. |
| 4 | `uv run eawf release approve REL-0.7.0.dev2 --release <candidate> --readiness <sweep> --approval-ref <receipt>` | Appends the record at `APPROVED`, binding the manifest digest the publication must reuse. Denies `release_not_ready` naming the first red gate otherwise. |
| 5 | `uv run eawf release tag 0.7.0.dev2 --push` | Recomputes the chokepoint sweep, refuses on any red row, then creates and pushes `v0.7.0.dev2`. **The push is the publication decision**: the release workflow fires on the tag and runs the same sweep again before its publish job. |
| 6 | `uv run eawf release publish REL-0.7.0.dev2 --release <approved> --approved-manifest-digest <digest> --proof-digest <digest> --idempotency-key <key>` | Opens the publication episode and queues the three legs. Returns the operation reference immediately; a slow registry cannot hold the call open. |
| 7 | `uv run eawf release observe REL-0.7.0.dev2 --target pypi --release <record> --manifest <frozen> --idempotency-key <key>` | Reads PyPI back through the `package_index` adapter and settles the leg. Only an independent read-back may write an `observed_*` status. |
| 8 | The same `observe` call with `--target npm` | Reads the npm registry back through the `npm_registry` adapter, resolving `0.7.0-dev.2` on the `next` dist-tag. `latest` must stay on the last stable. |
| 9 | The same `observe` call with `--target github` | Reads the source-host release back through the `source_host_release` adapter, which must find the release notes, checksums and plugin bundle the leg declares. |
| 10 | `uv run eawf release show 0.7.0.dev2 --json` | Confirms `status: baked` with `observed_success` on all three targets — the wave's fourth criterion. |
| 11 | `uv run eawf release advance --release <baked record> --receipt <one per required gate>` | Walks the train onto the dev3 rung, re-validating that every required dev2 gate receipt still binds the exact `source_sha` and `manifest_digest`. `current_checkpoint_index` moves by one. |

### The gap step 2 names

There is no verb between `release create` and `release approve`. `approve` takes a serialized candidate, so the `DRAFT -> CANDIDATE` pin — the move that binds the source commit, its tree and the manifest digest — happens outside the CLI. Every other status move on the ladder has an operator surface; this one does not, and the approval that follows is only as trustworthy as the document somebody hand-assembled. It is worth closing before dev3.

### Why the train index does not move here

`ADVANCING_STATUSES` admits only `baked` and `released`, and `REL-0.7.0.dev1` stands at `partially_released` — terminal, but abandonment rather than a shipped rung. So the train's index still points at dev1 and will move only when the dev2 record itself bakes, at step 11. Opening dev2 does not depend on that index: admission is gated on measurement and on the predecessor being finished with, which is a different question from whether the train ever walked past it.

## References

| Ref | What it anchors |
| --- | --- |
| `src/eawf/_version.py` | The version literal this checkpoint cuts |
| `CHANGELOG.md` | The `0.7.0.dev2` section, its migration outcome and its limitations |
| `src/eawf/workflow/verify/checkpoint_succession.py` | The guard that a checkpoint opens only over a recorded, terminal predecessor |
| `src/eawf/runtime/daemon/methods/release.py` | `release.create`, where that guard runs and where `supersedes_release_ref` is derived |
| `src/eawf/runtime/release/chokepoint.py` | The sweep `release tag --push` and `release preflight` both gate on |
| `src/eawf/workflow/verify/release_readiness.py` | The twelve-signal sweep and the `waiver_block` gate verdict |
| `src/eawf/workflow/release/train.py` | The dev2 checkpoint configuration and its twelve-gate binding table |
| `src/eawf/workflow/release/advance.py` | `ADVANCING_STATUSES` and the receipt re-validation step 11 runs |
| `tests/integration/workflow/release/test_dev2_checkpoint.py` | The open-and-approve walk and the waiver-block cases |
| `tests/integration/runtime/release/test_tag_chokepoint.py` | The chokepoint over the cut commit, and the red paths that earn it |
| `.ea/artifacts/evidence/2026-09-11-dev1-burn/2026-09-11-burn-basis.md` | Why `REL-0.7.0.dev1` is terminal and never republished |
| `MCT-26091101` | The measured contract dev2 admission asserts over |
| `P32-I01-W21` | The wave that cut this checkpoint |

## Provenance

Compiled 2026-09-11 for wave P32-I01-W21, from the code paths tabulated above rather than from a prior document. The command sequence was read off the shipped verb signatures, not off a design note: each row names options the verb actually declares. No state mutation, tag push or registry write was made while compiling it, and every claim about what is settled is backed by a test in this wave's diff.

## Scrub

- status: clean

No absolute local paths, no machine hostnames, no credentials and no PII. Every source reference is repo-relative, and the only external names are the public registries the checkpoint declares as publication targets.
