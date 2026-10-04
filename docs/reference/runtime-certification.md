# Runtime certification

A Run's controls reach the harness process the Run drives, so a steer, an answer, a resume, a fork or a retry is admitted only when the harness version the Run recorded holds a current certification. A refusal names the version and the gap, as one of `runtime_uncertified`, `runtime_version_not_recorded`, `runtime_certification_in_progress`, `runtime_quarantined`, `runtime_certification_expired`, `runtime_certification_not_verified` or `runtime_capability_uncertified`.

A stop is never refused: `eawf run cancel`, `eawf run interrupt`, `eawf run reconcile` and the console's stop controls are admitted whatever the certification says, so a runaway Run can always be stopped. When the runtime has a gap, the answer carries a warning naming it.

## Where certifications come from

- The native-canary exports committed under eawf's own `.ea/artifacts/evidence/` certify the versions eawf's releases were cut on, for eawf's repository only. They do not ship with the package, so a user repository starts with no certifications.
- This machine's daemon certifies the versions installed here, through the automatic probe below or `eawf runtime certify`; this is how a user repository's versions get certified. Each probe appends one row to `.ea/local/runtime_certification.jsonl`, which is machine-local and never committed: a certification describes the binary installed on this machine.

The newest record of a version decides. A probe that fails after an export certified the version quarantines it, and a later probe that passes lifts the quarantine.

## The probe

The probe is the conformance runner's probe stage. It runs the installed binary's `--version` and `--help` (and `codex features list` for Codex) and checks the advertised flags against the capability matrix. Each of those calls is bounded by a 10-second timeout. The stage record lands in the conformance journal under the version's tuple digest.

- A passed probe certifies the version for 90 days. Every capability the matrix has a probe rule for is recorded `verified` or `unsupported`. Install trust is `observed`, which never admits unattended dispatch.
- A failed probe quarantines the version, and the row lists each required capability the binary did not advertise.
- A probe that could not read the binary records nothing: a `--version` or `--help` call that timed out, could not be spawned or printed no help is refused with `runtime_not_installed` or `runtime_probe_incomplete`, so a slow machine never quarantines a working harness.

The probe starts no session and calls no model, so it costs no tokens.

## Certifying by hand

```text
eawf runtime certify claude-code
eawf runtime certify codex
```

A certified version prints its expiry and exits 0. A quarantined version prints what the probe found missing and exits non-zero. A runtime with no binary on the daemon's `PATH` is refused with `runtime_not_installed`, and nothing is recorded.

## Automatic certification

When a Run starts, or a host session is adopted as one, on a version that has no current certification, or whose quarantine is at least 24 hours old, the daemon queues a background probe of the installed binary. It queues a version again only after the same 24 hours, or at once when the last probe recorded nothing, and runs one probe at a time per tree. While the probe runs, a control other than a stop is refused with `runtime_certification_in_progress`. A failure of the probe or its trigger is logged and never stops the Run from starting. The probe reads the binary installed now, so a Run that reported an older version leaves that version uncertified.

Set `runtime.auto_certify: false` to turn the background probe off; `eawf runtime certify` still certifies on demand.
