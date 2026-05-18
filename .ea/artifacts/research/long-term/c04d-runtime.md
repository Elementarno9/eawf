# C04d — Runtime integration (cross-refs C07a) — Eä framework long-term specs

**Cluster:** C04d (Runtime integration — how workflow commands dispatch to runtime adapters)
**Status:** `accepted` (split per Q19; 2026-05-18)
**Depends on:** C00 (V1..V9), C01 (foundations), C02 (daemon), C04 (parent), C07a (runtime adapters)
**Consumed by:** C05 (CLI), C06 (TUI)

## 1. Purpose + scope statement

C04d is the **runtime-integration sub-cluster** — names how workflow commands (C04a) dispatch to runtime adapters (C07a) via the daemon (C02). Cross-references C07a §5.1 RuntimeAdapter Protocol; this brief only specifies the C04 ⇄ C07a boundary.

**In scope.**

- Skill ⇄ adapter handshake (per-skill manifest `runtime: [list]` consulted; daemon picks the highest-preference healthy adapter).
- `dispatch.session_policy` propagation from skill manifest → daemon `agent.dispatch` RPC → adapter session-handle resolver.
- Runtime fallback ladder integration with V5 reactive switchover.
- Per-runtime cache-control hook propagation (C07a §5.6).

**Out of scope.**

- Per-runtime adapter implementation → C07a.
- Skill body algorithm → C04a / C04b.
- TUI rendering of dispatch state → C06.

## 2. Goals + non-goals

- Workflow skill → daemon → adapter contract is fully typed.
- No skill body knows which runtime it ran on (adapter abstraction).
- Runtime fallback events propagate back to skill envelope as needed for retry semantics.

## 3. Prior verdicts cited

V1, V5, V8, V9 from C00.

## 4. Decision matrix

Inherits all C04 + C07a decisions. C04d-specific:

| # | Axis | Recommendation | Rationale |
|---|---|---|---|
| **D-d1** | Skill picks runtime? | **No — skill emits envelope; daemon picks runtime per V5 ladder** | Single point of policy. Skill manifest declares compatible runtimes; daemon picks the healthy one. |
| **D-d2** | Cache-control marker injection | **Adapter layer** (per C07a D4) | Skill body never sees cache markers; adapter injects per-runtime convention. |
| **D-d3** | Runtime switch mid-skill | **Forbidden in v0.3-v0.5; switchover happens between attempts** | Mid-skill switch corrupts session state. New attempt starts on new runtime per V5. |

## 5. Body

Canonical contract lives in C04 + C07a. C04d documents the interaction patterns:

- Skill emit → `OutputEnvelope` with `dispatch_metadata.session_policy` set per manifest.
- Daemon receives via `agent.dispatch` RPC.
- Daemon consults Wave.sessions[attempt_id] for hybrid session reuse.
- On runtime error: V5 fallback emits `runtime_switched` event; daemon issues a new dispatch envelope with `idempotency_key` preserved.

## 6. Failure modes

- `F-d01` Skill envelope cites runtime not in skill manifest `runtime:` list → daemon refuses with `ValidationError`.
- `F-d02` Adapter session-handle resolution fails mid-dispatch → fall through to fresh per V8 with `DispatchAnnotation`.

## 7. Migration plan

None — this is a contract-only sub-cluster. Implementations live in C04a (skill body) + C07a (adapter).

## 8. Open questions

- Q-d1 — Cross-runtime fanout (one skill dispatched to multiple runtimes in parallel) deferred to v0.5+ per blitz round 4 [C07a:25].

## 9. References

[1] Parent C04 `2026-05-16-c04-workflow-skills.md`.
[2] C02 `2026-05-16-c02-daemon-topology.md` §5.12 (runtime fallback state machine).
[3] C07a `2026-05-16-c07a-runtime-skill-dispatch.md` §5.1 (RuntimeAdapter Protocol).

## 10. Provenance + Scrub

### Provenance

- `store_record=none (local-only research brief)`
- `commit=3b86f7a (parent)`
- `cluster=C04d`
- `consumes=C00..C02, C04, C07a`
- `supersedes=none`
- `session=eawf-spec-c04d-runtime-2026-05-18`
- `last_revised=2026-05-18`
- `audit_consumed=2026-05-17-spec-series-combined-audit.md`
- `authority_binding=Q1 (2026-05-18): daemon = sole dispatcher; skills emit envelopes, daemon routes.`

### Scrub

- status: clean
- references: repo-relative only
- local paths: none
- real emails: none
