# Native-canary conformance evidence for 0.7.0.dev4

## Summary

This directory holds the `REL-0.7.0.dev4` native-canary evidence export [1]. `native-canary-evidence.json` is what the `provider` and `membership` readiness rows of the dev4 native-canary rung read, through `eawf.workflow.evidence.provider_certification` [4], which finds each release's export by its release key rather than at one fixed location. The dev3 export [5] stays where it was and keeps backing the dev3 rung unchanged.

Two parts of the export have different origins, and this brief keeps them apart. The certification is carried forward from the dev3 export, not re-probed. The acceptance walk behind the membership reference was re-recorded for this release.

## The certification, carried forward

The `certifications`, `advertised`, `run_logs` and `isolation` fields are copied unchanged from the dev3 export [5]. That record was produced on 2026-09-18 by wave `P33-I01-W63` (phase 33, iter 1, wave 63), which drove the conformance runner [6] against `claude-code` distribution version `2.1.274` on `macos` / `aarch64`. It carries contiguous `probe`, `canary` and `certify` stages, all `passed`, and is cited by the URN `certification://claude-code/2026-09-18`. Every capability and the certification itself expire on 2026-12-17.

No runtime was probed for this directory. The dev4 rung's `provider` row passes on the dev3 observation for as long as that observation is fresh, and the carried-forward record says so by keeping its original id, URN, stage timestamps and expiry rather than being re-dated. `claude-code-driver-manifest.json` [2] is a byte-for-byte copy of the dev3 manifest the recorded digest is taken over, so the digest stays recomputable from this directory alone.

What the dev3 brief says about the certification's limits still holds: the canary stage graded a declared containment outcome rather than a dispatched Run, and `install_trust` is `observed`, not `managed`. The `isolation` pair is likewise the dev3 rehearsal's, over canary provisioning and the native-authority fence.

## The acceptance walk, re-recorded

`canary-acceptance-walk.json` [3] is a fresh walk taken on 2026-09-26. One canary Milestone was carried from its create verbs through `/dispatch`, `/integrate` and `/verify` to an acceptance taken on a sealed approval, in process, on a disposable canary provisioned under project code `W37CANARY`. The provider launcher was an in-process stand-in, so no provider process was started. The walk rewrote only this export's `canaries` and `milestones`; the recorder writes the directory of the release it is pointed at and leaves every other release's files as they were.

The dev4 membership reference is the walk's Milestone reference:

`eawf://WSP-W37CANARY/PRJ-W37CANARY/REP-W37CANARY/milestone/MLS-0001#MAB-0001-MLS-0001`

The export records it `COMPLETED`, inside the declared `W37CANARY` canary, with the bundle digest the sealed approval covers, `sha256:5ce927ea…4f07`. The reference string equals the dev3 one because the walk is deterministic in its identifiers; the bundle digest, delivered head and timestamps are this walk's own.

## References

| # | Reference |
|---|---|
| 1 | `.ea/artifacts/evidence/2026-09-26-dev4-conformance/native-canary-evidence.json` |
| 2 | `.ea/artifacts/evidence/2026-09-26-dev4-conformance/claude-code-driver-manifest.json` |
| 3 | `.ea/artifacts/evidence/2026-09-26-dev4-conformance/canary-acceptance-walk.json` |
| 4 | `src/eawf/workflow/evidence/provider_certification.py` (the reader) |
| 5 | `.ea/artifacts/evidence/2026-09-18-dev3-conformance/2026-09-18-dev3-conformance.md` (the carried-forward certification) |
| 6 | `src/eawf/runtime/runtimes/conformance.py` (the sole certification writer) |
| 7 | `tests/integration/workflow/release/test_dev3_canary_rehearsal_record.py` (the walk recorder and its checks) |

## Provenance

Produced on 2026-09-26. The export was derived from the dev3 export by setting `release_key` to `REL-0.7.0.dev4`, then the walk was recorded into this directory by running the recorder [7] with `EAWF_RECORD_CANARY_WALK=REL-0.7.0.dev4`. The same suite re-walks a fresh canary on every run and checks this directory's recorded steps, Milestone, reference and digests against it, alongside the dev3 directory's.

## Scrub

No absolute path, machine name, account identifier or credential appears in this directory. The canary was provisioned into scratch directories and removed; its location is not recorded, and the walk record says so in its `runtime_dir` field. The recorder's own check scans both JSON files for absolute-path shapes.
