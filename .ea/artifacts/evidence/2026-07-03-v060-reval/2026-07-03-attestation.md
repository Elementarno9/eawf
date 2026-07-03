# Operator attestation — v0.6.0 pre-re-close live revalidation (2026-07-03)

## Summary

The W33 recorded drive validated the close/verify path BEFORE the W35 pre-ship blocker fix landed (the WAVE_CLOSE lock-handle clear, the broad watchdog-heartbeat suppress, and the rung-4 one-evidence_ref-per-typed-criterion tightening all changed `daemon/methods/state.py` + `workflow/verify/dispatch_close.py` after that recording). This revalidation re-ran the capped live e2e at the re-close HEAD (`9347b8a0`) on the external smoke fixture, covering both vendors, before P30-I21-W22 executes the v0.6.0 phase re-close [1][2].

## Attested facts

- **Claude autopilot** — two fresh gated waves (fixture P01-I01-W05 + W06) armed via `fleet.drive` (caps: eu 3.0 / usd 5.0 / waves 2, concurrency 1); both CLOSED through the W19 gate-executing clean path (`run_close_gates passed=True deterministic_evidence=1`, twice); lanes spawned under the seatbelt jail (`jail=on wrapper='sandbox-exec'`) and committed from inside it.
- **Codex autopilot** — one fresh gated wave (fixture P01-I01-W07) on runtime `codex`, auto-routed to `gpt-5.3-codex-spark`; CLOSED through the same gate-executing path; jailed spawn committed from inside the jail; one lane respawn absorbed by `spawn_with_retry` without operator intervention. First codex live traversal of the full I23 daemon core (lock split, 900s watchdog, state-root guard, W52 response shield) and of the tightened rung 4.
- **Research campaigns** — two campaigns run to `terminal=converged` (rounds=12, halt=round_budget; claims 39 and 110) through the live researcher spawn path at the same HEAD.
- **Researcher runtime note** — researcher lanes route to the claude lane by the typed routing table (`DEFAULT_ROUTING_TABLE` pins `runtime="claude"` for every role/effort pair); `runtime.preference` steers executor autopilot lanes only. A codex-driven researcher is therefore not a v0.6.0 surface; carried as a P31 note.
- **EU capture** — every closed lane carried `eu > 0` with harness + model provenance (`_finish_lane eu=0.0185 / 0.0202 / 0.0229`); `runtime_latest` rows populated with tokens + cost.
- **Spend** — 2.277 USD total across 27 priced sessions (claude-haiku-4-5: 2.166 USD; gpt-5.3-codex-spark: 0.111 USD), under the 5 USD cap on every armed run.
- **Zero manual interventions** — no pkill, no lock-file removal, no hand-edited state; the dedicated smoke daemon started and stopped cleanly (`drained=True`) twice.

## References

| N | Reference |
|---|-----------|
| 1 | `.ea/artifacts/evidence/2026-07-03-i23-live-drive/2026-07-03-attestation.md` (the pre-W35 recording this revalidates) |
| 2 | `.ea/artifacts/plans/2026-07-03-v0.6.0-reclose-runbook.md` (the re-close this gates) |

## Provenance

Recorded by the 2026-07-03 pre-re-close /flow session against HEAD `9347b8a0`; source stores live in the smoke fixture (not committed); facts quoted from the dedicated smoke-daemon session logs.

## Scrub

Machine paths withheld: fixture repo -> <smoke-repo>, runtime dir -> <smoke-runtime>. No hostnames, emails, or tokens present.
