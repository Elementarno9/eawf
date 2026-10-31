# Limitations of eawf 0.7.0

This page lists what 0.7.0 does not do, what it defers to a later release, and which runtimes and operating systems have a certification. A requirement id, such as `RUN-031`, names one row of the eawf requirement packet; `RUN-031..039` names `RUN-031` to `RUN-039`. `.ea/requirements.json` holds each deferral row and the decision it cites.

## Deferred to 0.7.1

Decision D64 defers 24 requirement ids to 0.7.1: the external-host Run and compaction-accounting rows RUN-031..039, and the quality rows for comments, documents and memory retrieval QUAL-021..030 and QUAL-051..055. D64 is an epoch-1 decision imported into the state's decision ledger; its deferral row is recorded in `.ea/requirements.json`.

The multi-project console is also deferred to 0.7.1.

## Deferred to 0.8.0

Decision D93 is active: the operator ratified it on 2026-10-31. Decisions D89, D90 and D91, the earlier proposals of this deferral, are obsolete. D93 defers 80 requirement ids to 0.8.0 in its committed deferral row in `.ea/requirements.json`: the 68 ids the 0.7 re-plan dropped, 10 owned ids whose feature 0.7 does not build, and AUTH-014 and AUTH-037, whose check was retired before it was built, so no test can exercise them. The requirement census reports the 80 ids as deferred under D93. AUTH-009 and REL-022 have tests that exercise them, so they stay owned by 0.7.0 rather than deferred.

## Certified runtimes

Each runtime below passed the probe, canary and certify stages: claude-code on 2026-10-30 and codex on 2026-10-31. A version in the range is admitted only after it passes a new probe on the machine that runs it.

- `claude-code` (Claude Code) 2.1.314 to 2.1.999, certified at 2.1.314.
- `codex` (Codex CLI) 0.164.0 to 0.199.999, certified at 0.164.0.

The committed evidence export of the release, `.ea/artifacts/evidence/2026-10-30-stable-native-linux/native-canary-evidence.json`, records both certifications, claude-code 2.1.314 and codex 0.164.0, and both version ranges.

## Operating systems

eawf 0.7.0 is certified on Linux (x86_64) only. macOS is not certified: its certification run is an operator step that has not run yet. Windows is not certified.

CI runs the test suite on `ubuntu-24.04` and `macos-26`. A green macOS CI run shows that the tests pass; it is not a runtime certification.

A Windows CI job on a real Windows host runs the CLI smoke and the win32 named-pipe tests, which backs the release's Windows platform claim; it is not a runtime certification either.

The 0.7.0 release declares two platform claims: `linux-x86_64`, which is certified, and `windows-x86_64`, which is CI-tested only.

## Other limitations

- 0.7.0rc4 is the last release that migrates a 0.6 tree; see `eawf help upgrade-from-0.6`.
- The packet-run release gate needs four live runs on the machine that tags the release, because each run receipt is machine-local.
- The console shows the full transcript of a Run only in the raw drawer, which the `r` key opens.
- Transcripts and session bindings are machine-local: a clone of the repository does not get them.
