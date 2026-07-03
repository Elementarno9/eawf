# I23 pre-ship multi-agent review (P30-I23-W35)

## Summary

Three fresh-context reviewers examined the phase diff (origin/main..HEAD, ~50 I23 commits) through independent lenses — daemon close-path correctness, verifier integrity, and v0.6.0 release preconditions. One blocker chain was found and FIXED before this artifact closed: a routine refused close left a dangling released lock handle on the daemon context, and the watchdog's next heartbeat on that closed handle raised an unsuppressed ValueError that killed the watchdog task permanently (disarming the ZD-R6 hard-abort net). The fix rides the same iter: the WAVE_CLOSE lock block clears the handle in a finally, the watchdog heartbeat suppress broadened, both pinned by regression tests [1]. Two further review-driven tightenings landed inline: the W49 evidence-refs rung now demands one ref per typed criterion (the DoD contract, not merely non-empty), and the drive-recording validator machine-verifies the gate-executing-close claim instead of accepting it attested [2]. The release-precondition majors were also fixed inline: the bare `eawf migrate` default target advanced 1.12 → 1.14 (it was leaving two registered edges unreachable and shipping live state two edges behind the declared schema), the live state migrated to 1.14, a guard test pins default-target == model-max, and three re-shipped audit artifacts were scrubbed of machine home paths [3].

**verdict: pass-with-followups**

All blockers resolved on this iter; the remaining findings are followup-sized and carried below.

## Findings disposition

| # | Lens | Finding | Severity | Disposition |
|---|------|---------|----------|-------------|
| 1 | daemon | dangling `active_lock_handle` after refused close kills the watchdog heartbeat | blocker | FIXED this iter (finally-clear + broad suppress + 2 regression tests) |
| 2 | daemon | verdict/jury tier runs UNDER the state lock (multi-minute holds on verdict closes) | major | FOLLOWUP → P31 (move the spawn off-lock into pre-flight) |
| 3 | daemon | cancelled spawns orphan the juror process group past a watchdog abort | minor | FOLLOWUP → P31 (reap on CancelledError in all three adapters) |
| 4 | daemon | watchdog abort surfaces as a bare dropped connection, shielded future unconsumed | minor | FOLLOWUP → P31 (typed error frame or documented poll-only contract) |
| 5 | daemon | optimistic re-check covers only the target wave row; EU/evidence computed pre-flight | minor | FOLLOWUP → P31 (recompute under lock or widen the stale scope) |
| 6 | verifier | fleet clean-close skips the verdict-always auditor gate (daemon-vs-autopilot parity gap) | major | FOLLOWUP → P31 (gate `_close_wave_on_disk` on `verdict_requirement`) |
| 7 | verifier | `worktree wave land` closes without the verdict tier | major | FOLLOWUP → P31 (same parity fix) |
| 8 | verifier | `--no-runtime` over-waives four gates under one runtime-labelled flag on the daemonless path | major | FOLLOWUP → P31 (split the waiver; record the actually-bypassed gate) |
| 9 | verifier | W49 rung 4 accepted any non-empty refs list | major | FIXED this iter (per-criterion count + refusal test) |
| 10 | verifier | rung-4 teeth bit fails open on profile-load errors | minor | ACCEPTED (advisory-by-construction; logged) |
| 11 | verifier | drive-recording validator did not machine-check the gate-executing claim | minor | FIXED this iter (assertion 8) |
| 12 | verifier | fork APPROVE_CLOSE closes without an evidence row naming the overridden gate | minor | FOLLOWUP → P31 |
| 13 | release | `eawf migrate` default target trailed the model max (1.12 vs 1.14) | major | FIXED this iter (default + guard test) |
| 14 | release | live state shipped at schema 1.12 vs the declared 1.14 | major | FIXED this iter (migrated + committed) |
| 15 | release | three re-shipped audit artifacts leaked machine home paths | minor | FIXED this iter (forward-scrubbed) |
| 16 | release | migration-note instruction relied on the broken default | minor | FIXED by #13 |

## Re-close preconditions (release lens, verified clean)

CHANGELOG 0.6.0 section complete with all six schema-edge migration notes; `__version__ = 0.6.0` advances past v0.5.4 and matches the workflow's verify-version step; the drafted phase-close subject passes the ship validator, the workflow extraction regex, and the commit-prefix lint byte-compatibly; no new PII in the phase range.

## References

| N | Reference |
|---|-----------|
| 1 | `src/eawf/runtime/daemon/methods/state.py` (finally-clear), `src/eawf/runtime/daemon/main.py` (broad suppress), `tests/daemon/test_mutation_watchdog.py`, `tests/daemon/test_close_lock_split.py` |
| 2 | `src/eawf/workflow/verify/dispatch_close.py` (rung 4), `tools/validate_drive_recording.py` (assertion 8) |
| 3 | `src/eawf/kernel/migrations/_base.py`, `tests/unit/test_migrate.py`, `.ea/artifacts/audits/A17/A18/A28-*-ship-gate.md` |
| 4 | Reviewer reports persisted via the auditor_report store row for P30-I23-W35 |

## Provenance

Synthesized 2026-07-03 by the P30-I23-W35 session from three independent fresh-context reviewer reports (daemon close-path, verifier integrity, release preconditions); reviewers had no access to the parent conversation.

## Scrub

Repo-relative references only; no machine paths, hostnames, or PII.
