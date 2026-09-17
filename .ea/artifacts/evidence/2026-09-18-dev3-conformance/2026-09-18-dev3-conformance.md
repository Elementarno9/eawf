# Native-canary conformance evidence for 0.7.0.dev3

## Summary

This directory holds the `REL-0.7.0.dev3` native-canary evidence export: the conformance runner's records for one real runtime tuple installed on the producing host, plus the production-root digest pair taken across a real canary provisioning. `native-canary-evidence.json` is what the `provider` and `membership` readiness rows read, through `eawf.workflow.evidence.provider_certification`.

The export opens the `PRX-050` cycle. One tuple carries contiguous `probe` then `canary` then `certify` stage history written by `eawf.runtime.runtimes.conformance.ConformanceRunner`, which is the sole writer of a `DriverCertification`, and the dev3 readiness cites that record by the URN `certification://claude-code/2026-09-18`.

## The certified tuple

| Field | Value |
|---|---|
| Runtime | `claude-code` |
| Distribution version | `2.1.274` |
| Manifest reference | `driver://claude-code/2.1.274` |
| Manifest digest | `sha256:9f3a95bc…e0e7` |
| Platform | `macos` / `aarch64` |
| Install trust | `observed` |
| Certification id | `claude-code-dev3` |
| Stage history | `probe`, `canary`, `certify`, all `passed` |

`claude-code-driver-manifest.json` is the observation the manifest digest is taken over, so a reviewer recomputes it rather than trusting it. Its `observed_flags` are the rule tokens the packaged capability matrix consults, as they literally appeared in the installed binary's `--help` output on the producing host.

Two capabilities are advertised and certified `verified`: `tool_use` (the binary advertises `--allowedTools` / `--allowed-tools`) and `streaming` (`--output-format`). A third row, `session_resume`, is recorded `unsupported`: the binary advertises `--continue`, `--session-id` and `--resume`, but no runtime adapter implements a resume spawn, so the matrix declares it unsupported and the tuple does not advertise it.

## What is real here, and what is not

The probe stage is a live observation: the installed binary was executed and its advertised surface confronted with the declared matrix cells.

The canary stage graded a declared containment outcome per attempt rather than a dispatched canary Run. The native Run path is not delivered yet, so no canary Run existed to dispatch; the ten containment attempts are recorded as denied because that is the contract the stage grades, not because a Run reported them. `install_trust` is therefore `observed` and not `managed`, which is the axis that refuses this certification for unattended dispatch.

`milestones` is empty. No canary Milestone has been accepted, so the `membership` row stays red for any dev3 record that declares an acceptance bundle. Recording a Milestone nobody completed is exactly the failure the row exists to catch.

## Production-root isolation

`isolation` records two digests over a staged production root: the repository's committed `.ea` tree at `HEAD`, extracted into a scratch directory. Between the two digests a real disposable canary was provisioned under project code `W63CANARY`, its tree resolved to authority epoch 2, and the staged production root was offered to `require_native_authority` and refused with `NativeAuthorityRequiredError`. The pair is equal.

The record names its own depth in `rehearsal_scope`: canary provisioning and the native-authority fence, not a whole native Milestone. The `canary_isolation` gate's full rehearsal -- a Milestone driven end to end inside a disposable repository -- lands with the rehearsal wave, and the proof command that settles the gate still names a test file that does not exist yet.

## References

| # | Reference |
|---|---|
| 1 | `.ea/artifacts/evidence/2026-09-18-dev3-conformance/native-canary-evidence.json` |
| 2 | `.ea/artifacts/evidence/2026-09-18-dev3-conformance/claude-code-driver-manifest.json` |
| 3 | `src/eawf/runtime/runtimes/conformance.py` (the sole writer) |
| 4 | `src/eawf/workflow/evidence/provider_certification.py` (the reader) |
| 5 | `src/eawf/runtime/runtimes/capabilities.yaml` (the declared matrix) |

## Provenance

Produced on 2026-09-18 by wave `P33-I01-W63`, in the wave's own worktree, by driving `ConformanceRunner` in process over the live `claude` binary on the producing host. Stage timestamps are pinned to `2026-09-18T00:00:00+00:00` so the export is a function of its inputs rather than of the minute it was written.

## Scrub

No absolute path, machine name, account identifier or credential appears in this directory. The canary and the staged production root were provisioned into scratch directories and removed; neither their locations nor the installed binary's location is recorded. The recorded digests are over file contents and repo-relative locators only.
