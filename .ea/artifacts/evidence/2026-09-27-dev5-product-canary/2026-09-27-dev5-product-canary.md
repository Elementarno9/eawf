# Product-canary evidence for 0.7.0.dev5

## Summary

This directory holds the evidence the `REL-0.7.0.dev5` product-canary rung is opened on. It carries the release's native-canary export [1], read by the `provider` and `membership` readiness rows through the evidence reader [4], and the canary-window receipt file [3], read by `release create` before it admits a product-canary rung [5].

The export follows the dev4 pattern [6]. The `certifications`, `advertised`, `run_logs` and `isolation` fields are copied unchanged from the dev4 export, which carried them forward from dev3: `claude-code` 2.1.274 on `macos` / `aarch64`, certified 2026-09-18 and expiring 2026-12-17. No runtime was probed for this directory. `claude-code-driver-manifest.json` [7] is a byte-for-byte copy, so the recorded digest can be recomputed here. The acceptance walk [2] was re-recorded on 2026-09-27 on a disposable canary under project code `W37CANARY`, and its Milestone reference is the membership bundle a dev5 create names: `eawf://WSP-W37CANARY/PRJ-W37CANARY/REP-W37CANARY/milestone/MLS-0001#MAB-0001-MLS-0001`.

The receipt file binds six pre-merge canary-window gates to evidence: `plan_revision_approved` to the native roadmap plans and the plan-apply suite, `parallel_dispatch` to the walk and the dispatch suite, `exact_head_integration` to the real-daemon seal suite, `real_diff_review` to the audit-and-review suite, `milestone_accepted` to the export and the acceptance suite, and `migration_rerun_identical` to this repository's committed live cutover record [8]. Read at merge, it supports "release-ready pending post-merge observation": `release_tagged_observed` is not filed, because only a pushed tag can earn it. Live references are appended to a receipt's `live_refs` as data: the plan receipt carries the pending actions `ACT-0101`, `ACT-0201` and `ACT-0301`, which the operator answered to approve the rc1, rc2 and stable plan revisions `PRV-0101`, `PRV-0201` and `PRV-0301` before applying them.

## References

| # | Reference |
|---|---|
| 1 | `.ea/artifacts/evidence/2026-09-27-dev5-product-canary/native-canary-evidence.json` |
| 2 | `.ea/artifacts/evidence/2026-09-27-dev5-product-canary/canary-acceptance-walk.json` |
| 3 | `.ea/artifacts/evidence/2026-09-27-dev5-product-canary/product-canary-receipts.json` |
| 4 | `src/eawf/workflow/evidence/provider_certification.py` (the export reader) |
| 5 | `src/eawf/workflow/release/canary_receipts.py` (the receipt reader and verdict) |
| 6 | `.ea/artifacts/evidence/2026-09-26-dev4-conformance/2026-09-26-dev4-conformance.md` |
| 7 | `.ea/artifacts/evidence/2026-09-27-dev5-product-canary/claude-code-driver-manifest.json` |
| 8 | `.ea/artifacts/evidence/2026-09-dev5-live-cutover/live-cutover.json` |
| 9 | `tests/integration/workflow/release/test_dev3_canary_rehearsal_record.py` (the walk recorder) |
| 10 | `tests/integration/workflow/release/test_dev5_ready_record.py` (the receipt checks) |

## Provenance

Produced on 2026-09-27. The export was derived from the dev4 export by setting `release_key` to `REL-0.7.0.dev5`, then the walk was recorded into this directory by running the recorder [9] with `EAWF_RECORD_CANARY_WALK=REL-0.7.0.dev5`. The receipt file was authored by hand from the evidence it names, and its checks [10] confirm every repository path it cites is committed and that removing any one pre-merge receipt drops the verdict to not ready.

## Scrub

No absolute path, machine name, account identifier or credential appears in this directory. The canary was provisioned into scratch directories and removed, and the receipt model refuses an absolute or parent-relative evidence path.
