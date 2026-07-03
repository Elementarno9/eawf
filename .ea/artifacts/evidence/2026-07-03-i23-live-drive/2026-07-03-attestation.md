# Operator attestation — I23 recorded live drive (2026-07-03)

## Summary

The recorded fleet drive ran against the hardened HEAD (all P30-I23 daemon-core waves landed: the lock split, mutation watchdog at the raised 900s ceiling, state-root guard, gate-executing clean close, verdict teeth), on a dedicated smoke-fixture daemon whose runtime dir and state bind are isolated from the development repo.

## Attested facts

- The smoke daemon was freshly spawned from the fixture cwd post-W09/W10/W52 (bind verified via the EP3 wrong-state-root refusal firing when the wrong daemon was addressed first); smoke daemon uptime=1132.1s at recording time.
- DriveParams caps were explicitly armed on every run: eu_cap=3.0, usd_cap=5.0, waves_cap=3 (run 1) / 1 (run 2), concurrency=1 — never an uncapped autopilot run.
- Zero manual interventions: no pkill, no lock-file removal, no hand-edited state; every mutation went through the daemon (the one FAILED wave was the verify gate refusing a malformed report, left terminal as recorded).
- Total live spend: 1.217 USD on claude-haiku-4-5 (canonical runtime label `claude`), under the 5 USD cap.
- Jail smoke: every lane spawned under the seatbelt wrapper (`jail=on wrapper='sandbox-exec'` markers in jail_smoke.log); lanes wrote files and ran `git commit` inside the jail (commits landed in the fixture repo).

## References

- summary.json — machine-readable run summary (validated by tools/validate_drive_recording.py)
- close_gates.log — the W19 gate-executing clean-close proof (run_close_gates passed=True, deterministic_evidence=1, twice)
- jail_smoke.log — seatbelt spawn markers
- watch_tail.txt — readable researcher/executor output tail
- dispatch_cost.jsonl — per-attempt priced rows

## Provenance

Recorded by the P30-I23-W33 drive session on 2026-07-03; source stores live in the smoke fixture (not committed); excerpts scrubbed of machine paths before promotion.

## Scrub

Machine paths replaced: home directory -> <home>, fixture repo -> <smoke-repo>, runtime dir -> <smoke-runtime>. No hostnames, emails, or tokens present.
