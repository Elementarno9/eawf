# Runtime certification

A Run's controls reach the harness process the Run drives, so `eawf run cancel`, `eawf run interrupt` and the console's Run controls are admitted only when the harness version the Run recorded holds a current certification. A refusal names the version and the gap, as one of `runtime_uncertified`, `runtime_certification_in_progress`, `runtime_quarantined`, `runtime_certification_expired`, `runtime_certification_not_verified` or `runtime_capability_uncertified`.

## Where certifications come from

- The committed native-canary exports under `.ea/artifacts/evidence/` certify the versions a release was cut on.
- This machine's daemon certifies the versions installed here. Each probe appends one row to `.ea/local/runtime_certification.jsonl`, which is machine-local and never committed: a certification describes the binary installed on this machine.

The newest record of a version decides. A probe that fails after an export certified the version quarantines it, and a later probe that passes lifts the quarantine.

## The probe

The probe is the conformance runner's probe stage. It runs the installed binary's `--version` and `--help` (and `codex features list` for Codex) and checks the advertised flags against the capability matrix. Each of those calls is bounded by a 10-second timeout. The stage record lands in the conformance journal under the version's tuple digest.

- A passed probe certifies the version for 90 days. Every capability the matrix has a probe rule for is recorded `verified` or `unsupported`. Install trust is `observed`, which never admits unattended dispatch.
- A failed probe quarantines the version, and the row lists each required capability the binary did not advertise.

The probe starts no session and calls no model, so it costs no tokens.

## Certifying by hand

```text
eawf runtime certify claude-code
eawf runtime certify codex
```

A certified version prints its expiry and exits 0. A quarantined version prints what the probe found missing and exits non-zero. A runtime with no binary on the daemon's `PATH` is refused with `runtime_not_installed`, and nothing is recorded.

## Automatic certification

When a Run starts, or a host session is adopted as one, on a version that has no current certification and no quarantine, the daemon queues a background probe of the installed binary. It probes each version at most once per daemon process and runs one probe at a time per tree. While the probe runs, a control on that version is refused with `runtime_certification_in_progress`. The probe reads the binary installed now, so a Run that reported an older version leaves that version uncertified.

Set `runtime.auto_certify: false` to turn the background probe off; `eawf runtime certify` still certifies on demand.
