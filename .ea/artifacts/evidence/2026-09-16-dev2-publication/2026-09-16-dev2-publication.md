# v0.7.0.dev2 publication and approval

## Summary

`0.7.0.dev2` was published on 2026-09-16 from the merged P32 commit `d4f71dcf` through the annotated tag `v0.7.0.dev2`. All three targets published and read back: PyPI, npm under the `next` dist-tag, and a GitHub prerelease. The release record `REL-0.7.0.dev2` is `approved` at revision 2 with this note as its approval reference; it is not `baked`, because the shipped publication verbs cannot take any record past approval yet, and that repair belongs to `P33-I01-W01`.

The approval was recorded after the tag push, on purpose. This note says why, what the approval binds, and what is left.

## What was published

| Target | Identity | Version | Artifact | Digest | Workflow run |
|---|---|---|---|---|---|
| PyPI | `eawf` | `0.7.0.dev2` | `eawf-0.7.0.dev2-py3-none-any.whl` | `sha256:d03753d6…8da5` | Release `35119940687` |
| PyPI | `eawf` | `0.7.0.dev2` | `eawf-0.7.0.dev2.tar.gz` | `sha256:51218682…2b78` | Release `35119940687` |
| npm | `@elementarno/eawf` | `0.7.0-dev.2` (`next`) | `elementarno-eawf-0.7.0-dev.2.tgz` | `sha256:61918412…bd8c` | Plugin Release `35119940584` |
| GitHub | `Elementarno9/eawf` | `v0.7.0.dev2` (prerelease) | `RELEASE_NOTES.md` | `sha256:135a7b96…d305` | Plugin Release `35119940584` |
| GitHub | `Elementarno9/eawf` | `v0.7.0.dev2` (prerelease) | `SHA256SUMS` | `sha256:d942bada…0afc` | Plugin Release `35119940584` |
| GitHub | `Elementarno9/eawf` | `v0.7.0.dev2` (prerelease) | `eawf-plugin-0.7.0.dev2.tar.gz` | `sha256:6d894244…99e9` | Plugin Release `35119940584` |

The full digests are in the three `publication-receipt-*.json` files beside this note, exactly as the workflows wrote them. npm's `latest` tag stays on `0.6.8`.

## Readiness before the tag

The approval binds `pre-tag-readiness.json`, the dev2 sweep computed at 2026-09-16T15:36:39Z over a clean checkout of `d4f71dcf`, with the dependency and reproducibility receipts from the no-publish release dry run `35115222826` in place. It reports `ready` with zero waivers, and all seven required rows pass: version consistency, changelog, ancestry, tree cleanliness, migration, artifacts and dependencies.

CI on `d4f71dcf` concluded green before the tag was pushed (CI run `35114319538`; its ubuntu job was re-run once after a known flaky lockfile heartbeat test). Both publish workflows re-checked that result, and the release workflow re-ran the same sweep as its tag chokepoint before publishing to PyPI.

## Why the tag came before the approval

An approval pins the manifest digest that every later observation compares the registries against. Before the tag run, that manifest cannot be written truthfully:

- the npm tarball, the plugin bundle and `SHA256SUMS` exist only once the tag's workflows have built them;
- the PyPI files the tag run builds do not carry the dry run's digests, because the dry run's reproducibility build pins the build timestamp and the tag build does not.

An approval recorded first would therefore have pinned digests guaranteed to fail observation, which ends in recovery and a burn, the way `0.7.0.dev1` ended. The operator chose to tag first and approve after. The approval binds the pre-tag sweep above and `frozen-manifest.json`, which is built from the digests the workflows actually published (`manifest_digest` `sha256:3ea0e125…e148`). The record is an approval, not an adoption: it carries `approval_ref` and no `adoption` block, so the path to `baked` stays open.

## Registry read-back

At the time of recording, each target was read back by hand:

- the PyPI JSON API lists both files for `0.7.0.dev2`, with sha256 digests matching the receipt;
- the npm registry lists `0.7.0-dev.2`, published 16:11:29Z, with `next` on it and `latest` on `0.6.8` (a first read minutes earlier returned a cached document without it);
- the GitHub release `v0.7.0.dev2` is a prerelease with the three assets above, and their digests match the receipt.

These are manual checks. They are not `eawf release observe` settlements and are not recorded as such.

## What is not done

`baked` cannot be reached with the code as shipped in 0.7.0.dev2:

- `release publish` refuses the approved record as `approval_stale`, because the daemon recomputes readiness without the working-copy tag probes, so five required rows read `unavailable` (`src/eawf/runtime/daemon/methods/release.py`);
- publication legs open as `queued`, and no verb moves them on or starts verification;
- `release observe` has no live registry reader and needs hand-supplied responses, and the npm response has to carry a `dist.files` digest the registry does not serve;
- `release advance` always refuses, because the train's current rung is a source constant.

`P33-I01-W01` owns the repair and the remaining walk: publish, observe the three targets, `baked`, then advance the train to dev3.

## Files

| File | What it is |
|---|---|
| `frozen-manifest.json` | the manifest the approval pins; its digest is the record's `manifest_digest` |
| `pre-tag-readiness.json` | the readiness sweep the approval binds |
| `publication-receipt-pypi.json` | the PyPI publish receipt from the release workflow |
| `publication-receipt-npm.json` | the npm publish receipt from the plugin release workflow |
| `publication-receipt-github.json` | the GitHub release receipt from the plugin release workflow |

## References

| Ref | Location | What it establishes |
|---|---|---|
| R1 | `.ea/artifacts/evidence/2026-09-11-dev2-cut/2026-09-11-dev2-publication-handoff.md` | the operator runbook this publication followed and where it deviated |
| R2 | `.github/workflows/release.yaml` | the PyPI publish job and its green-CI and tag-chokepoint gates |
| R3 | `.github/workflows/plugin-release.yaml` | the npm and GitHub publish jobs |
| R4 | `.ea/artifacts/audits/2026-09-16-A-P32-closeout.md` | the phase that cut this checkpoint |
| R5 | `.ea/store/release_record.jsonl` | `REL-0.7.0.dev2` at revisions 0 (draft) and 2 (approved) |

## Provenance

Compiled 2026-09-16 at publication, from the workflow receipts, the release-record verbs' own output, and read-backs of the three registries. The candidate was built with the release library's `advance_release` and `FrozenManifest` rather than by hand-typed JSON, and the stored manifest was re-read to confirm it reproduces the pinned digest.

## Scrub

- status: clean
- references: repo-relative
- credentials, PII, machine-specific paths: none
