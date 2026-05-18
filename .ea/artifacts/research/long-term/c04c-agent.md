# C04c — Agent entity (AgentReport, attempt_id, session_handle) — Eä framework long-term specs

**Cluster:** C04c (Agent entity — AgentReport, attempt_id, session_handle binding)
**Status:** `accepted` (split per Q19; 2026-05-18)
**Depends on:** C00 (V1..V9), C01 (foundations — Wave/AgentSession entities), C02 (daemon — session-handle tracking)
**Consumed by:** C04a, C04b, C07a

## 1. Purpose + scope statement

C04c owns the **Agent entity contract** — typed `AgentReportBody`, `attempt_id` semantics, and `session_handle` binding inside `Wave.sessions[attempt_id] -> SessionAttempt`.

**In scope.**

- `AgentReportBody` typed union per role (executor/reviewer/auditor/researcher/planner/polisher/domain-specialist).
- `attempt_id` integer counter; increments on retry; persisted on `Wave.sessions`.
- `session_handle` opaque handle (per XB05 / C02-I007 fix 2026-05-18) — never a raw filesystem path.
- Append-only report contract per AGENTS rule 19: never overwrite; retry appends next attempt for same `(role, base_id)` pair.
- Verdict enum `pass | pass-with-followups | fail | blocked` (AGENTS rule 19).

**Out of scope.**

- Plugin manifest for agent registration → C04b / C07a.
- Per-runtime session-log path catalog → C07a §5.4.

## 2. Goals + non-goals

- Every agent session terminates with a typed `agent_end` report.
- Append-only contract enforced at writer.
- `session_handle` opaque — daemon resolves to a real path on demand; the path never appears in state.json or event.jsonl.

## 3. Prior verdicts cited

V1, V8 from C00.

## 4. Decision matrix

| # | Axis | Recommendation | Rationale |
|---|---|---|---|
| **D-c1** | session_handle representation | **Opaque handle (blob-URN or daemon-side index key)** per XB05 | Original `session_log_path: str` could carry `<local-path>` paths; violates AGENTS rule 16. Opaque handle preserves the lookup capability without leaking host paths. |
| **D-c2** | AgentReport verdict enum | `pass | pass-with-followups | fail | blocked` (per AGENTS rule 19) | Locked at v0.3; no extension. |
| **D-c3** | attempt_id retry semantics | Monotonically increasing integer per `(wave_id, role, base_id)` | One counter per role × wave; never reused on rollback. |

## 5. Body

```python
from typing import Literal
from pydantic import BaseModel, ConfigDict

AgentVerdict = Literal["pass", "pass-with-followups", "fail", "blocked"]

class AgentReportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    role: Literal["executor", "reviewer", "auditor", "researcher", "planner", "polisher", "domain-specialist"]
    verdict: AgentVerdict
    summary: str
    findings: list[str] = []
    next_actions: list[str] = []

class SessionAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt: int                          # monotonic per (wave_id, role, base_id)
    runtime: str
    session_id: str                       # runtime-specific id
    session_handle: str                   # opaque per XB05 — NEVER a raw filesystem path
    started_at: str                       # ISO-8601 UTC
    ended_at: str | None = None
```

## 6. Failure modes

- `F-c01` Report verdict outside the 4-value set → `ValidationError`.
- `F-c02` Attempt counter reset → `LifecycleError` (append-only invariant violated).
- `F-c03` session_handle leaks a raw filesystem path on write → scrub validator rejects at envelope emit.

## 7. Migration plan

Existing `SessionAttempt.session_log_path: str` → `session_handle: str` (opaque). Migration writer:
- Reads existing rows.
- Hashes the path → blob URN.
- Daemon stores `path ↔ handle` lookup internally; CLI/TUI/event-log never see the path.

## 8. Open questions

- Q-c1 — Capability extension for v0.5+ Principal model (XB08 follow-on).

## 9. References

[1] Parent C04 `2026-05-16-c04-workflow-skills.md`.
[2] C01 `2026-05-16-c01-foundations.md` §5.3.5 (Wave + AgentSession).
[3] C02 `2026-05-16-c02-daemon-topology.md` §5.13 (session-handle tracking).
[4] AGENTS.md rule 19 — typed agent reports.

## 10. Provenance + Scrub

### Provenance

- `store_record=none (local-only research brief)`
- `commit=3b86f7a (parent)`
- `cluster=C04c`
- `consumes=C00..C02, C04`
- `supersedes=none`
- `session=eawf-spec-c04c-agent-2026-05-18`
- `last_revised=2026-05-18`
- `audit_consumed=2026-05-17-spec-series-combined-audit.md`
- `authority_binding=Q1 (2026-05-18): daemon = sole writer for AgentReport store + Wave.sessions map.`

### Scrub

- status: clean
- references: repo-relative only
- local paths: none
- real emails: none
