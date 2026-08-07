# Future ideas + deferred phases — Eä framework (post v0.3-v0.5 spec series)

**Created:** 2026-05-18

**Purpose.** Reconcile the pre-spec-series v0.3-v0.4 roadmap proposal (`.ea/local/research/2026-05-15-v0.3-v0.4-roadmap-proposal.md`) with the ratified 13-cluster spec series + C12 implementation rollup. Maps each prior-roadmap idea to its current spec home OR records it as a future-phase candidate with target version.

This brief is the **ideas backlog** for v0.5+/v0.6+ planning. Not a roadmap — does not commit phases. `/roadmap propose` consumes it when the operator picks the next phase after C11-IMPL ships.

## 1. Mapping: old roadmap → current spec series

### 1a. Absorbed — already specced under v0.3-v0.5

| Old roadmap idea | Current spec home | Status |
|------------------|-------------------|--------|
| P22 PREREQ-A: schema_version dispatch + migration framework | C01-IMPL W01 (URN_KINDS) + Q5 lock to `Literal["1.0"]` + `2026-05-18-migration-dag.md` | ✅ Absorbed |
| P22 PREREQ-A: `actor_principal_id` rename | C01-IMPL W02 minimum Principal model (Q3 / XB08) | ✅ Absorbed (placeholder; full enforcement v0.5+) |
| P23 KERNEL: LLMSession protocol | C07a `RuntimeAdapter` Protocol (C07a-IMPL W01) | ✅ Absorbed |
| P23 KERNEL: ClaudeCodeAdapter + CodexAdapter (+ OpenCodeAdapter) | C07a 3-adapter slate (C07a-IMPL W01) | ✅ Absorbed |
| P23 KERNEL: ExecutionPolicy + AgentBinding + WorktreePolicy + ArtifactStyleProfile | C07a + C07b + c04d | ✅ Absorbed |
| P23 KERNEL: spawn surface + per-wave timeout + idempotency keys | C02 daemon spawn model (D6) + §5.8 resource limits + outcome-WAL (Q10) | ✅ Absorbed |
| P23 KERNEL: auth pre-flight (`claude --check-auth`) | C07a-IMPL W01 RuntimeAdapter contract | ✅ Absorbed |
| P23 KERNEL: quota auto-pause/resume on 429 | C07a V5 runtime fallback + C02 §5.12 fallback state machine | ✅ Absorbed |
| P23 KERNEL: worktree pre-created + clean | C07b §5.1 worktree subsystem + Q13 `.ea/worktrees/` | ✅ Absorbed |
| P24 COST: cost ledger + Decimal pricing | C09 §5.9 metrics catalog + C09-IMPL W06 pricing source | ✅ Absorbed |
| P24 COST: harness session log ingestion | C09 V8 session-reuse metrics + agent-lens vendor (V7) | ✅ Absorbed |
| P24 COST: minimal OTel scaffold | C09 telemetry projector (V7; folded into daemon per Q1) | ✅ Absorbed |
| P25 CACHE: KV-cache dispatch prefix | C07a §5.6 cache-control hooks per runtime (D4) | ✅ Absorbed |
| P25 CACHE: cache_creation vs cache_read alarms | C09 cache-mislayer alarm (CROSS.F35 / G-26) | ✅ Absorbed |
| P26 TUI-EXEC: wave-board live cards | C06 §5.4 RoadmapTree + WaveBoardScreen | ✅ Absorbed |
| P26 TUI-EXEC: session log tail overlay | C06 §5.6 modal stack | ✅ Absorbed |
| P26 TUI-EXEC: cost pane + quota pane | C06 /metrics overlay (D9) | ✅ Absorbed |
| P26 TUI-EXEC: 429 banner | C06 D8 runtime-switched toast | ✅ Absorbed |
| P27 DAEMON: eawfd skeleton + JSON-RPC over UDS | C02 daemon V1 + Q1 supersede (sole mutator) | ✅ Absorbed |
| P27 DAEMON: notification bus | C02 §5.7 subscription bus | ✅ Absorbed |
| P27 DAEMON: single-writer mutator | C02 + Q1 supersede | ✅ Absorbed (stronger: daemon = sole; not just single-writer-arbiter) |
| D34/D46/D47: tag-only v0.3-v0.4 (no PyPI) | C10 Q3 + Q4-refr PyPI-only confirmed | ✅ Absorbed (operator picked PyPI vs no-push; superseded) |
| D35: P19 reactive iter | DONE 2026-05-15 era | ✅ Closed |
| D37: P16 zero-wave closure | DONE | ✅ Closed |
| D39: LLMSession inside KERNEL (no separate phase) | C07a-IMPL W01 (no separate LLMSession phase) | ✅ Absorbed |
| D41: daemon scope (5 surfaces) | C02 covers watcher/bus/scheduler/mutator/budget; memory-trigger surface deferred per Q22 | 4/5 ✅; 1 ⏭️ |
| D43: BYOK rejected — subprocess to vendor CLI | C07a §5.2 SDK matrix locks subprocess-primary; SDK forecast post-2026-06-15 | ✅ Absorbed |
| D44: JSON + event.jsonl in git canonical | Confirmed — C09 Q18 SQLite for telemetry only; `state.json` stays JSON | ✅ Absorbed |
| D45: coauthor policy | C07b D6 `runtime` mode + KISS-001 fix (Q23 closure) | ✅ Absorbed |
| B063: normalize-coauthor.py commit-msg hook | c04b `/coauthor` skill body + KISS-001 fix | ✅ Absorbed |
| B064: defer:publication backlog tag | Covered by Q3+Q4-refr PyPI-only lock | ✅ Absorbed (no longer needed) |

### 1b. Reshaped — order or scope changed by spec series

| Old roadmap idea | Reshape | Reason |
|------------------|---------|--------|
| D40: v0.4 ordering KERNEL→COST→CACHE→TUI-EXEC→DAEMON→MEMORY→REPLAY | **Daemon first (P23-DAEMON moved before KERNEL in spec series)** | V1 ratified daemon Day-1; C02 KERNEL functions ride daemon as RPC client |
| P22 PREREQ-A as separate phase | **Folded into C01-IMPL (W01 URN_KINDS + W02 Principal min)** | Combined with foundations cluster impl |
| P26 TUI-EXEC as separate phase after KERNEL | **Folded into C06-IMPL (P30) — depends on event-subscribe protocol from C02** | C06 spec consolidates TUI rebuild on Textual; kernel overlays now built atop ratified event/RPC contracts |
| Per-runtime adapter contract `runtime` field | **Renamed to `runtime: list[str]`** per c04b D-b2 (was `visibility.runtimes`) | Codex C04-I010 fix |

### 1c. Deferred — moved to v0.5+/v0.6+ ideas backlog (rest of this brief)

The 8 items below were in the old roadmap but **NOT** absorbed into the v0.3-v0.5 spec series. Each gets a future-phase slot below.

## 2. Future-phase candidates (v0.5+)

### IDEA-01 — Auto-execute kernel (`/flow execute` real stage)

**Source:** Old P23 KERNEL non-protocol bits (`flow.auto_accept.execute`, autopilot, disjoint-scope gate, dispatch-batch bundle prompts, UserQuestion `decision_id` + `resume_command`).

**What:** Accepted plans dispatch to bounded vendor-CLI subagents under explicit `flow.auto_accept.execute` authorisation, with producer-enforced disjoint write scopes, dispatch-batch bundle output for offline review.

**Why not in v0.3-v0.5:** Spec series freezes the contracts (skill envelopes, runtime adapter, dispatch metadata) but `/flow execute` autopilot stage requires operator trust + budget governor + safety gates that build on the v0.4 cost ledger landing first.

**Target:** **v0.5 P34-EXECUTE** (after C11 ships).
**Prereqs:** C04 (workflow contracts), C07a (runtime adapters), C09 (cost ledger + budget governor), C02 (daemon outcome-WAL).
**EU envelope:** ~20-30 EU (~15 waves) — most of old P23 KERNEL.
**Spec needed:** Yes — author `c13-execute-autopilot.md` (or equivalent) before phase opens.

### IDEA-02 — Budget governor + per-wave token-budget enforcement (B028b)

**Source:** Old P24 COST (budget governor soft-cancel) + B028b (per-wave token-budget cap warn 75% / block 100%).

**What:** Daemon enforces per-project + global monthly $ caps and per-wave token caps. Soft-cancel on breach (operator can override); hard-block at 100%. Operator-configurable.

**Why not in v0.3-v0.5:** C09 specs cost tracking + pricing source but does not include enforcement layer. Spec series mentions `daemon.budget_governor` surface but reserves implementation.

**Target:** **v0.5 P35-BUDGET** (after P34-EXECUTE).
**Prereqs:** P34 (execute autopilot needs budget to enforce against), C09 cost ledger ratified.
**EU envelope:** ~8-12 EU (~5 waves).
**Spec needed:** Yes — extend C09 R-version OR author new `c14-budget-governor.md`.

### IDEA-03 — Daemon scheduler primitive (cron / lifecycle-idle / memory triggers)

**Source:** Old P27 DAEMON W04 (scheduler) + W07 (memory-trigger surface).

**What:** apscheduler-based per-project cron primitive. Listens on `lifecycle.idle` events (phase-close, fail, abandon). Fires `memory.trigger.{consolidate,downscale,rehearse,reflect,replay}` envelopes. Double-fire guard keyed on `(phase, iter, max-closed-wave-timestamp)`.

**Why not in v0.3-v0.5:** Q22 deferred bio-memory to v0.6+. Without bio-memory, scheduler has only cron-style ops to schedule. C02 daemon doesn't include scheduler subsystem.

**Target:** **v0.6 P40-SCHEDULER** (paired with P41-BIO-MEMORY).
**Prereqs:** v0.5 ship; Q22 prereq bundle (telemetry replay + event store maturity + audit DSL).
**EU envelope:** ~6-10 EU (~4 waves).
**Spec needed:** Yes — author when Q22 prereqs ratify.

### IDEA-04 — Bio-memory consolidation (P28 MEMORY entirely)

**Source:** Old P28 MEMORY — `MemoryKind{EPISODIC|SEMANTIC|PROCEDURAL}` + `MemorySalience{HIGH|MEDIUM|LOW}` + `MemoryDigest` + `MemoryReflection` + `MemoryReplayCandidate`. memory verbs: consolidate/downscale/rehearse/reflect/replay. LLM-driven; daemon-triggered.

**What:** Sleep-time-compute pre-expansion of next-wave dispatch envelope. Park 2023 reflection threshold. Prioritized replay sampling. ~$0.15/phase consolidation ceiling.

**Why not in v0.3-v0.5:** Q22 + C00 Non-Goal row — defers to v0.6+ pending prereq bundle (telemetry replay maturity + event-store stability + audit DSL kind catalog).

**Target:** **v0.6 P41-BIO-MEMORY**.
**Prereqs:** All three Q22 prereqs land; render-context integration (dispatch envelope reads `MemoryDigest`).
**EU envelope:** ~9-15 EU (~6 waves).
**Spec needed:** Yes — re-open `2026-05-15-long-term-features-deep.md` §2 [9]; ratify after Q22 prereqs.

### IDEA-05 — HLC envelope stamp + non-determinism audit

**Source:** Old P29 REPLAY W02-W03 — HLC (48-bit physical ms + 16-bit logical) envelope stamp; refactor `datetime.now()` / `uuid.uuid4()` out of `_append_event` apply path.

**What:** Hybrid Logical Clock ordering on events for cross-process audit replay determinism.

**Why not in v0.3-v0.5:** C02 outcome-WAL fix (XB12) partially addresses non-determinism by capturing post-apply diff. HLC stamp is a separate v0.5+ enhancement — not load-bearing for v0.3-v0.5 daemon.

**Target:** **v0.5+ P36-EVENT-REPLAY** (paired with event-source rebuilder).
**Prereqs:** C02 daemon outcome-WAL ratified; event envelope schema frozen (Q14 — done).
**EU envelope:** ~3-5 EU (~3 waves).
**Spec needed:** Light — extend C07b R-version with HLC field on Event envelope.

### IDEA-06 — Event-source rebuilder + Merkle hash-tree verify + speculative branches + time-travel TUI

**Source:** Old P29 REPLAY W04-W08.

**What:**
- `eawf state replay` — walks event.jsonl, applies typed mutations, asserts `after_state_version` matches.
- Merkle hash-tree verify — lazy build for `eawf state verify --hash-tree`; pin `__root__` in audit.jsonl nightly; O(log n) drift localisation.
- Speculative branches — `eawf replay branch --from-event <hex> --label <name>`; read-only relative to main; 7-day TTL.
- Time-travel TUI — Rich Live 3-row layout; ←→ step events, g goto, d diff.

**Why not in v0.3-v0.5:** Heavy lift; PyO3 candidate per `2026-05-15-language-and-pyo3-fit.md` (event-source rebuilder + Merkle verify benchmark gate — Python beyond 50-100K events becomes user-visible).

**Target:** **v0.5+ P36-EVENT-REPLAY** + **v0.6 P42-TIME-TRAVEL-TUI**.
**Prereqs:** IDEA-05 HLC envelope stamp; canonical Event model (Q14 — done); typed Mutation discriminated union (PREREQ-B inside this phase).
**EU envelope:** ~12-18 EU (~7 waves) for rebuilder + Merkle + branches; +5-8 EU for time-travel TUI.
**Spec needed:** Yes — author `c15-event-replay.md` when prereqs land.

### IDEA-07 — Daemon filesystem watcher (inotify / fsevents)

**Source:** Old P27 DAEMON W02.

**What:** Watcher on `.ea/event.jsonl` per registered repo; fan out to dashboard SSE-equivalent over UDS. Replaces mtime-poll fallback C06 uses today.

**Why not in v0.3-v0.5:** C02 daemon spec uses push-via-event-subscribe RPC primary + mtime-poll fallback (C06 §5.10). Native filesystem watcher is an optimization layer, not load-bearing for first ship.

**Target:** **v0.5 P37-WATCHER** (perf optimization phase).
**Prereqs:** C02 daemon + C06 TUI both ratified.
**EU envelope:** ~4-6 EU (~3 waves) including per-OS inotify/fsevents adapter.
**Spec needed:** Light — extend C02 R-version.

### IDEA-08 — AskUserQuestion bridging across runtimes

**Source:** Old P27 DAEMON W03 + UserQuestion presenters from P23 W13.

**What:** `agent_question` event from subagent → daemon inbox → TUI "needs you" panel; bridges AskUserQuestion across CC + Codex + OpenCode (each runtime has different prompt surface).

**Why not in v0.3-v0.5:** C04 needs_user handshake covers the envelope contract but the cross-runtime presenter bridging requires both runtime adapters fully ratified + daemon notification bus stable.

**Target:** **v0.5 P38-AUQ-BRIDGE** (or fold into P34-EXECUTE).
**Prereqs:** C07a runtime adapters; C04 envelope contract; C02 notification bus.
**EU envelope:** ~3-5 EU (~3 waves).
**Spec needed:** Yes — extend C04a R-version OR author `c16-auq-bridge.md`.

## 3. Deferred to v0.6+ (post-spec-series)

These were in the old roadmap or surface in feeder briefs (`long-term-valuable-features-2026-05-15.md`, `long-term-features-deep.md`). NOT in current spec series. Listed for completeness.

| Idea | Source | Target | Notes |
|------|--------|--------|-------|
| Federation / multi-host daemon | D42 + roadmap-synthesis | v0.6+ | Multi-user invariant in V1 single-user only |
| OPA policy bundle | rev-1 v0.4 NG | v0.6+ | Authorization rules engine |
| gVisor / Firecracker sandbox isolation | rev-1 v0.4 NG | v0.6+ | OS-level isolation |
| Sigstore release signing | rev-1 v0.4 NG | v0.6+ | Supply-chain integrity |
| CRDT-LWW claim register | rev-1 v0.4 NG | v0.6+ | Federation enabler |
| SQLite-WAL backend for state.json | Axis B (long-term-features-deep) | v0.6+ | Trigger: state.json > 1 MB; current ~50KB |
| Web dashboard (real, not stub) | C06 web stub | v0.5+ | C06 marked stub-only per Codex C06-I005 |
| Outcome eval lake | long-term-valuable-features | v0.6+ | Centralized eval data store |
| MCP supply-chain firewall | long-term-valuable-features | v0.6+ | Defense against malicious MCP servers |
| Work-stealing dispatcher | Axis B bottleneck | v0.6+ | Only at 4+ workers |
| Agent router / task broker | long-term-valuable-features | v0.6+ | Cross-runtime dispatch |
| Webhook ingress (relay / tunnel) | Q15 / C11 | v0.6+ | Local polling stands v0.3-v0.5; webhook listener code deleted per Q26 |
| Full Principal enforcement | XB08 / Q3 | v0.6+ | Min model lands v0.3-v0.5; enforcement requires signed events + caps |
| Webhook signing key rotation | Q21 | v0.6+ | No policy v0.3-v0.5; operator-triggered only |
| Bio-memory consolidation | Q22 | v0.6+ | Full bio-memory phase per IDEA-04 |
| spike + hybrid bootstrap profiles | Q24 | v0.4+ | C08-IMPL W03 ships 3 profiles only |
| `flow.jsonl` schema v2 (8-step) migration | CROSS.F39 | v0.4 hygiene wave | Bump after C04a-IMPL ratifies the 8-step pipeline |
| `Wave.commit` field drop + git-log-walk backfill | Q11 / BOT-07 | v0.4 hygiene wave | Tracked in C01 Provenance |
| State-history 5-tier archival model | state-history-cache-design feeder | v0.6+ | Gitignored `.ea/indexes/state-history/` derivation cache; 5-tier decision tree (T0 inline closed records → T1 render-fold → T2 stub-in-state + `git_ref` → T2.5 gitignored derivation cache w/ LRU blob eviction → T3 SQLite-WAL hot-state swap when state.json > 1 MB). Cache is purely a read-side accelerator; every byte re-verifiable against git pack-files. First hard consumer = daemon memory-size/time-elapsed triggers |

## 4. Recommended post-v0.5 phase sequencing

After C11-IMPL (P33) ships v0.5:

```
v0.4 hygiene wave           (inline; ~2-3 EU; Wave.commit drop + flow.jsonl bump + spec backfill)
                                                ↓
P34-EXECUTE        (~20-30 EU) IDEA-01 auto-execute kernel + UserQuestion presenters
                                                ↓
P35-BUDGET         (~8-12 EU)  IDEA-02 budget governor + per-wave token cap
                                                ↓
P36-EVENT-REPLAY   (~12-18 EU) IDEA-05 HLC + IDEA-06 rebuilder + Merkle + branches  [v0.5 ship]
                                                ↓
P37-WATCHER        (~4-6 EU)   IDEA-07 filesystem watcher (optimization)
                                                ↓
P38-AUQ-BRIDGE     (~3-5 EU)   IDEA-08 AskUserQuestion bridging
                                                ↓
[v0.6 prereqs land]    (Q22 prereq bundle stable)
                                                ↓
P40-SCHEDULER      (~6-10 EU)  IDEA-03 daemon scheduler + lifecycle-idle hooks
P41-BIO-MEMORY     (~9-15 EU)  IDEA-04 consolidate + downscale + rehearse + reflect + replay
P42-TIME-TRAVEL    (~5-8 EU)   time-travel TUI (rest of IDEA-06)
                                                ↓
[v0.6+ federation/sandbox/Sigstore etc. ratify on demand]
```

**v0.5+ EU envelope:** ~47-71 EU across 5 phases.
**v0.6 EU envelope:** ~20-33 EU across 3 phases.
**v0.6+ items:** Sized on operator demand signal; no roadmap claim until prereqs land.

## 5. Spec gap closure before phases above can claim

Each IDEA above requires a spec brief before `/roadmap propose` claims its phase:

- **IDEA-01** → `c13-execute-autopilot.md` (or extend c04a R-version)
- **IDEA-02** → `c14-budget-governor.md` (or extend C09 R-version)
- **IDEA-03** → fold into bio-memory spec when Q22 prereqs ratify
- **IDEA-04** → re-open + ratify `long-term-features-deep.md` §2
- **IDEA-05 + IDEA-06** → `c15-event-replay.md`
- **IDEA-07** → extend C02 R-version (light)
- **IDEA-08** → extend c04a R-version OR `c16-auq-bridge.md`

## Methodology positioning (manifesto)

**Positioning.** Eä is a *governed-ADD profile* — an opinionated profile of Agent-Driven Development whose non-negotiables are state-resident specs, append-only audit, and process-as-trust. **Eä is the methodology; `eawf` is one reference implementation** — the methodology is the part a team adopts, the CLI is the part it can swap. Vendor-neutral: no required model or IDE.

**Three load-bearing invariants.** (1) Specification is the source of truth, not chat history. (2) State is structured and dispatched, not free-form. (3) Trust is produced by process, not granted by inspection.

**The seven rules.**

1. **Specs are source of truth** — intent lives in a versioned, machine-readable spec; code is the derived artifact. When spec and code disagree, the code is the drift.
2. **Agents draft; humans decide** — the agent proposes (implementations, tests, deletions, plan revisions); the human directs, scopes, approves. Merge authority never transfers.
3. **Process produces trust** — delegation is earned by installing guardrails that make bad output expensive (tests, type-checking, lint, hooks, golden fixtures, audit logs, review gates), not by inspecting more output.
4. **One repo-resident contract** — a single source-controlled `AGENTS.md` captures conventions, command surface, naming, deletion policy, anti-patterns; tool-specific configs include it rather than fork it.
5. **Plan before execute** — no execute-mode run without a written, reviewable plan (scope, decomposition, success criteria, rollback). Execution that diverges halts and revises the plan first.
6. **State is structured; mutations are dispatched** — a typed, schema-validated store; mutations go through a single dispatcher with file locking and audit emission. No agent edits state directly.
7. **Verify before claiming** — behavioural claims cite the implementation (file path, line, log excerpt, snapshot). Design docs are intent; the source tree is truth.

**Rule 8 (extension — multi-workstream scale).** Phase-bundled delivery: work ships as phases, one PR per phase; waves are independent worktree-isolated units that cherry-pick (never merge) into the long-running phase branch.

**What Eä is not.** The discriminators are not stylistic — vibe coding has no recovery story, assistant coding has no scope story, bare spec-driven development has no audit story; Eä addresses all three.

| | Vibe coding | AI-assisted | Spec-driven (alone) | Eä ADD |
|---|---|---|---|---|
| Source of truth | chat thread | IDE buffer | the spec | spec + state store |
| Agent role | autocomplete++ | line-by-line helper | code generator | full-SDLC contributor |
| Human role | reviewer of vibes | typist with hints | spec author + merger | architect + gatekeeper |
| Trust mechanism | gut feel | inspection | spec adherence | guardrails + audit |
| Recovery model | start over | undo | regenerate from spec | replay from audit |
| Audit trail | none | git blame | spec version | state mutations + reports |

## 6. References

[1] `.ea/local/research/2026-05-15-v0.3-v0.4-roadmap-proposal.md` — original v0.3-v0.4 roadmap proposal (this brief supersedes its P22..P29 phase plan + D38..D48 decisions where v0.3-v0.5 spec series ratified)

[2] `.ea/local/research/long-term/2026-05-17-spec-series-combined-audit.md` — Stage-0 audit + operator decisions Q1..Q26

[3] `.ea/local/research/long-term/2026-05-18-c12-implementation-rollup.md` — current v0.3-v0.5 implementation EU envelope

[4] Bio-memory + Axis B/D feeder (IDEA-04 + IDEA-02 source) — nuggets folded into §2 above; source feeder deleted in the post-v0.3 local purge.
[5] Outcome-eval-lake + MCP-firewall + agent-router feeder — nuggets folded into §3 above; source feeder deleted in the post-v0.3 local purge.
[6] PyO3 perf-gate feeder (IDEA-06 source) — nugget folded into §2 above; source feeder deleted in the post-v0.3 local purge.

## 7. Provenance

- `store_record=none (local-only)`
- `commit=3b86f7a (parent)`
- `cluster=N/A (future-ideas / backlog brief)`
- `consumes=2026-05-15-v0.3-v0.4-roadmap-proposal + 13-cluster spec series + Q1..Q26 operator decisions`
- `supersedes=2026-05-15-v0.3-v0.4-roadmap-proposal.md phase plan + decision rows where v0.3-v0.5 spec series ratified`
- `audit_consumed=2026-05-17-spec-series-combined-audit.md`
- `session=eawf-future-ideas-rollup-2026-05-18`

## 8. Scrub

- status: clean
- references: repo-relative only
- local paths: 0
- real emails: 0
- abstract placeholder names: not applicable
