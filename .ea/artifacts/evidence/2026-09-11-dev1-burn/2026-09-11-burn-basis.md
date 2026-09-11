# Burn basis — the three read-backs that make `REL-0.7.0.dev1` a spent version

## Summary

This is the evidence the burn of `REL-0.7.0.dev1` rests on: an independent read-back of each of the three targets the `0.7.0.dev1` checkpoint configuration declares, taken before any state mutation for W01.

The burn is the transition `RECOVERING -> PARTIALLY_RELEASED`. Its guard is `recovery_exhausted` — nothing is left to retry. That guard is satisfied here not because a budget ran out but because every leg has already been *observed*, and an observed leg has no retry edge at all: the version is in the world in whatever state the read-back found it, and no further publication under this version number can change that. PyPI forbids filename reuse, so the one leg that diverges cannot be corrected in place even in principle.

What this document does **not** record is an approval of the release. No operator ever approved `0.7.0.dev1` against a manifest digest; the artifacts were published by a tag push with no Release record open at all.

The operator approval that carries the record through `APPROVED` on its way to the burn is an approval **of the burn path**: of walking the spent version to its terminal state so the train has a predecessor. The `approval_ref` and the burn `reason` both say so in those words. A reader who takes the `APPROVED` row as evidence that the release itself was approved would be reading it wrong, and this paragraph exists so that they cannot.

### The three observed target states

| Target | Observed state | What the read-back found |
| --- | --- | --- |
| `pypi` | `observed_mismatch` | The index carries a `0.7.0.dev1` wheel and source distribution uploaded 2026-09-07T23:00, built from a commit that is neither of the two commits the tag was later moved to. The filename is occupied under a digest that does not match the tag target, and filename reuse is refused, so no retry can reconcile it. |
| `npm` | `observed_success` | `next` resolves to `0.7.0-dev.1`, published 2026-09-08T14:27 from the final tag target. This leg is correct. `latest` still resolves to `0.6.8`, so no consumer on the default channel was affected. |
| `github` | `observed_mismatch` | The source-host release `v0.7.0.dev1` exists but carries zero assets and is flagged `prerelease: false`, and its target is `main` rather than the commit the artifacts were built from. The leg's three declared artifact kinds — release notes, checksums and the plugin bundle — are absent. |

Two legs contradicted, one matched. A contradicted read-back routes the record to `RECOVERING`; with every leg observed, no leg has a retry edge left, and the burn is the only move the status machine still offers.

### Why the version is not republished

A burned version is superseded by the next version and never republished. The correction for `0.7.0.dev1` is `0.7.0.dev2`, whose record carries `supersedes_release_ref` pointing back at the burned one. The burned record's `source_sha`, `source_tree_sha`, `manifest_ref` and `manifest_digest` are frozen by the burn's own signature, which accepts no field updates, so the historical claim about what was built stays exactly as recovery found it.

### What the operator runs

The burn is driven by `eawf release burn REL-0.7.0.dev1 --release <record.json> --reason "<why>" --idempotency-key <key>`, which dispatches to the `release.burn` daemon method. The reason is mandatory and is written to both durable rows the call appends — the abandoned publication operation and the burned record — so the terminal status is never left to be read as an outcome.

## References

| Ref | What it anchors |
| --- | --- |
| `.ea/artifacts/incidents/2026-09-11-dev1-published-outside-release-machinery.md` | The incident this burn closes out; the source of the four-target read-back tabulated above |
| `src/eawf/workflow/release/publication.py` | `burn_release`, the terminal burn and its `recovery_exhausted` guard |
| `src/eawf/workflow/release/lifecycle.py` | `RECOVERING -> PARTIALLY_RELEASED` is the only edge into the burn; `PARTIALLY_RELEASED` has no out-edges |
| `src/eawf/workflow/release/target_machine.py` | The two `observed_*` statuses are terminal per leg, which is why an observed leg offers no retry edge |
| `src/eawf/runtime/daemon/methods/release.py` | `release.burn`, the operator-facing verb that reaches the burn |
| `tests/integration/workflow/release/test_dev1_burn.py` | The walk to the terminal state, this document's chassis, and the train's refusal to close a burned rung |
| `INC-P32-01` | The incident's typed row |
| `P32-I01-W01` | The wave that wires the burn and records this basis |

## Provenance

Compiled 2026-09-11 for wave P32-I01-W01. The three read-back rows restate the point-in-time inspection recorded in the incident report, which was taken by reading the public registries and the source-host release before any state mutation for this wave. No write was made to any publication target while compiling it, and the record transitions the document describes are driven separately by the operator.

## Scrub

- status: clean

No absolute local paths, no machine hostnames, no credentials and no PII. Every reference is repo-relative or an eawf-internal id, and the only external names are the public package indexes the published artifacts already live on.
