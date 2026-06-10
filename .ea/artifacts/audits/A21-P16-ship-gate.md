# A21-P16 ship-gate audit

## Summary

P16 closes the artifact chassis and estimation substrate described in the
integrated v0.3 roadmap [1]. The landed branch contains the dual-harness
co-author fix plus P16 W01-W12 commits; W12 tightened artifact validation so
headings are parsed as chassis sections, provenance and scrub status are
required, and markdown reference rows must be dense, portable, and used by
prose [2][3].

W13 and W14 are covered by the documentation and skill-template commit that
updated managed AGENTS guidance, core profile text, rendered goldens, and the
six core skill bodies for citation density, draft sentinels, scrub status, and
ship-time artifact validation [4][5]. No new `StoreKind.PLAN` was added; plan
source of truth remains state plus rendered markdown views [1].

Backlog reconciliation was inspected for the P14/P15 leftovers. The integrated
roadmap states that stale P14/P15 backlog cleanup is a P22 W01 release-gate
task, so this ship gate does not bulk-close B054-B062 without item-specific
closure proof [6]. P16 leaves that release cleanup explicit rather than hiding
mixed partial delivery under one audit.

## References

[1] .ea/local/research/2026-05-13-v0.3.0-roadmap-integrated.md:83
[2] src/eawf/artifacts/validation.py:36
[3] tests/unit/test_artifact_references.py:18
[4] src/eawf/render/skills.py:268
[5] src/eawf/profiles/data/core.yaml:250
[6] .ea/local/research/2026-05-13-v0.3.0-roadmap-integrated.md:307

## Provenance

- kind: ship-gate
- phase: P16
- audit_id: A21-P16
- scope_id: EAWF
- source: eawf:flow finish P16

## Scrub

- status: clean
