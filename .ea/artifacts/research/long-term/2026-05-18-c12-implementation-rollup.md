# C12 — Implementation rollup (per-cluster EU + DAG) — Eä framework long-term specs

**Cluster:** C12 (Implementation rollup — Q4 deliverable; per-cluster EU + dependency DAG for the v0.3-v0.5 implementation envelope)

**Status:** `accepted` (per Q4 / BOT-05 closes 2026-05-18)

**Closes:** Q4 (implementation-phase EU envelope disclosure), BOT-05 (implementation-phase EU undisclosed in every cluster brief).

## 1. Purpose

Per operator Q4 (2026-05-18) + audit BOT-05: every cluster brief omitted its implementation-phase EU envelope, so the operator could not see the v0.3-v0.5 implementation cost upfront. C12 rolls up per-cluster EU estimates + cross-cluster dependency DAG so the operator funds the implementation phase with full visibility.

## 2. EU envelope rollup

| Cluster | Implementation EU | Wave count (est.) | Notes |
|---------|------------------:|------------------:|-------|
| C01 | 8-12 | 2-3 | URN_KINDS expansion + persona authority + lifecycle DAGs + minimum Principal model (Q3) + 16 glossary terms (BOT-02) |
| C02 | 40-50 | 8-10 | daemon scaffolding + IPC + outcome-WAL (Q10) + per-OS service + supervisor + Windows pywin32 thread bridge (Q8) + SCM-asyncio bridge (XB14) + 3 per-OS peer-cred recipes (XB13) |
| C03 | 25-30 | 5-7 | 7 Pydantic schemas + audit-DSL `verify-implements` kind + verb expansion + URN_KINDS hard precondition |
| C04 (split into 4) | 30-40 | 8-12 | 17 skills total (6 missing landed inline per Q9); C04a workflow + C04b skills + C04c agent + C04d runtime each ratifies independently |
| C05 | 18-23 | 4-6 | ~30 new verbs + completion + ErrorEnvelope shape + exit-code old-to-new table + **split god files per Q25 LOC cap** (+3 EU) |
| C06 | 25-35 | 5-7 | tui/ refactor on Textual (Q6) + widget catalog + reactive + Pilot snapshot + asciinema |
| C07a | 20-25 | 4-6 | per-runtime adapter (claude-code/codex/opencode) + plugin sync/doctor + capability matrix 8×3 + KISS-001/KISS-004 (Q23) |
| C07b | 15-20 | 3-5 | commit-prefix lint + glyph + worktree (Q13: `.ea/worktrees/`) + multi-repo + event-log + KISS-007 import cycle (Q23) |
| C08 | 13-18 | 3-5 | layered config + composition + bootstrap + **3 profiles** (Q24 trim from 5) + field registry (−2 EU) |
| C09 | 27-32 | 6-8 | hooks + Pilot snapshot + telemetry projector + telemetry-prototype vendor + bench + Q17 macOS-every-PR + KISS-006 indexed validation + Q25 EAWF010 LOC-cap lint (+2 EU) |
| C10 | 15-20 | 3-5 | docs IA + mkdocs + migration tooling + EU calibration + release |
| ~~C11~~ | ~~14-18~~ | ~~3-5~~ | **C11-IMPL DROPPED from v0.3-v0.5 per operator decision 2026-05-18 post-blitz.** gh CLI shell-outs in skill bodies stay as current pattern. C11 cluster spec stays ratified for v0.4+ when WriteRetryPolicy/keyring/polling/doctor demand surfaces. −14-18 EU. |
| **Total** | **236-306** | **51-74** | EU bucket calibration: 1 EU ≈ 25-30 min focused work with agent throughput; XL task = 5-8 EU = 2-4h. Aggregate ~120-150 hours focused work; calendar ~1-4 months at operator pacing (2-10 EU/day). Audit's "12-18 months" estimate carried traditional-developer calibration and is superseded 2026-05-18. |

**Stage-0 normalization (Stage 0 of audit):** ~38 EU (this revision pass).
**Stream-1 R-version revisions:** ~25-35 EU (in-place revisions; this iteration).
**Implementation envelope:** ~248-322 EU (above table).
**Spec phase already paid:** ~90 EU.
**Total v0.3-v0.5 effort:** ~365-470 EU.

## 3. Dependency DAG (implementation order)

Implementation waves open in this order (DAG arrows = "blocked-until"):

```text
C01-IMPL (URN_KINDS + Principal min model + lifecycle)
   ↓
C02-IMPL (daemon scaffolding + outcome-WAL + per-OS service + IPC)
   ↓
   ├─→ C03-IMPL (spec infrastructure; URN_KINDS hard precondition)
   ├─→ C07b-IMPL (event store + worktree + canonical Event model per Q14)
   ├─→ C08-IMPL (layered config + profile composition + field registry)
   │
C07a-IMPL (runtime adapter; depends on C02 daemon + C07b event model)
   ↓
C05-IMPL (CLI surface as dispatch-only; depends on C02 + C07a)
   ↓
   ├─→ C04a-IMPL (workflow commands)
   │   C04b-IMPL (skill manifests + 6 missing skills)
   │   C04c-IMPL (agent entity)
   │   C04d-IMPL (runtime integration)
   │
C06-IMPL (Textual TUI; depends on C02 event subscribe + C05 verbs + C07b Event model)
   ↓
C09-IMPL (telemetry projector + Q17 CI matrix)
   ↓
C10-IMPL (migration tooling + docs + release)
   ↓
C11-IMPL (GitHub bridge with Q15 local polling; HMAC + show-secret-removal landed)
```

## 4. Per-cluster wave plan sketches

Skeleton plans (the per-cluster implementation phase will produce real WaveSpec bodies per C03):

### C01-IMPL (8-12 EU; 2-3 waves)

- W01 — URN_KINDS expansion to 26 (XB15) + golden fixture + backward-compat aliases
- W02 — Minimum Principal model (Q3) + `Cost.attributed_to` placeholder + EventPayload.actor_principal_id placeholder
- W03 — Lifecycle DAG migration helpers (for downstream entities)

### C02-IMPL (40-50 EU; 8-10 waves)

- W01 — Daemon process scaffolding (asyncio loop + JSON-RPC framing)
- W02 — Windows pywin32 named-pipe + queue bridge (Q8)
- W03 — Outcome-WAL implementation (Q10) + replay
- W04 — Per-OS service registration (systemd / launchd / pywin32 service + SCM-asyncio bridge XB14)
- W05 — Per-OS peer-cred (XB13: Linux/macOS/FreeBSD recipes)
- W06 — Subscription bus + drop-oldest backpressure (D7 revised)
- W07 — Session-handle tracking (V8) with opaque handles (XB05)
- W08 — Idle-timeout + spawn model + cold-spawn benchmark
- W09 — Migration: state-CLI → daemon proxy (per authority map)
- W10 — Migration: layered-config + registry writers into daemon (per authority map)

### C03-IMPL (25-30 EU; 5-7 waves)

- W01 — PhaseSpec + IterSpec + WaveSpec schemas
- W02 — `verify-implements` audit-DSL kind
- W03 — Spec writer (daemon-mediated)
- W04 — Migration backfill for closed waves
- W05 — KPI + non-empty + spec-path-grammar tests

### C04a-IMPL through C04d-IMPL (30-40 EU total; 8-12 waves)

Each sub-cluster claims its own waves per its brief.

### C05-IMPL (18-23 EU; 4-6 waves)

- W01 — Verb-noun matrix + `--plain` default + ErrorEnvelope shape + exit-code table
- W02 — Daemon-vs-daemonless escalation rules + raw RPC behind dev-mode gate
- W03 — Streaming output + completion install
- W04 — **Split god files per Q25 LOC cap (700)**: `lifecycle.py` (2596 LOC) → sub-modules per scope kind; `evidence.py` (1407 LOC) → per-DSL-kind split; `memory.py` (987 LOC) → per-tier split. Mandatory rationale comment + `# noqa: EAWF010` for any waiver. ~3-5 EU.

### C06-IMPL (25-35 EU; 5-7 waves)

- W01 — Textual EaApp + scope dispatch
- W02 — RoadmapTree + EUBar + StatusPane + GitPane widgets
- W03 — Modal stack + needs-user handshake
- W04 — Pilot snapshot harness + asciinema cast
- W05 — `/metrics` overlay + telemetry tile binding
- W06 — Web-stub WS bridge (stub-only per Codex C06-I005)
- W07 — Legacy `src/eawf/tui/` migration (per Codex C06-I010 — migration + state verdict)

### C07a-IMPL (20-25 EU; 4-6 waves)

- W01 — RuntimeAdapter Protocol + 3 adapters (claude-code/codex/opencode)
- W02 — PluginManifest(BaseModel) + plugin sync (`eawf plugin sync`) + **KISS-004 installer shared helpers extracted; module < 300 LOC (Q23)**
- W03 — Plugin doctor + 4 drift kinds enumeration + **KISS-001 coauthor env-detection contract fix (P0)**
- W04 — Capability matrix render 8×3
- W05 — SDK probe re-run post-2026-06-15 + V8 matrix update

### C07b-IMPL (15-20 EU; 3-5 waves)

- W01 — Commit-prefix lint + worktree subsystem at `.ea/worktrees/` + **KISS-007 worktree import cycle fix (Q23)**
- W02 — Event store + canonical Event model (per Q14)
- W03 — Multi-repo registry + scope dispatch ladder
- W04 — Render envelope + brand glyph + ASCII fallback

### C08-IMPL (13-18 EU; 3-5 waves)

- W01 — Layered config taxonomy + branch layer + wave layer
- W02 — ProfileBody v2 schema + contributes typed + composition loader
- W03 — **3 bootstrap templates** (research + engineering + reverse-engineering) + `eawf init --profiles` (Q24 trim from 5 templates; ~2 EU saved)
- W04 — Config schema migration runner

### C09-IMPL (27-32 EU; 6-8 waves)

- W01 — Hook inventory + pre-commit + pre-push
- W02 — Coverage gate per-package (Q16) + macOS-every-PR CI matrix (Q17)
- W03 — Telemetry projector subsystem (folded into daemon per Q1)
- W04 — telemetry-prototype vendor + DuckDB→SQLite migration
- W05 — Bench harness + bench fixture seeds
- W06 — Pricing source + cadence + stale warning
- W07 — Ruff custom rule feasibility verification + EAWF002/EAWF003 + **EAWF010 (per-module LOC cap 700 with documented exceptions per Q25)**
- W08 — **KISS-006 indexed validation context (Q23)** — per-validation indexes before invariants run; perf ≤ current 1.1 ms baseline

### C10-IMPL (15-20 EU; 3-5 waves)

- W01 — Migration tooling (`eawf migrate` through daemon canonical writer)
- W02 — Docs IA + mkdocs + auto-generated CLI/skill/schema refs
- W03 — Release pipeline + PyPI-only packaging
- W04 — Init wizard + per-persona onboarding

### C11-IMPL (14-18 EU; 3-5 waves)

- W01 — GitHub bridge (gh CLI subshell) + **delete `daemon/webhook_listener.py` + tests (Q26 — strict YAGNI; re-implement v0.6+)**
- W02 — Local polling (per Q15) + GitHub API rate-limit handling
- W03 — Keyring + HMAC raw-bytes (XB22) + generate-secret/set-secret/verify-secret (HMAC primitive stays — reusable for v0.6+ ingress)
- W04 — Doctor verb + integration manifest profile contribution

## 4.1 KISS / YAGNI gap closures (post-blitz 2026-05-18)

Per Q23 (all-6 closure) + Q24 (3 profiles only) + Q25 (700 LOC cap) + Q26 (delete webhook code):

| Gap ID | Source | Closure wave | Detail |
|--------|--------|--------------|--------|
| **KISS-001** | Codebase review P0 — coauthor env-detection contract | C04b-IMPL W01 (or C07a-IMPL W03) | Make env runtime detection opt-in via explicit `EAWF_COAUTHOR_RUNTIME`, or expose actual detected runtime in JSON. Fix `tests/integration/test_coauthor_verification.py` failure. |
| **KISS-004** | Codebase review P2 — installer shared helpers | C07a-IMPL W02 | Extract shared delta/classify/sidecar helpers from `runtimes/claude/plugin_install.py` + `runtimes/codex/plugin_install.py` + `runtimes/opencode/plugin_install.py`. Target: shared helper module < 300 LOC (Q23 LT direction). |
| **KISS-006** | Codebase review P3 — indexed validation context | C09-IMPL polish (W08+) | Build per-validation indexes before running invariants (`src/eawf/validate/strict.py` + `invariants.py`). Validation perf: keep near current ~1.1 ms baseline. |
| **KISS-007** | Codebase review P3 — worktree import cycle | C07b-IMPL W01 | Break `src/eawf/worktree/` import cycle. Submodules import `eawf.worktree.git` directly instead of through package namespace. Cycle detector → 0 internal cycles. |
| **CLI LOC cap** | Codebase review LT direction + Q25 | C09-IMPL W07 (ruff rule) + C05-IMPL split waves | EAWF010 ruff/custom lint warns above 700 LOC; per-module waiver via `# noqa: EAWF010` + mandatory rationale comment. Forces split of `lifecycle.py` (2596 LOC) + `evidence.py` (1407 LOC). |
| **Runtime LOC cap** | Codebase review LT direction + Q23 | C07a-IMPL W02 | Shared installer helper module stays < 300 LOC. Per-runtime adapters stay separate. |

**Profile trim (Q24):** C08-IMPL W03 ships 3 profiles (research + engineering + reverse-engineering) instead of 5. Spike + hybrid catalog rows stay for v0.4+ documentation.

**Webhook delete (Q26):** C11-IMPL W01 deletes `daemon/webhook_listener.py` + associated tests. HMAC verify_signature library function stays. Re-implement listener when v0.6+ ingress model ratifies.

**Net EU impact:** +3-5 EU (KISS gap closures) − 2 EU (profile trim) − 0.5 EU (webhook delete) ≈ +1-2 EU net.

## 5. Open risk surfaces (carry to v0.5+)

- Spike profile vs non-negotiable gates (Codex C08-I012).
- Multi-repo workspace integrations (CROSS.F32).
- Webhook signing key rotation cadence (Q21 — no policy v0.3-v0.5; v0.6+ ratification).
- Bio-memory consolidation (Q22 — deferred to v0.6+).
- Per-field profile override semantics (C08 D4 — whole-profile only v0.3-v0.5; per-field v0.5+).

## 6. References

[1] `.ea/local/research/long-term/2026-05-17-spec-series-combined-audit.md` §"Bottom-line answer to operator's four questions" + Q4 + BOT-05

[2] `.ea/local/research/long-term/2026-05-18-authority-map.md` — Q1 deliverable

[3] `.ea/local/research/long-term/2026-05-18-migration-dag.md` — G10 deliverable

[4] All 13 cluster briefs + 4 C04 sub-cluster briefs

## 7. Provenance

- `store_record=none (local-only)`
- `commit=3b86f7a (parent)`
- `cluster=C12 (new — Q4 deliverable)`
- `consumes=C00..C11 cluster briefs (post-Stage-0 revisions 2026-05-18)`
- `supersedes=none`
- `audit_consumed=2026-05-17-spec-series-combined-audit.md`
- `session=eawf-stage0-c12-implementation-rollup-2026-05-18`

## 8. Scrub

- status: clean
- references: repo-relative only
- local paths: 0
- real emails: 0
- abstract placeholder names: not applicable
