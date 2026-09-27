# Incident — 0.7.0.dev4 published to four targets while its Release record stranded at candidate

## Summary

The `v0.7.0.dev4` tag was pushed on 2026-09-26 and the publication pipeline shipped the version to all four configured targets (PyPI, npm, the source-host release and the plugins-dist branch), but the Release record `REL-0.7.0.dev4` never left `candidate` [1]. The version is spent in the world and was not settled in the model, so the train could not open its successor: `eawf release pipeline 0.7.0.dev5` refused at its `create` step with `predecessor_live`, because a successor opens only over a `baked`, `cancelled`, `partially_released` or `released` predecessor [2].

Verdict: **functional impact none, process impact medium.** Every target holds exactly the frozen dev4 artifact set, so no consumer received a wrong build; the defect is that the record could not describe what happened.

### What is live

A read-back on 2026-09-27 through the shipped observation adapters, compared against the frozen dev4 manifest (`sha256:11c56460…`, the digest the record pins), found every leg matching [3]:

| Target | Holds | Matches the frozen manifest |
| --- | --- | --- |
| PyPI `eawf` | `0.7.0.dev4` wheel and source distribution, uploaded 2026-09-26T07:13Z | Yes |
| npm `@elementarno/eawf` | `0.7.0-dev.4`, published 2026-09-26T07:14Z; `latest` stays `0.6.8` | Yes |
| Source-host release `v0.7.0.dev4` | prerelease with the plugin bundle, release notes and checksums | Yes |
| plugins-dist branch | `versions/0.7.0.dev4`, committed 2026-09-26T07:11Z | Yes |

The tag `v0.7.0.dev4` resolves to `9b59f041`, the record's pinned source.

### Root cause

1. **A proof gate could never be receipted at the pinned source.** The serial `epoch1_stabilization` gate failed at `9b59f041` on a test-isolation bug (backlog B192), fixed later in `5c6c2b5b`. A gate proof is bound to the pinned source, so no rerun at that source could turn it green, and the record could not be approved.
2. **Approval happens after the tag.** The pipeline pushes the tag and waits for publication before it creates and approves the record, and `release tag --push` refuses once `release create` has dirtied the tree. So publication had already happened by the time the gate blocked approval (backlog B194).
3. **No reachable terminal edge fit.** `burn` needs an open publication operation, which an unapproved record never has, and `cancel` is refused because the publication touched registries.

### Disposition

The version is spent and is superseded by 0.7.0.dev5. `REL-0.7.0.dev4` is settled by the shipped adoption route: `eawf release adopt` records the four independent read-backs above against this incident, and `eawf release burn` then moves the adopted record to the terminal `partially_released`. No approval is asserted and no gate is waived; the adoption model forbids an adoption beside an approval reference [4].

### Follow-ups this incident does not close

- The pipeline still tags and publishes before the gates that approval depends on have been receipted at the pinned source (B194); a gate that cannot pass at that source should stop the pipeline before `tag`.
- Under the epoch-2 tree `eawf incident open` is refused (`legacy_operation_removed`) and `eawf record append` accepts no incident kind, so this incident has no typed row; the adoption cites this artifact instead.

## References

| Ref | What it anchors |
| --- | --- |
| [1] `eawf release show 0.7.0.dev4` | The record at `candidate`, revision 1, before settlement |
| [2] `src/eawf/workflow/verify/checkpoint_succession.py` | The successor-opening guard behind `predecessor_live` |
| [3] `.ea/artifacts/evidence/2026-09-27-dev4-settlement/` | The adapter observations, the frozen manifest, the adoption and the GitHub read-back; the raw registry documents stay machine-local |
| [4] `src/eawf/workflow/release/adoption.py` | `adopt_publication`, the adoption route for a draft or candidate |

## Provenance

Compiled 2026-09-27 from read-only inspection of the four publication targets and the dev4 and dev5 pipeline journals. No write was made to any publication target.

## Scrub

Checked for absolute paths, hostnames, emails and credentials; references are repo-relative.
