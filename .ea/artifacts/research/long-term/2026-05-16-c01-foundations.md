# C01 — Foundations — Eä framework long-term specs

**Cluster:** C01 (Foundations — vocabulary, URN scheme, entity catalog, lifecycle, persona authority)

**Title:** Foundations

**Status:** `local-draft`, `needs-user` (pending operator ratification of §8 open questions)

**Created:** `2026-05-16T00:00:00Z`

**Author:** `claude-opus-4-7`

**Depends on:** C00 (verdicts V1..V8 locked; cluster catalog confirmed) [1]

**Consumed by:** C02..C11 (every downstream cluster cites the entity catalog, URN grammar, or persona matrix)

## 1. Purpose + scope statement

C01 locks the **universal vocabulary** every downstream cluster depends on. Without C01 the daemon (C02) cannot name what it arbitrates, the spec subsystem (C03) cannot type its records, the skill envelope (C04) cannot reference state-resident rows, the CLI surface (C05) cannot validate its arguments against a closed enum, and the operator surface (C06) cannot render the scope ladder.

**In scope (C00 §C01 [1:312-358]):**

- Single canonical **glossary** (~40 alphabetical terms covering: phase / iter / wave / scope / audit / hypothesis / decision / artifact / brief / spec / runtime / plugin / runtime-coauthor / verdict / profile / daemon / dispatcher / envelope / chassis / URN — plus the foundational ones the rest of this brief references).
- **URN scheme** finalised: `urn:eawf:v1:<kind>:<path>` with a complete `kind` enum (26 single-word tokens, operator-confirmed 2026-05-16 [§4 D1]) and per-kind path grammar.
- **State entity catalog** (Repo, Subproject, Phase, Iter, Wave, Hypothesis, Decision, Audit, Artifact, Memory, AgentReport, AgentSession, Event, Profile, Spec, Runtime, Plugin, McpServer, Principal-deferred) with field-level Pydantic v2 schema sketch grounded in `src/eawf/state/models.py` [10].
- **Per-entity lifecycle state machines** (DAGs from current `eawf.lifecycle.transitions` [16] plus proposed transitions for new entities).
- **Persona definitions** (operator, agent-executor, agent-reviewer, agent-auditor, agent-researcher, agent-planner, agent-polisher, agent-domain-specialist, daemon, watcher, profile-author) with authority matrix (rows = personas, cols = actions).
- **Trust + audit-replay model** — how a future operator reconstructs the evidence chain for any state-resident assertion from `state.json` + `event.jsonl` + `audit.jsonl` + agent-report JSONLs alone.
- **SDLC mapping** — research → spike → roadmap propose → revise → apply → prep → flow → audit → ship → close, with phase-bundling rules per Rule 8 of the manifesto [2:75-78].

**Out of scope (deferred per C00 [1:325-327]):**

- **API design**: daemon IPC method catalog, CLI verb-noun matrix, TUI widget catalog → C02 / C05 / C06.
- **Per-skill output contract**: skill registry, envelope status transitions, `needs_user` handshake → C04.
- **Schema migration tooling**: `VersionedState` discriminated union, Alembic-style `vN_to_vN+1.py` runners, the per-bump golden fixtures → C03 (`schema_version: Literal["1.0"]` on State today [10:492] is the foundation C01 documents but does not extend).

## 2. Goals + non-goals

### Goals

| G# | Goal | Source |
|---|---|---|
| G1 | Every cross-cutting concept named exactly once (canonical glossary, ~30-50 terms). | C00 §C01 [1:315-317] |
| G2 | URN grammar covers every entity in the catalog (so the C09 telemetry projection key, the V8 session-handle map, and the R5 federation handshake can all dereference the same URN). | C00 §C01 [1:317-318], C00 V7 [1:184-224], C00 V8 [1:228-271] |
| G3 | Per-entity Pydantic sketch is field-level (not hand-waved), grounded in current `src/eawf/state/models.py` [10] when the entity already exists, or in proposed shape when new. | C00 §C01 [1:318-319] |
| G4 | Every entity has a complete lifecycle DAG (no `status` value left without a documented transition in or out). | C00 §C01 [1:319-320] |
| G5 | Persona authority matrix is complete (rows × cols = decided cell, no "tbd"). | C00 §C01 [1:320-321] |
| G6 | Trust + audit-replay model spells out the minimum evidence chain a future operator needs to retract or confirm any in-state assertion using only `state.json` + `event.jsonl` + `audit.jsonl` + agent-report JSONLs. | C00 §C01 [1:321-322] |
| G7 | SDLC mapping covers research → ship → close, naming the state-mutator at every step. | C00 §C01 [1:322-323] |
| G8 | Brief is self-contained — quotes V1..V8 inline, cites all source-tree file:line refs, ratifiable in one fresh CC session. | C00 V4 [1:99-125] |

### Non-goals

| NG# | Non-goal | Why deferred |
|---|---|---|
| NG1 | Daemon IPC method catalog or RPC framing. | C02 owns it [1:362-425]. |
| NG2 | CLI verb-noun matrix. | C05 owns it [1:539-583]. |
| NG3 | Skill registry contract + `needs_user` handshake. | C04 owns it [1:485-534]. |
| NG4 | Schema versioning tooling (migration runners, golden fixtures, `VersionedState` union). | C03 owns it [1:430-479]. |
| NG5 | Telemetry DB schema (DuckDB vs SQLite, telemetry-prototype vendoring). | C09 owns it [1:769-841]. |
| NG6 | TUI widget catalog or scope dispatch ladder. | C06 owns it [1:587-644]. |
| NG7 | Per-runtime adapter shape. | C07 owns it [1:649-712]. |
| NG8 | External integration surface (GitHub bridge, Slack, Linear). | C11 owns it [1:897-930]. |

## 3. Prior verdicts cited

C01 inherits eight verdicts from C00 [1:22-271]; below are the relevant passages and the C01 concept they constrain.

### V1 — eawfd daemon Day-1 + smart-spawn writer [1:24-53]

> "Mutations to `state.json` (and all future stateful surfaces — config layers, registry, event log) route through the eawfd daemon. CLI auto-spawns daemon on demand if not running ... Reads MAY bypass daemon ... daemon stays alive on idle-timeout (default 300 s, configurable)."

**C01 binding:** `State` mutator surface is the daemon (proxied by today's `uv run eawf state ...` CLI per AGENTS rule 4 [11]). Entity catalog records this in the persona authority matrix (only `daemon` persona writes; every other persona reads). New URN kind `daemon` reserved (single-word token); persona definition for `daemon` enumerated in §5.5.

### V2 — Three-tier specs (Phase + Iter + Wave) [1:55-74]

> "Each scope level carries its own typed spec with own schema, validator, mockup requirement, audit check: PhaseSpec — phase charter ... IterSpec — iter intent ... WaveSpec — wave deliverable ... Storage paths: `.ea/specs/<phase>/spec.md` ... `.ea/specs/<phase>/<iter>/spec.md` ... `.ea/specs/<phase>/<iter>/<wave>.md`."

**C01 binding:** `Spec` is a new entity in the catalog. Per operator decision (§4 D3) the spec body lives at the V2 storage paths and is addressable by URN `urn:eawf:v1:spec:<repo>/<phase>[/<iter>[/<wave>]]`. No `spec_path` field on Phase/Iter/Wave — the URN is derivable from the entity's own ID. Spec lifecycle DAG specified in §5.4. C03 owns the per-tier Pydantic schemas; C01 only locks that Spec is a first-class entity with its own URN kind.

### V3 — Composable profile bundle with declared precedence [1:76-96]

> "Project carries `profiles: [research, engineering, reverse-engineering, spike, ...]` ordered list. Each profile declares `conflicts_with: [...]` and `overrides: [...]`. Loader fails fast if conflict undeclared. ... Effective ruleset = union of profile contributions, conflict-resolved by precedence."

**C01 binding:** `Profile` is a new entity in the catalog (today's `ComposedProfile` and `ProfileBody` [13:97-122] become the typed shape). URN kind `profile` reserved; profiles addressable as `urn:eawf:v1:profile:<owner>/<id>` where `owner` is `user` for global profiles, or a project code for repo-pinned overlays. Persona `profile-author` enumerated in the matrix.

### V4 — Cluster-sequential batching [1:98-125]

> "11 cluster briefs, written one at a time, each consumed by a separate fresh CC session ... Each brief is self-contained: includes prior-cluster verdicts as inline citations rather than depending on conversation log."

**C01 binding:** This brief honours the contract — V1..V8 quoted inline (§3), self-contained references (§9), front-matter per the template.

### V5 — Runtime fallback: reactive switchover on error [1:127-151]

> "Daemon uses reactive auto-switch on primary-runtime failure (HTTP 429 / 5xx / timeout / API-error) ... daemon flips the affected wave to the next runtime in the configured preference ladder and re-issues the dispatch envelope against that runtime with the idempotency key preserved."

**C01 binding:** `Runtime` is a new entity in the catalog with a configured preference ladder and per-wave override. URN kind `runtime` reserved. The audit-replay model (§5.6) names the `runtime_switched` event as the audit anchor for fallback decisions so a future operator can reconstruct which runtime serviced which dispatch attempt.

### V6 — Cross-platform daemon: per-OS native service + on-demand spawn [1:153-182]

> "Daemon bootstraps natively on each supported OS ... Linux: `systemd --user` ... macOS: `launchd` ... Windows: per-user Windows Service via `pywin32` ... On-demand spawn from V1 remains the default."

**C01 binding:** Persona `daemon` defined as a single OS-user-scoped service in the authority matrix; no multi-user daemon variant in v0.3 → v0.5. The trust model (§5.6) assumes one daemon per OS user; cross-user federation is C07/v0.5+ work.

### V7 — Telemetry: vendor the telemetry-prototype schema, rebuild inside eawf [1:184-224]

> "Telemetry lives inside eawf, not as a separate sidecar ... Storage: User-scope DuckDB ... fed by per-repo `event.jsonl` projections (`incident`, `audit_*`, `agent_end`, `wave_close`, `runtime_switched` events) ... per-runtime session logs ... per-dispatch envelope metadata."

**C01 binding:** Telemetry is a *projection* of state-resident records, not a separate source of truth. URN kind `event` reserved so the projection key can address envelopes uniformly across repos. C01 documents the projection-source entities (Event, AgentReport, Incident, Wave, AgentSession); C09 owns the DuckDB schema + rebuild algorithm.

### V8 — Agent dispatch: hybrid session reuse [1:226-271]

> "Fresh process per new wave dispatch — clean context, full KV-cache hit on the stable prefix ... Reuse session (`claude --continue <session-id>` / Codex `--resume` / OpenCode equivalent) on retry / edit / follow-up against the same wave ... Daemon tracks the session handle per `(wave_id, attempt_id)` and routes retry envelopes back to the existing session."

**C01 binding:** `AgentSession` already exists in state (`State.agent_sessions` [10:514]). C01 adds per-attempt session handles as a nested map under `Wave.sessions: dict[attempt_id, SessionAttempt]` (proposed in §5.3 Wave subsection). URN kind `session` reserved.

### V9 — Native per-runtime plugins remain first-class distribution channel [1:287-329]

> "Per-runtime plugin manifests (Claude `.claude/plugins/eawf/`, Codex `<local-path>`, OpenCode `<local-path>`) remain the canonical distribution channel even after V1 daemon. Plugin sync regenerates manifests deterministically from `SKILL_REGISTRY`. Plugin doctor reports drift."

**C01 binding:** `Plugin` is a new entity in the catalog (per `PluginInstall` proposal in §5.3.18). URN kind `plugin` reserved (single-word token). The four hard non-negotiables ratified with V9 — (a) plugin sync is deterministic regenerate-from-registry; (b) plugin doctor surfaces drift; (c) PluginManifest is `BaseModel` with `extra="forbid"` and `schema_version: Literal["1.0"]` (per XB19); (d) `eawf plugin sync` (not `plugin install --regenerate`) is the canonical verb — bind through C07a §5.7-§5.9 and feed C04b skill registry.

## 4. Decision matrix

Operator-confirmed decisions (2026-05-16 AskUserQuestion answers, user notes inline) seed the rows below. Per V4 cluster-sequential-batching the brief records what was locked, not what was debated.

| # | Axis | Options considered | Recommendation | Rationale |
|---|---|---|---|---|
| **D1** | URN kind enum scope | (a) broad — one kind per entity; (b) narrow — keep current 10; (c) hybrid | **broad, single-word tokens** (operator-confirmed) | C09 telemetry projection key + R5 federation handshake [12] both want one URN per entity. Single-word tokens (`report` not `agent_report`, `mcp` not `mcp_server`) keep URN strings short for envelope bodies. URN_KINDS triples from 10 → 25; every new kind must declare its path grammar (§5.2). |
| **D2** | Project-vs-Repo split | (a) merged (today); (b) split (Project user-scope spanning repos) | **merged — Repo = Project** (operator-confirmed); `Subproject` kept as nested scope-defining concept | The current `State.project` field [10:496] already names this conflation. Operator note: "maintain Subprojects inside too (scope-defined)". `WorkspaceIndex` + `Registry` [14] handle cross-repo aggregation outside the project entity. Subprojects retain their existing semantics (group goals + waves under a sub-workstream id) and route through `current.subproject_id` [10:144]. |
| **D3** | Spec storage shape | (a) state-resident row; (b) filesystem-only with state pointer; (c) hybrid | **filesystem-only, URN-derivable, archived on phase close** (operator-confirmed) | Spec lives at `.ea/specs/<phase>/[<iter>/]<wave\|spec>.md` per V2 [1:69-73]. URN `urn:eawf:v1:spec:<repo>/<phase>[/<iter>[/<wave>]]` is derivable from the entity id; no `spec_path` field on Phase/Iter/Wave. On phase close, the daemon `git rm`s `.ea/specs/<phase>/` and indexes the last SHA in a daemon-side cache so `eawf spec show <phase>` can hydrate from `git log` without operator handwork. C03 owns the schema + validator. |
| **D4** | Principal entity in v0.3 catalog | (a) lock now (per R5); (b) defer to v0.5+ governance phase | **defer + reserve URN kind + document migration** (operator asked for explanation; recorded under §5.3 Principal subsection + §8 Q3) | R5 [12:486-507] specifies Principal as the prerequisite for signed events / RBAC / federation. Today's actor is hardcoded `"cli"` literal at 16+ sites [10:--, 23:17]. C01 catalogues Principal as a *reserved* entity (URN kind + Pydantic sketch) so v0.5+ governance work doesn't have to invent the shape from scratch, but state.json shape stays unchanged for v0.3-v0.5. C01 §5.3 Principal subsection explains the entity in detail. |
| D5 | Event vs Audit log canonical role | (a) one log, two views; (b) two logs, two purposes | **two logs, two purposes** (matches current code [3]) | `event.jsonl` records every mutation envelope (`EventPayload` [25]); `audit.jsonl` records every `Audit` check-result (`AuditPayload` [27]). Telemetry-projection per V7 [1:191-192] reads both. Separation is load-bearing: event is mutation provenance; audit is verdict provenance. Reconcile via a third `agent_end` projection per V7 [1:191]. |
| D6 | Memory namespace | (a) per-scope only; (b) per-(user, project, session) | **per-scope, with tier slot** (matches today's `MemoryTier` enum [4:226-240]) | `MemoryStatus`+`MemoryTier`+`scope_id` already encode the per-scope + tier dimensions in `MemorySummary` [10:435-453]. User-scope memory lives at workspace URN; session-scope memory lives at session URN. Operator can recall any tier via `memory render-context --tier`. |
| D7 | AgentReport append-only retry | (a) overwrite; (b) append + max-attempt projection | **append + projection** (current shape [23:79-89]) | `report_record_id(role, base_id, attempt)` already encodes the retry tuple [23:223-234]. AGENTS rule 19 [11] mandates append-only. C01 documents the projection rule: read-side computes "latest attempt for (role, base_id)" by max(attempt). |
| D8 | Lifecycle granularity for new entities (Spec, Profile, Principal, Runtime) | (a) reuse PLANNED/ACTIVE/CLOSED triple; (b) per-entity bespoke | **per-entity, conservative** (§5.4 details) | The scope triple PLANNED/ACTIVE/CLOSED suits Phase/Iter; Wave uses PENDING/CLAIMED/IN_PROGRESS/CLOSED/FAILED/ABANDONED [4:59-65]; Spec uses DRAFT/READY/IMPLEMENTED/ARCHIVED per V2 [1:69-73] and operator §4 D3. Profile uses LOADED/SHADOWED/CONFLICTED. Runtime uses CONFIGURED/HEALTHY/DEGRADED/UNAVAILABLE. Principal (deferred) uses ACTIVE/REVOKED. |

## 5. Proposed schemas, vocabulary, lifecycle

This is the brief's body — the §5 "Proposed schema/API/protocol" of the C00 contract — broken into seven sub-sections (5.1 Glossary, 5.2 URN, 5.3 Entity catalog, 5.4 Lifecycle DAGs, 5.5 Persona matrix, 5.6 Trust model, 5.7 SDLC mapping). C03/C04/C05/C06/C07/C08/C09 each pick up from one of these sub-sections and extend it within their own domain.

### 5.1 Glossary

Canonical names. Each entry: 1-3 line definition + cross-link to the entity / URN / brief that owns it. Alphabetical.

| Term | Definition |
|---|---|
| **agent-auditor** | Persona that executes audit-DSL kinds against a closed scope; emits `auditor_report` store records [4:281]. Authority: read everything in the audited scope; cannot mutate state. |
| **agent-executor** | Persona that writes code under a claimed wave; emits `executor_report` store records [4:280]. Authority: edit repo files in its worktree; commit on the worktree branch; cherry-pick back through the operator. |
| **agent-reviewer** | Persona that reviews a diff or branch and emits severity-tagged findings as `reviewer_report` store records [4:282]. Authority: read everything; emit reports; cannot mutate state. |
| **AGENTS.md** | Project-resident contract file at repo root [11] holding non-negotiable rules every agent (CLI / TUI / runtime adapter) reads. Manifesto Rule 4 says one canonical contract; everything else symlinks [2:50-54]. |
| **artifact** | Tracked file or external resource pinned via `Artifact` record [10:273-291]. Kinds enumerated in `ArtifactKind` [4:288-309]. URN: `urn:eawf:v1:artifact:<scope>/<id>`. |
| **audit** | Audit record [10:259-271] tracking one evaluation / ship-gate / incident / review. Verdicts: PASS / MINOR / MAJOR [4:104-107]. URN: `urn:eawf:v1:audit:<scope>/<id>`. |
| **brief** | Markdown research artifact under `.ea/local/research/` (draft) or `.ea/artifacts/research/` (promoted). Chassis: Summary / References / Provenance / Scrub [11]. Filename `<YYYY-MM-DD>-<slug>.md`. |
| **chassis** | Renderer-owned section set on every durable markdown artifact: Summary / References / Provenance / Scrub [11]. Local drafts carry an `eawf-template` sentinel; promoted artifacts do not. |
| **cluster** | One of the 11 spec briefs (C01..C11) in the long-term spec series [1:273-308]. Each cluster is one fresh CC session, one brief, one ratification AUQ. |
| **CLI** | `uv run eawf <verb> <noun>` surface. Per AGENTS rule 1 the CLI is *dispatch only* — domain logic lives in the library [11]. CLI verbs map to library functions on validated typed objects. |
| **daemon** | `eawfd` process — single coordinator per OS-user. Day-1 mutator surface per V1 [1:24-53]. Auto-spawned on first CLI mutation; idles down after 300 s default. URN: `urn:eawf:v1:daemon:user/<host>`. |
| **decision** | Architectural / process decision record [10:294-305]. Status: ACTIVE / SUPERSEDED / REVERSED [4:110-113]. URN: `urn:eawf:v1:decision:<scope>/<id>`. |
| **dispatcher** | Daemon subsystem that routes a wave dispatch envelope to a runtime per V8 [1:226-271]. Tracks `(wave, attempt)` → `(runtime, session_id)` for retry session-reuse. |
| **envelope** | Three-part output structure (header / body / footer) every skill emits [9]. Pydantic-validated, markdown-and-JSON dual wire-form, byte-stable round-trip. |
| **event** | One row of `event.jsonl` — `Envelope` [25] wrapping an `EventPayload` [26]. Records every state mutation with `actor`, `command`, `args_hash`, `before_state_version`, `after_state_version`. Append-only. URN: `urn:eawf:v1:event:<scope>/<id>`. |
| **hypothesis** | Research-driven testable claim [10:244-256]. Status: PENDING / CONFIRMED / REJECTED / INCONCLUSIVE / DEFERRED [4:76-81]. URN: `urn:eawf:v1:hypothesis:<scope>/<id>`. |
| **iter** | Iteration under a phase; groups waves toward a sub-goal [10:207-218]. Status: PLANNED / ACTIVE / CLOSED / ABANDONED [4:52-56]. ID pattern `P\d{2}-I\d{2}` [15:6]. URN: `urn:eawf:v1:iter:<scope>/<iter-id>`. |
| **manifesto** | The 7-rule + Rule-8-extension framework charter [2]. Names Eä as governed ADD; rules: specs source of truth, agents draft / humans decide, process produces trust, one repo-resident contract, plan before execute, structured state, verify before claiming, phase-bundled delivery. |
| **memory** | Memory record (`MemoryPayload` [22] + `MemorySummary` cache row [10:435-453]). Tier: WORKING / ARCHIVAL / RETRIEVAL [4:226-240]. Status: ACTIVE / STALE / SUPERSEDED / PRUNED [4:219-223]. URN: `urn:eawf:v1:memory:<scope>/<id>`. |
| **operator** | Human persona that approves merges, opens phases, ratifies AUQs. Highest authority in the matrix (§5.5). Never delegates merge authority per manifesto Rule 2 [2:38-42]. |
| **phase** | Top scope below project; bundles iters [10:190-204]. Status: PLANNED / ACTIVE / CLOSED / ARCHIVED [4:45-49]. ID pattern `P\d{2}` [15:5]. URN: `urn:eawf:v1:phase:<scope>/<phase-id>`. |
| **plugin** | Runtime-adapter install record [10:421-432]. Status: INSTALLED / DRIFTED / CONFLICTED / DISABLED [4:212-216]. URN: `urn:eawf:v1:plugin:<scope>/<id>`. |
| **principal** | (v0.5+ reserved) Identity record for an operator or agent [12:486-507]. Carries `caps: frozenset[Capability]` + public key fingerprint for signed events. URN kind reserved in v0.3-v0.5 catalog so the migration doesn't require URN-grammar churn. |
| **profile** | Composable rule-bundle [13:97-122]. Project carries `profiles: [a, b, c]` ordered list per V3 [1:76-96]. Loader contributes skills / hooks / templates / validators / config-layer-values. URN: `urn:eawf:v1:profile:<owner>/<id>`. |
| **profile-author** | Persona that drafts new `ProfileBody` YAMLs under `<local-path>` or `<repo>/.ea/profiles/`. Authority: write profile YAML + run `eawf profile validate`; never mutate state directly. |
| **repo** | Single git working tree carrying a project [10:97-115]. `code` slot is the project code; `slug`, `title`, `description`, `domains`, `default_branch`, `status`, `weekly_eu_target` fields. URN: `urn:eawf:v1:repo:<code>`. Per D2 Repo ≡ Project. |
| **report** | Typed agent-end record per AGENTS rule 19 [11]. `AgentReportBody` Pydantic union per role [23:193-203]. Append-only with `(role, base_id, attempt)` retry tuple [23:222-234]. URN: `urn:eawf:v1:report:<scope>/<id>`. |
| **runtime** | LLM-execution adapter (claude-code / codex / opencode). Configured via `runtime.preference: [a, b, c]` ladder per V5 [1:127-151]. URN: `urn:eawf:v1:runtime:<owner>/<id>`. |
| **runtime-coauthor** | Trailer line on every commit identifying the runtime + model that authored it per AGENTS commit-prefix rule [11]. Recognized values: any `Co-Authored-By:` form matching the canonical Claude or Codex shape. |
| **scope** | Logical addressing unit — `repo`, `workspace`, `phase`, `iter`, `wave`, `session`. Every state-resident row carries a `scope_id` linking to its enclosing scope's URN. |
| **session** | Agent work session (`AgentSession` [10:350-363]). Tracks `runtime`, `claimed_wave_ids`, `worktree_ids`, lifecycle status ACTIVE / CHECKPOINTED / CLOSED / STALE / FAILED [4:183-189]. URN: `urn:eawf:v1:session:<scope>/<id>`. |
| **spec** | Typed Phase / Iter / Wave spec per V2 [1:55-74]. Stored at `.ea/specs/<phase>/[<iter>/]<wave\|spec>.md`. Lifecycle: DRAFT / READY / IMPLEMENTED / ARCHIVED (§5.4). URN: `urn:eawf:v1:spec:<scope>/<id>`. C03 owns the per-tier schema. |
| **subproject** | Sub-workstream under a Repo [10:149-160]. Status: ACTIVE / PLANNED / DEFERRED / RETIRED [4:18-22]. Groups goals + reports under `current.subproject_id` [10:144]. |
| **URN** | Uniform-resource-name string `urn:eawf:v1:<kind>:<path>` [17]. Stable identifier; cross-process, cross-repo, cross-runtime. `identity()` strips query+fragment for equality. |
| **verdict** | Operator-confirmed decision recorded in a brief (e.g. V1..V8 in C00). The discrete unit of architectural commitment; every wave success criterion cites at least one. |
| **wave** | Atomic execution unit under an iter [10:221-241]. Status: PENDING / CLAIMED / IN_PROGRESS / CLOSED / FAILED / ABANDONED [4:59-65]. ID pattern `P\d{2}-I\d{2}-W\d{2}` [15:7]. URN: `urn:eawf:v1:wave:<scope>/<wave-id>`. |
| **watcher** | Daemon-subscribed read-only persona (TUI, web-stub, CI). Receives push notifications on event-bus subscription; never mutates state. |
| **workspace** | Cross-repo aggregation scope [10:129-135]. Lists `WorkspaceRepoRef` entries pointing at on-disk repos. URN: `urn:eawf:v1:workspace:<code>`. |
| **worktree** | Git worktree created for parallel wave dispatch [10:366-377]. Status: ACTIVE / CONFLICTED / MERGED / ABANDONED [4:191-195]. Per AGENTS rule 11 commits cherry-pick into the parent branch — never merge [11]. |

**Additional glossary terms landed 2026-05-18 per BOT-02 (16 contract terms cross-cluster):**

| Term | Definition |
|---|---|
| **dispatch.session_policy** | Per-skill manifest field per V8 [1:226-271]. Values: `fresh` / `continue` / `hybrid` (default). Locks how the daemon routes retry envelopes to existing sessions. Owner: C04b manifest schema. |
| **idempotency key** | Per-dispatch envelope hash that lets the daemon dedupe re-issued dispatches (e.g. operator clicks Retry twice). Persisted on the WAL per XB12 outcome-WAL fix. Owner: C02 daemon protocol. |
| **attempt_id** | Per-`(wave, attempt)` integer counter [10:514+]; identifies one execution attempt for a wave. Increments on retry. Used in `Wave.sessions: dict[attempt_id, SessionAttempt]`. Owner: C01 entity catalog + C04c agent contract. |
| **runtime preference ladder** | Ordered `list[str]` of runtime ids per V5 [1:127-151]. Fallback walks the ladder on retryable error class. Owner: C07a + C08 field registry. |
| **schema_version** | Required field on every Pydantic state model. Value: `Literal["1.0"]` string MAJOR.MINOR per Q5 / BOT-03 lock 2026-05-18. Pre-commit lint rejects deviations. Owner: C01 + C03 + C08. |
| **profile.overrides** | List of profile ids whose contributions THIS profile takes precedence over. Loader fails fast if undeclared overlap detected. Owner: C08 ProfileBody. |
| **profile.conflicts_with** | List of profile ids THIS profile cannot co-load with. Loader fails fast on conflict. Owner: C08 ProfileBody. |
| **state_extensions** | Profile-contributed extra state fields. Materialised on `state.json` at `enable_profile()` time. Owner: C08 ProfileBody + C03 spec validators. |
| **instrument_requirements** | Profile-contributed test markers / coverage gates / pre-commit hooks. Strictest-wins on per-`kind` merge. Owner: C08 ProfileBody + C09 quality gates. |
| **render_blocks** | Profile-contributed chassis sections / overlay tiles / palette extensions. Owner: C08 ProfileBody + C07b renderer + C06 TUI. |
| **cache-control** | V8 cache-interplay hook on runtime adapter; per-runtime breakpoint to mark stable-prefix vs hot-prefix. Owner: C07a RuntimeAdapter Protocol + C09 cache-mislayer alarm. |
| **runtime_switched** | Event subtype emitted on V5 fallback. Carries `(wave, attempt, from, to, error_class, latency_ms)`. Owner: C07b event payload registry (per Q14). |
| **LifecycleError** | Raised when a status transition violates the per-entity lifecycle DAG (§5.4). Owner: C01 lifecycle module + C02 daemon mutator. |
| **_StrictModel** | Internal `BaseModel` alias with `model_config = ConfigDict(extra="forbid")`. Every state-resident schema subclasses. Owner: C01 entity catalog. |
| **actor** | Field on every event envelope identifying which persona issued the mutation. Currently hardcoded `"cli"`; migrates to `Principal.id` per XB08 / Q3 (placeholder field landed v0.3-v0.5; full enforcement v0.5+). Owner: C01 trust model + C07b event envelope. |
| **before_state_version / after_state_version** | `state.json` schema_version snapshots wrapping each mutation in the event envelope. Used for audit replay + migration boundary detection. Owner: C07b event envelope. |

Total entries after BOT-02: **56**. Outside the C00-mandated 30-50 window but each term load-bearing for the cross-cluster contract.

### 5.2 URN scheme

#### 5.2.1 Grammar

The base grammar (today's code [17:30-34]) stays:

```
urn:eawf:<version>:<kind>:<path>[?=<query>][#<fragment>]
```

- `<version>` = `v1` for v0.3 → v0.5. v2 requires a brief and an Open Question per AGENTS rule 20 [11].
- `<kind>` = one of the **26** single-word tokens enumerated below (count audited 2026-05-18 per XB15; expansion landed in C01-W01 with golden fixture; URN_KINDS frozenset in `src/eawf/state/urn.py` MUST be expanded to match before C03 spec verbs ratify). Pattern: `^[a-z]+$` (no underscore, no dash).
- `<path>` = `<owner>[/<id>]`. `owner` is the enclosing scope identifier (project code, repo code, workspace code, user, host). `id` is the entity-specific ID within that scope.
- `<query>` = `?=k=v&k=v` for non-equality-relevant decoration (rendered-revision pinning, tier override). `identity()` strips it [17:48-52].
- `<fragment>` = `#anchor` for in-document positioning. Also stripped by `identity()`.

The `_SLASH_KINDS` frozenset [17:35] enumerates kinds whose `<id>` may itself contain a slash (today: `repo`, `artifact`, `store`). C01 extends the slash-friendly set so that `spec` paths can encode `<phase>/<iter>/<wave>` and `report` paths can encode `<role>/<base_id>-<attempt>`. Updated `_SLASH_KINDS` (proposed): `repo`, `artifact`, `store`, `spec`, `report`, `event`, `memory`, `session`, `plugin`, `mcp`.

#### 5.2.2 Kind catalog

Each row: kind (single-word token) + owner-segment shape + id-segment shape + example + purpose. **26 kinds total.** Operator §4 D1 confirmed broad scope, single-word tokens.

| Kind | Owner | Id | Example | Purpose |
|---|---|---|---|---|
| `workspace` | code | — | `urn:eawf:v1:workspace:tm-dev` | Cross-repo aggregation scope. Carries `WorkspaceIndex` [10:129-135]. One per user-defined workspace folder. |
| `repo` | code | (subpath allowed) | `urn:eawf:v1:repo:eawf` | Single project = single repo per D2. Equal to project-code. Carries `Project` record [10:97-115]. |
| `state` | scope-code | — | `urn:eawf:v1:state:eawf` | The `state.json` document URN. Owns every scope-internal row by reference. |
| `phase` | scope-code | phase-id | `urn:eawf:v1:phase:eawf/p20` | Phase row [10:190-204]. Lowercase id form on the wire (per `urn` quoting). |
| `iter` | scope-code | iter-id | `urn:eawf:v1:iter:eawf/p20-i03` | Iter row [10:207-218]. |
| `wave` | scope-code | wave-id | `urn:eawf:v1:wave:eawf/p20-i03-w01` | Wave row [10:221-241]. |
| `hypothesis` | scope-code | hyp-id | `urn:eawf:v1:hypothesis:eawf/h03-12` | Hypothesis record [10:244-256]. |
| `decision` | scope-code | dec-id | `urn:eawf:v1:decision:eawf/d-2026-05-16-urn-scope` | Decision record [10:294-305]. ID pattern: `d-<YYYY-MM-DD>-<slug>` (proposed; C03 confirms). |
| `audit` | scope-code | aud-id | `urn:eawf:v1:audit:eawf/aud-p20-i03-ship-gate` | Audit record [10:259-271]. |
| `artifact` | scope-code | artifact-id (path-shaped) | `urn:eawf:v1:artifact:eawf/.ea/artifacts/research/2026-05-16-c00-spec-index.md` | Tracked artifact [10:273-291]. Slash-friendly so the on-disk path can be the id directly. |
| `store` | scope-code | `<kind>/<id>` | `urn:eawf:v1:store:eawf/research/RES-2026-05-16-c01` | Store JSONL record [25]. The `<kind>` segment matches `StoreKind` [4:268-285]. |
| `blob` | scope-code | sha256 prefix | `urn:eawf:v1:blob:eawf/sha256:8a4f...` | Content-addressable blob (event-source rebuilder snapshot per R3 [29:443-446], future). |
| `memory` | scope-code | mem-id | `urn:eawf:v1:memory:eawf/mem-2026-05-15-naming` | Memory record [10:435-453]. Slash-friendly for tier/path encoding. |
| `report` | scope-code | `<role>/<base_id>-<attempt>` | `urn:eawf:v1:report:eawf/executor/p20-i03-w01-01` | Typed agent report record per AGENTS rule 19 [11]. Slash-friendly so role + base + attempt land cleanly. Replaces the legacy `agent_report` form (single-word per D1). |
| `spec` | scope-code | `<phase>[/<iter>[/<wave>]]` | `urn:eawf:v1:spec:eawf/p20/i03/w01` | Spec at `.ea/specs/<phase>/[<iter>/]<wave\|spec>.md` [1:69-73]. Slash-friendly tier-aware path. C03 schema. Lifecycle DRAFT → READY → IMPLEMENTED → ARCHIVED. |
| `profile` | owner (`user` or repo-code) | profile-id | `urn:eawf:v1:profile:user/engineering` | Profile manifest [13:74-94]. Owner = `user` for <local-path> repo-code for repo-pinned overlays. |
| `runtime` | owner (`user`) | runtime-id | `urn:eawf:v1:runtime:user/claude-code` | Runtime adapter handle [1:127-151]. Owner stays `user`-scope; per-wave override binds to the wave URN. |
| `session` | scope-code | session-id | `urn:eawf:v1:session:eawf/s-2026-05-16-abc123` | AgentSession row [10:350-363]. Slash-friendly when sessions carry per-attempt continuation paths. |
| `event` | scope-code | event-id | `urn:eawf:v1:event:eawf/e-2026-05-16-0001-mutation` | Event-log envelope [25]. Slash-friendly when carrying year/sequence. |
| `principal` | owner (`user` or repo-code) | principal-id | `urn:eawf:v1:principal:user/u-abc123` | (Reserved per D4.) Identity record [12:486-507]. Owner = `user` for cross-repo, repo-code for repo-pinned overlay. |
| `plugin` | scope-code | plugin-id | `urn:eawf:v1:plugin:eawf/claude-skills` | PluginInstall row [10:421-432]. Slash-friendly when carrying runtime/version segments. |
| `mcp` | scope-code | server-id | `urn:eawf:v1:mcp:eawf/filesystem` | McpServer + McpGrant rows [10:380-419]. Slash-friendly when carrying server/grant scope. |
| `pr` | repo-code | pr-number | `urn:eawf:v1:pr:eawf/123` | GitHub PR handle (current — kept) [17:25]. |
| `commit` | repo-code | sha | `urn:eawf:v1:commit:eawf/3b86f7a` | Git commit (current — kept) [17:26]. |
| `branch` | repo-code | branch-name | `urn:eawf:v1:branch:eawf/feature%2Feawf-v0.3-p20` | Git branch (current — kept) [17:27]. Forward slashes in branch names percent-encoded. |
| `secret` | scope-code | secret-id | `urn:eawf:v1:secret:eawf/${ENV:ANTHROPIC_API_KEY}` | Env-var-bound secret reference (current — kept) [17:28]. Never resolved into a URN value; only the binding name is recorded. |

**Removed from operator-confirmed broad scope:** none. The earlier narrow set [17:16-29] gains 16 kinds; legacy 10 retained. Total **26 kinds**.

**Entities without their own URN kind (v0.3-v0.5).** Goal, Outcome, BacklogItem, EstimateSummary, ActualSummary, WorktreeRecord, SandboxPolicy, Incident, Flow, Subproject — these state-resident rows are addressable via the composite key `(state_urn, kind_python_name, id)` rather than a dedicated URN. Rationale: C00's entity catalog target [1:318-319] enumerates the rows that *need* cross-process addressing; these supplementary rows are scope-internal and don't appear in dispatch envelopes, federation payloads, or audit URN chains. Promotion to first-class URN kinds is an Open Question for v0.5+ (§8 Q14).

#### 5.2.3 Path-grammar edge cases

- **Owner cannot contain `/`** [17:97-98] — the `_SLASH_KINDS` flag governs only the `<id>` segment.
- **Owner is percent-decoded on parse, percent-encoded on build** [17:71-80, 102-103]. Workspaces with spaces become `urn:eawf:v1:workspace:tm%20dev`.
- **Lowercase canonical form for IDs.** Phase / iter / wave IDs are stored uppercase in `state.json` (`P20`, `P20-I03`, `P20-I03-W01`) per [15:5-7] but the URN form lowercases them. Rationale: URNs are case-insensitive per RFC 8141, and lowercase is more URL-friendly. The parser preserves whatever case appears in the URN; equality checks normalize via `.lower()`.
- **Fragment is positional, not load-bearing.** `urn:eawf:v1:wave:eawf/p20-i03-w01#success_criteria` points at a sub-region of the row's render output; the URN's `identity()` strips it [17:48-52].
- **Query string format `?=k=v` (leading `=`) is unusual** [17:33] — preserved for current-code compatibility; C05 may revisit but C01 keeps it.
- **Unknown kind on parse raises `ValueError`** [17:62-67]. The kind set is closed; new kinds require a C01 update.

### 5.3 Entity catalog

Each subsection: purpose + Pydantic v2 sketch (field-level) + status enum + lifecycle DAG cross-link + invariants this brief locks (cross-entity, not schema-level). Existing fields cite current code; new fields are marked `(new)`.

#### 5.3.1 Repo (= Project)

**Purpose.** The single top-level project record per repo. Per D2 Repo ≡ Project; `WorkspaceIndex` aggregates many Repos for cross-repo views. Subprojects nest underneath when one Repo carries multiple workstreams.

```python
class Repo(_StrictModel):  # was Project [10:97-115]
    code: ProjectCodeStr             # ^[A-Z][A-Z0-9_-]{1,15}$
    slug: str
    title: str
    description: str | None = None
    domains: list[str]
    default_branch: str
    status: ProjectStatus            # ACTIVE | ARCHIVED | RETIRED [4:12-15]
    repo_urn: UrnStr                 # urn:eawf:v1:repo:<code>
    weekly_eu_target: float | None = None
```

**Lifecycle.** §5.4.1.

**Invariants.**

- `code` equals the segment after `urn:eawf:v1:repo:` in `repo_urn` (today's pattern, [17:84-104]).
- Exactly one Repo per `state.json`. `State.project: Repo | None` [10:496] retains the optional shape so workspace-only states (no per-repo) still validate.
- `WorkspaceIndex.repos: dict[code, WorkspaceRepoRef]` [10:131] is the cross-repo aggregator; a workspace lists Repos by code + path + state-URN.

#### 5.3.2 Subproject

**Purpose.** Sub-workstream under a Repo. Groups goals, outcomes, and (via `scope_id`) waves under a sub-id. Operator note (§4 D2): "maintain Subprojects inside too — scope-defined, but think what features they change". Today's features: `current.subproject_id` [10:144] biases the dispatch envelope title, the TUI scope breadcrumb, and report aggregation. Future features (deferred to C04/C06): subproject-scoped weekly EU target rollup, subproject-scoped audit-replay window.

```python
class Subproject(_StrictModel):  # [10:149-160] unchanged
    id: ProjectCodeStr
    code: ProjectCodeStr
    slug: str
    title: str
    kind: str
    domains: list[str]
    status: SubprojectStatus     # ACTIVE | PLANNED | DEFERRED | RETIRED [4:18-22]
    owner: str | None = None
    goal_ids: list[str] = []
```

**Lifecycle.** §5.4.2.

**Invariants.** `id == code` (no de-duplication across the two); subprojects are addressed by composite `<repo-code>/<subproject-code>` when cross-referenced via URN — though Subproject does *not* get its own URN kind (it is a scope marker only, not a state-resident root entity).

#### 5.3.3 Phase

**Purpose.** Top scope below project. Bundles iters toward a single ship-PR per Rule 8 [2:75-78].

```python
class Phase(_StrictModel):  # [10:190-204] unchanged
    id: PhaseIdStr               # ^P\d{2}$
    scope_id: str
    subproject_id: str | None = None
    title: str
    status: PhaseStatus          # PLANNED | ACTIVE | CLOSED | ARCHIVED [4:45-49]
    iter_ids: list[IterIdStr] = []
    outcome_ids: list[str] = []
    depends_on: list[PhaseIdStr] = []
    source_brief_ids: list[str] = []
    opened_at: UtcDatetime
    closed_at: UtcDatetime | None = None
    audit_id: str | None = None
```

**Lifecycle.** §5.4.3.

**Invariants.** Closure rules in `eawf.lifecycle.transitions` [16] forbid closing while any owned iter is non-terminal. `source_brief_ids` may carry research-brief URNs (the v0.3 form is store-record IDs; C03 adopts URN form).

#### 5.3.4 Iter

**Purpose.** Iteration under a phase. Groups waves toward a sub-goal of the phase outcome.

```python
class Iter(_StrictModel):  # [10:207-218] unchanged
    id: IterIdStr                # ^P\d{2}-I\d{2}$
    phase_id: PhaseIdStr
    title: str
    status: IterStatus           # PLANNED | ACTIVE | CLOSED | ABANDONED [4:52-56]
    wave_ids: list[WaveIdStr] = []
    estimate_id: str | None = None
    audit_id: str | None = None
    opened_at: UtcDatetime
    closed_at: UtcDatetime | None = None
```

**Lifecycle.** §5.4.4.

**Invariants.** `phase_id` equals `parents_of(self.id)[0]` [15:71-90]. Closing requires all owned waves CLOSED or ABANDONED.

#### 5.3.5 Wave

**Purpose.** Atomic execution unit. Worktree-isolated, cherry-picked back per Rule 8 [2:75-78].

```python
class Wave(_StrictModel):  # extends [10:221-241]
    id: WaveIdStr                # ^P\d{2}-I\d{2}-W\d{2}$
    iter_id: IterIdStr
    title: str
    status: WaveStatus           # PENDING | CLAIMED | IN_PROGRESS | CLOSED | FAILED | ABANDONED [4:59-65]
    deps: list[WaveIdStr] = []
    blocks: list[WaveIdStr] = []
    file_scopes: list[str] = []
    success_criteria: list[str] = []
    agent_role: AgentSessionRole | None = None
    effort_bucket: EffortBucket | None = None
    claim_session_id: str | None = None
    worktree_id: str | None = None
    token_budget: int | None = None
    tokens_consumed: int = 0
    outcome: str | None = None
    commit: ShaStr | None = None
    opened_at: UtcDatetime
    closed_at: UtcDatetime | None = None
    # (new — V8 session-handle map) — proposed C01 surface, C02 implements daemon-side
    sessions: dict[int, SessionAttempt] = {}  # attempt -> handle
    runtime_preference: list[str] | None = None  # per-wave override [1:140-141]
    dispatch_history: list[DispatchAnnotation] = []  # V8 [1:268-269]
```

```python
class SessionAttempt(_StrictModel):
    attempt: Annotated[int, Field(ge=1)]
    runtime: str
    session_id: str
    started_at: UtcDatetime
    ended_at: UtcDatetime | None = None

class DispatchAnnotation(_StrictModel):
    attempt: Annotated[int, Field(ge=1)]
    note: str                     # 'fresh dispatch' | 'continue from session' | 'continue failed -> fresh' [V8]
    runtime_from: str | None = None
    runtime_to: str | None = None
```

**Lifecycle.** §5.4.5.

**Invariants.** Claim order rule [11]: `eawf wave claim` rejects if any dep wave is not CLOSED or if a lower-numbered sibling is still PENDING with its own deps satisfied (escape hatch: `--out-of-order`). Cherry-pick rule: `commit` is the cherry-picked SHA on the parent branch, not the worktree SHA. Per V8, on runtime fallback the new session is opened fresh and appended to `sessions`; the old attempt's `ended_at` is set and a `DispatchAnnotation` recorded.

#### 5.3.6 Hypothesis

**Purpose.** Research-driven testable claim with confirm/reject thresholds.

```python
class Hypothesis(_StrictModel):  # [10:244-256] unchanged
    id: HypothesisIdStr           # ^H\d{2}-\d{2}$ or scoped
    scope_id: str
    text: str
    metric: str
    confirm: str
    reject: str
    status: HypothesisStatus      # PENDING | CONFIRMED | REJECTED | INCONCLUSIVE | DEFERRED [4:76-81]
    verdict: HypothesisVerdict | None = None
    audit_id: str | None = None
    source_artifact_id: str | None = None
```

**Lifecycle.** §5.4.6.

**Invariants.** Verdict (CONFIRMED / REJECTED / INCONCLUSIVE [4:84-87]) requires `audit_id` set (cited audit's verdict is the evidence chain head per §5.6).

#### 5.3.7 Decision

**Purpose.** Architectural / process decision with rationale + alternatives.

```python
class Decision(_StrictModel):  # [10:294-305] unchanged
    id: IdStr                     # proposed pattern: d-<YYYY-MM-DD>-<slug>
    scope_id: str
    summary: str
    rationale: str
    alternatives: list[str] = []
    status: DecisionStatus        # ACTIVE | SUPERSEDED | REVERSED [4:110-113]
    created_at: UtcDatetime
    superseded_by: str | None = None
```

**Lifecycle.** §5.4.7.

**Invariants.** `superseded_by` forms a DAG (acyclic, eventually rooted at an ACTIVE decision). `alternatives` is free-form text, not URN refs (C03 may tighten).

#### 5.3.8 Audit

**Purpose.** Records one evaluation / ship-gate / incident / review pass.

```python
class Audit(_StrictModel):  # [10:259-271] unchanged
    id: IdStr
    scope_id: str
    kind: AuditKind                  # EVALUATION | SHIP-GATE | INCIDENT | REVIEW [4:90-94]
    status: AuditStatus              # PENDING | RUNNING | COMPLETE | FAILED [4:97-101]
    report_artifact_id: str | None = None
    check_results: list[Any] = []    # CheckResult [27:9-17]
    integrity_results: list[Any] = []
    created_at: UtcDatetime
    verdict: AuditVerdict | None = None  # PASS | MINOR | MAJOR [4:104-107]
```

**Lifecycle.** §5.4.8.

**Invariants.** `verdict` set only when `status == COMPLETE`. Wave / Iter / Phase closure rules require an `audit_id` set when the audit kind is `SHIP-GATE` and the scope is being closed.

#### 5.3.9 Artifact

**Purpose.** Tracked file or external resource. Kind values pinned by `ArtifactKind` [4:288-309].

```python
class Artifact(_StrictModel):  # [10:273-291] unchanged
    id: IdStr
    kind: str                     # transitional; v0.4 closes to ArtifactKind enum [10:281-282]
    uri: str
    urn: UrnStr                   # urn:eawf:v1:artifact:<scope>/<path>
    sha256: str | None = None
    size_bytes: int | None = None
    created_at: UtcDatetime
    metadata: dict[str, Any] = {}
```

**Lifecycle.** §5.4.9.

**Invariants.** `urn` and `uri` agree: `uri` is the on-disk or remote path; `urn` is the eawf-form identifier. Artifacts may be promoted from `.ea/local/` to `.ea/artifacts/` once and only once.

#### 5.3.10 Memory

**Purpose.** Memory entry — append-only `memory.jsonl` with cache row in `state.memory_index`.

```python
class MemorySummary(_StrictModel):  # [10:435-453] unchanged
    id: IdStr
    scope_id: str
    summary: str
    confidence: Confidence              # HIGH | MEDIUM | LOW [4:243-246]
    status: MemoryStatus                # ACTIVE | STALE | SUPERSEDED | PRUNED [4:219-223]
    store_record_id: str
    review_due: UtcDatetime | None = None
    promoted_to_artifact_id: str | None = None
    tier: MemoryTier = MemoryTier.WORKING  # WORKING | ARCHIVAL | RETRIEVAL [4:226-240]

class MemoryPayload(_StrictModel):  # [22:12-36]
    body: str
    confidence: Confidence
    review_due: datetime | None = None
    promoted_to_artifact_id: str | None = None
    expired_at: datetime | None = None
```

**Lifecycle.** §5.4.10.

**Invariants.** Per D6, the (`scope_id`, `tier`) pair uniquely namespaces memory recall. Promotion sets `promoted_to_artifact_id`; supersession creates a new MemorySummary with `status=SUPERSEDED` referencing the predecessor via the `superseded_by`-style backlink on the new payload's body (today's `memory.promotion` module [28] enforces).

#### 5.3.11 Report (= AgentReport)

**Purpose.** Typed agent-end record per AGENTS rule 19 [11]. One row per `(role, base_id, attempt)` tuple.

```python
# Today's shape [23:53-203]
class AgentReportHeader(_StrictModel):
    report_id: str               # AR-<role>-<base>-<attempt>
    role: AgentSessionRole       # RESEARCHER | PLANNER | EXECUTOR | AUDITOR | REVIEWER | POLISHER | OPERATOR | DOMAIN_SPECIALIST [4:165-173]
    session_id: str
    scope_id: str
    base_id: str
    attempt: Annotated[int, Field(ge=1)]
    runtime: str
    generated_at: UtcDatetime
    summary: str
    artifact_ids: list[str] = []
    blob_refs: list[str] = []

class AgentReportCommonBody(_StrictModel):
    verdict: AgentReportVerdict  # PASS | PASS_WITH_FOLLOWUPS | FAIL | BLOCKED [4:177-180]
    confidence: Confidence
    summary: str
    evidence_refs: list[AgentReportEvidenceRef] = []
    followups: list[AgentReportFollowup] = []
    # role-specific body extends with role discriminator [23:117-203]
```

**Lifecycle.** §5.4.11 (very thin — append-only, no transitions).

**Invariants.** Per D7 + AGENTS rule 19 [11]: append-only; retry appends a new row with `attempt = max(prior)+1`; "latest" projection is computed read-side. URN kind `report` (single-word per D1); legacy `agent_report` aliased on read for backwards compat during the migration.

#### 5.3.12 AgentSession

**Purpose.** Tracks a runtime-execution context — claimed waves, owned worktrees, accumulated artifacts.

```python
class AgentSession(_StrictModel):  # [10:350-363] unchanged
    id: IdStr
    role: AgentSessionRole
    runtime: str
    scope_id: str
    status: AgentSessionStatus    # ACTIVE | CHECKPOINTED | CLOSED | STALE | FAILED [4:183-189]
    claimed_wave_ids: list[WaveIdStr] = []
    worktree_ids: list[str] = []
    artifact_ids: list[str] = []
    started_at: UtcDatetime
    ended_at: UtcDatetime | None = None
    summary: str | None = None
```

**Lifecycle.** §5.4.12.

**Invariants.** Per V8 [1:226-271], `session_id` carries the runtime-specific continuation handle (e.g. `claude --continue <id>`). Cross-runtime fallback (V5) starts a new session — handles are runtime-specific, never portable.

#### 5.3.13 Event

**Purpose.** One row of `event.jsonl`. Records every mutation envelope.

```python
class EventPayload(_StrictModel):  # [26:10-24] today's shape
    timestamp: datetime
    event_type: str               # 70+ stringly-typed values [29:265]
    actor: str                    # hardcoded 'cli' literal at 16+ sites today [29:484-489]
    command: str
    args_hash: str
    before_state_version: str | None = None
    after_state_version: str | None = None
    status: str
    message: str
```

Wrapped by `Envelope` [25]:

```python
class Envelope(_StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    id: str
    kind: StoreKind              # EVENT for this row [4:268-285]
    scope_id: str | None
    created_at: UtcDatetime
    updated_at: None             # event rows force updated_at=None [25:35-39]
    summary: str
    payload: dict[str, Any]      # validates as EventPayload
    blob_refs: list[str] = []
    artifact_ids: list[str] = []
```

**Lifecycle.** §5.4.13.

**Invariants.** Per D5 the Event log is mutation provenance — every `state.json` write writes one row first. Per V7 telemetry projects from this row but does not own it. C03 typed-Mutation discriminated union on `payload` replaces the stringly-typed `event_type` field in a later phase.

#### 5.3.14 Profile

**Purpose.** Composable rule-bundle per V3 [1:76-96].

```python
class ProfileBody(_StrictModel):  # [13:74-94] today's shape
    name: str
    version: str = "1.0"
    description: str = ""
    extends: str | None = None
    state_extensions: StateExtensions = StateExtensions()
    instrument_requirements: list[InstrumentReq] = []
    render_blocks: list[RenderBlock] = []
    skills_referenced: list[str] = []
    hooks_referenced: list[str] = []

class ComposedProfile(_StrictModel):  # [13:97-122] today's shape
    name: str
    version: str
    description: str
    state_extensions: StateExtensions
    instrument_requirements: list[InstrumentReq]
    render_blocks: list[RenderBlock]
    skills_referenced: list[str]
    hooks_referenced: list[str]
    provenance: dict[str, list[str]]  # field -> contributors
```

**C08 will add:** `conflicts_with: list[str]`, `overrides: list[str]` per V3 conflict-declaration semantics [1:78-79]; today's shape is composition-by-union without explicit conflict surface.

**Lifecycle.** §5.4.14.

**Invariants.** Project carries `profiles: [a, b, c]` ordered list; loader fails fast when conflicts undeclared (C08 owns the algorithm). URN `urn:eawf:v1:profile:<owner>/<id>` — owner is `user` for global, repo-code for repo-pinned overlays.

#### 5.3.15 Spec

**Purpose.** Typed PhaseSpec / IterSpec / WaveSpec per V2. Filesystem-only per D3 — body lives at `.ea/specs/<phase>/[<iter>/]<wave|spec>.md` with Pydantic-validated YAML frontmatter. C03 owns the per-tier schema; C01 locks the entity name, URN kind, lifecycle, and archival behaviour.

**Storage paths (V2 [1:69-73]):**

```
.ea/specs/P20/spec.md                  # PhaseSpec
.ea/specs/P20/I03/spec.md              # IterSpec
.ea/specs/P20/I03/W01.md               # WaveSpec
```

**URN:** `urn:eawf:v1:spec:<scope>/<phase>[/<iter>[/<wave>]]` — single URN, slash-friendly.

**No state row.** Per D3 there is no `State.specs` dict. Phase / Iter / Wave rows do not carry a `spec_path` field; the spec URN is derivable from the entity's id. The daemon optionally caches a `spec_index` (per-phase SHA + lifecycle status) in `<local-path>` so `eawf spec show` can find archived specs without `git log` round-trip.

**Per-tier sketch (C03 finalises):**

```python
# Sketch only — C03 §5 owns the schema
class WaveSpec(_StrictModel):
    schema_version: Literal["1.0"]
    kind: Literal["WaveSpec"]
    id: WaveIdStr
    title: str
    implements: list[VerdictCitation]    # (verdict_id, brief, line) per V2 [1:60-62]
    file_scopes: list[str]
    behaviors: list[str]                 # B1..Bn
    failure_modes: list[str]
    tests: list[str]                     # references real test paths
    mockup: str | None = None            # required for UI scopes (heuristic in C03)

class VerdictCitation(_StrictModel):
    verdict_id: str
    brief: str                            # repo-relative
    line: int | None = None
```

**Lifecycle.** §5.4.15. Per D3 the archival transition `git rm`s the file on phase close; daemon caches the last SHA so the spec is restorable from `git log`.

**Invariants.** `implements` must be non-empty (every wave cites at least one verdict per the verify-before-claim rule [11]). `tests` entries must point at real test paths (pre-commit hook in C03). Mockup required for UI scopes (heuristic: file scopes under `src/eawf/tui_v2/` or `src/eawf/render/` — C03 finalises).

#### 5.3.16 Runtime

**Purpose.** LLM-execution adapter handle per V5 [1:127-151]. Today's runtime is a string field on AgentSession / Wave; C01 reifies it as an entity with URN + status so the audit-replay model (§5.6) can address it.

```python
# New entity, proposed shape (C07 finalises)
class Runtime(_StrictModel):
    id: str                           # 'claude-code' | 'codex' | 'opencode'
    urn: UrnStr                       # urn:eawf:v1:runtime:user/<id>
    status: RuntimeStatus             # CONFIGURED | HEALTHY | DEGRADED | UNAVAILABLE
    binary_path: str | None = None
    version: str | None = None
    last_health_check: UtcDatetime | None = None  # advisory only — V5 is reactive, not probed [1:131-136]
    accepts_continue: bool            # V8 [1:228-232] — does this runtime support session resume?
    supports_cache_control: bool      # V8 cache-control interplay [1:251-254]
    error_classes_emitted: list[str]  # V5 [1:130] — RUNTIME_RATE_LIMIT | RUNTIME_SERVER_ERROR | RUNTIME_TIMEOUT | ...
```

**RuntimeStatus enum (new):**

```python
class RuntimeStatus(StrEnum):
    CONFIGURED = "configured"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
```

**Lifecycle.** §5.4.16.

**Invariants.** `runtime.preference: list[Runtime.id]` in `<local-path>` [1:139] orders the fallback ladder. Per V5 [1:144-147] daemon never silently rewrites `Wave.runtime`; it emits a `runtime_switched` event and re-issues against the next adapter.

#### 5.3.17 Plugin

**Purpose.** Runtime plugin install record.

```python
class PluginInstall(_StrictModel):  # [10:421-432] unchanged
    id: IdStr
    owner: str                       # 'eawf' for our installs
    runtime: str                     # matches Runtime.id
    scope_id: str
    target_path: str
    status: PluginInstallStatus      # INSTALLED | DRIFTED | CONFLICTED | DISABLED [4:212-216]
    managed_files: list[str] = []
    installed_at: UtcDatetime
    updated_at: UtcDatetime
```

**Lifecycle.** §5.4.17.

**Invariants.** `managed_files` is the authoritative list of paths the installer may write; drift detection compares on-disk SHA to recorded value.

#### 5.3.18 McpServer + McpGrant

**Purpose.** MCP server config (`McpServer`) + scope-binding grant (`McpGrant`) so the dispatcher knows which servers are allowed under which scope.

```python
class McpServer(_StrictModel):  # [10:380-394] unchanged
    id: IdStr
    owner: str
    command: str
    args: list[str] = []
    env_refs: list[str] = []
    risk: McpRisk                    # READ | READ_WRITE | ADMIN [4:198-201]
    write_capable: bool
    status: McpStatus                # NOT_CONFIGURED | CONFIGURED | INSTALLED | DEGRADED | DISABLED [4:204-209]
    installed_targets: list[str] = []

class McpGrant(_StrictModel):  # [10:396-419] unchanged
    id: IdStr
    scope_kind: McpGrantScopeKind    # wave | profile | global [10:84-85]
    scope_id: str
    server_id: IdStr
    granted_at: UtcDatetime
```

**Lifecycle.** §5.4.18.

**Invariants.** `server_id` must reference an extant `state.mcp_servers` entry [10:400-407]; the referential check lives in `eawf.validate.invariants.check_mcp_grant_server_ref` [10:407-408].

#### 5.3.19 Principal (minimum model — v0.3-v0.5; full enforcement v0.5+)

**Purpose.** Identity record for an operator or agent. Today's `EventPayload.actor` field is the literal string `"cli"` at 16+ sites [29:484-489]; Principal is the v0.5+ replacement that carries capabilities + a public-key fingerprint for ed25519-signed events.

**Update 2026-05-18 (XB08 / Q3 supersede).** Original D4 deferred Principal entirely to v0.5+. Operator Q3 (2026-05-18) reverses: **minimum Principal model lands in v0.3-v0.5** as a placeholder field. Full enforcement still v0.5+; the field shape stabilises now so query side + telemetry projection can be typed today.

**Minimum Principal model (v0.3-v0.5 landing):**

```python
from typing import Literal

class Principal(_StrictModel):
    id: str                              # 'u-<8hex>' pattern; lowercase, ascii
    kind: Literal["operator", "agent", "cli"]   # 'cli' = legacy CLI dispatch
    display_name: str

# EventPayload.actor: str — KEEP for v0.3-v0.5 backward compat
# EventPayload.actor_principal_id: str | None = None — placeholder; populated when known
# Cost.attributed_to: Literal["cli"] = "cli" — placeholder for v0.5+ per-principal cost attribution
```

Migration target (v0.5+) preserved below for documentation:

**What is Principal?** A typed identity row per R5 [12:486-507]. Fields (sketched, finalised in the v0.5+ governance phase):

```python
# Sketch only — NOT in v0.3-v0.5 state schema. Documented here so the migration target is known.
class Principal(_StrictModel):
    id: str                              # 'u-<8hex>' pattern; lowercase, ascii
    email: str
    public_key_fingerprint: str          # sha256 of ed25519 pubkey
    caps: frozenset[Capability]          # CLAIM_WAVE | RELEASE_WAVE | ACTIVATE_PHASE | CLOSE_PHASE | REOPEN_PHASE | MUTATE_POLICY | SIGN_RELEASE | REVIEW | ADMIN [12:503]
    added_at: UtcDatetime
    revoked_at: UtcDatetime | None = None

# v0.5+ migration target:
# EventPayload.actor: str  →  EventPayload.actor_principal_id: str
# EventPayload.signature: str | None    (new — base64 ed25519)
# Principal database at .ea/principals/<id>.json + <local-path> overlay [12:506-507]
```

**Why reserve the URN kind now?** So that the v0.5+ migration can write `urn:eawf:v1:principal:user/u-abc123` strings into AgentSession.runtime, Event.actor, and federation-handshake payloads without C01-grammar churn. The grammar is fixed once.

**Why defer the entity?** The 16+ site rename + ed25519 infra + per-repo principal database is a phase-sized effort per R5 [12:556-566] and lands no value until federation or multi-user dispatch arrives. v0.3-v0.5 stays single-user, single-runtime-process; the existing `AgentSession.runtime` + `EventPayload.actor='cli'` shape is sufficient.

#### 5.3.20 Other current entities (kept verbatim, cited for completeness)

The following current entities [10] are unchanged by C01 — they retain today's shape and lifecycle. C01 catalogues them so downstream clusters know they exist and where to find the schema:

- **Goal** [10:163-173] — quantitative outcome attached to a project / subproject.
- **Outcome** [10:176-187] — measurable target on a metric. DIRECTION: MIN / MAX / EQUAL / RANGE [4:38-42].
- **BacklogItem** [10:307-318] — triaged backlog entry. Priority: P0..P3 [4:116-120].
- **EstimateSummary** [10:321-334] / **ActualSummary** [10:337-347] — EU estimate + actual rollups (`current_store_record_id` points at the latest store row; full history in `estimate.jsonl` / `actual.jsonl`).
- **WorktreeRecord** [10:366-377] — git-worktree provenance.
- **SandboxPolicy** [10:518] — sandbox profile (Pydantic shape in `eawf.sandbox.policy`).
- **Incident** [10:456-468] — incident record (full timeline in `incidents.jsonl`).
- **Flow** [10:471-483] — long-running flow with budgets + safe checkpoints (used by `/loop` skill).

### 5.4 Lifecycle state machines

One DAG per entity. Notation: `STATUS` (enum value); `→` is a transition; the gate `[predicate]` annotates the transition.

#### 5.4.1 Repo

```
              ┌─────────────────┐
              │   not-yet-init  │  (not in state — pre-`eawf init`)
              └────────┬────────┘
                       │ eawf init
                       v
                ┌─────────────┐
                │   ACTIVE    │
                └─────┬───────┘
                      │ eawf repo archive
                      v
                ┌─────────────┐
                │  ARCHIVED   │  read-only; daemon refuses mutations
                └─────┬───────┘
                      │ eawf repo retire
                      v
                ┌─────────────┐
                │   RETIRED   │  removed from workspace index
                └─────────────┘
```

C03 / C05 own the CLI verbs `eawf repo archive` / `eawf repo retire` (today's CLI doesn't surface them; status enum already exists [4:12-15]).

#### 5.4.2 Subproject

```
       eawf subproject add
              │
              v
   ┌─────────────────┐
   │     ACTIVE      │ ───────────┐
   └────────┬────────┘            │ eawf subproject defer
            │                     v
            │              ┌─────────────┐
            │              │   DEFERRED  │
            │              └─────┬───────┘
            │                    │ reactivate
            │                    v
            │              (back to ACTIVE)
            │ retire (only when no active goals + waves)
            v
   ┌─────────────────┐
   │     RETIRED     │
   └─────────────────┘

       eawf subproject add --planned
              │
              v
   ┌─────────────────┐
   │    PLANNED      │ ── activate ──→ ACTIVE
   └─────────────────┘
```

#### 5.4.3 Phase

Direct from `eawf.lifecycle.transitions.{open_phase, close_phase, activate_phase}` [16]:

```
   eawf roadmap propose P##
              │
              v
   ┌─────────────────┐
   │    PLANNED      │ ──── /prep activate ────┐
   └─────────────────┘                          │
            ▲                                   v
            │                          ┌─────────────────┐
            │  eawf phase reopen       │     ACTIVE      │
            │  (rule 20 [11])          └────────┬────────┘
            │                                   │ close (all iters CLOSED, ship-gate audit PASS)
            └───────────────────────────────────┤
                                                v
                                       ┌─────────────────┐
                                       │     CLOSED      │
                                       └────────┬────────┘
                                                │ eawf phase archive
                                                v
                                       ┌─────────────────┐
                                       │    ARCHIVED     │
                                       └─────────────────┘
```

#### 5.4.4 Iter

```
   eawf roadmap revise --add-iter
              │
              v
   ┌─────────────────┐
   │    PLANNED      │ ── activate (parent phase ACTIVE) ──┐
   └─────────────────┘                                       │
                                                             v
                                                     ┌─────────────┐
                                                     │   ACTIVE    │
                                                     └─────┬───────┘
                                          ┌────────────────┤
                                          │ close          │ abandon
                                          v                v
                                ┌─────────────┐   ┌─────────────┐
                                │   CLOSED    │   │  ABANDONED  │
                                └─────────────┘   └─────────────┘
```

Closure gate: every owned wave CLOSED or ABANDONED.

#### 5.4.5 Wave

```
   eawf roadmap revise --add-wave (under PLANNED phase)  OR
   eawf roadmap propose --add-wave (under ACTIVE phase, AGENTS rule 20 [11])
              │
              v
   ┌─────────────────┐
   │    PENDING      │
   └────────┬────────┘
            │ eawf wave claim (deps CLOSED, sibling-order honored [11])
            v
   ┌─────────────────┐
   │    CLAIMED      │
   └────────┬────────┘
            │ /flow dispatches subagent (worktree created)
            v
   ┌─────────────────┐
   │  IN_PROGRESS    │
   └────────┬────────┘
            │
       ┌────┴────────────┬─────────────────┐
       │ close           │ fail            │ abandon
       v                 v                 v
   ┌────────┐     ┌────────┐         ┌────────────┐
   │ CLOSED │     │ FAILED │         │ ABANDONED  │
   └────────┘     └───┬────┘         └────────────┘
                     │ retry (V8 [1:228-232] — new attempt, session reuse)
                     v
              ┌─────────────────┐
              │  IN_PROGRESS    │  (sessions[attempt+1] populated)
              └─────────────────┘
```

V5 fallback (runtime switchover) does *not* change wave status — it appends a `DispatchAnnotation` + a `runtime_switched` event, opens a fresh `SessionAttempt` on the new runtime, and IN_PROGRESS continues.

#### 5.4.6 Hypothesis

```
   eawf hypothesis add
            │
            v
   ┌─────────────────┐
   │    PENDING      │
   └────────┬────────┘
            │ eawf audit run (kind=evaluation, cites this hypothesis)
            │
       ┌────┴───────────┬────────────┬──────────────┐
       │ verdict=conf   │ verdict=rej│ verdict=inc  │ defer
       v                v            v              v
  ┌────────────┐  ┌────────────┐ ┌──────────────┐ ┌──────────┐
  │ CONFIRMED  │  │  REJECTED  │ │INCONCLUSIVE  │ │ DEFERRED │
  └────────────┘  └────────────┘ └──────────────┘ └────┬─────┘
                                                       │ reactivate
                                                       v
                                                   (PENDING)
```

All terminal-except-DEFERRED transitions require `audit_id` set (§5.3.6 invariant).

#### 5.4.7 Decision

```
   eawf decision add
            │
            v
   ┌─────────────────┐
   │     ACTIVE      │
   └────────┬────────┘
       ┌────┴───────────┐
       │ supersede      │ reverse
       v                v
  ┌────────────┐  ┌────────────┐
  │ SUPERSEDED │  │  REVERSED  │
  └────────────┘  └────────────┘
```

Reverse is rare; supersede is the common forward path (sets `superseded_by` on the old; new decision is ACTIVE).

#### 5.4.8 Audit

```
   eawf audit run
            │
            v
   ┌─────────────────┐
   │    PENDING      │
   └────────┬────────┘
            │ runner starts
            v
   ┌─────────────────┐
   │    RUNNING      │
   └────────┬────────┘
            │
       ┌────┴───────────┐
       │ all checks ran │ runner crash / timeout
       v                v
   ┌────────────┐  ┌────────────┐
   │  COMPLETE  │  │   FAILED   │  retry → PENDING
   └─────┬──────┘  └────────────┘
         │
         v
    verdict ∈ { PASS, MINOR, MAJOR } (set on COMPLETE only)
```

Per §5.3.8 verdict is only emitted when status is COMPLETE.

#### 5.4.9 Artifact

```
   eawf artifact track <path>
            │
            v
   ┌─────────────────┐
   │   tracked       │  (no explicit lifecycle enum — Artifact has no status field)
   └────────┬────────┘
            │ promote (.ea/local/ → .ea/artifacts/) — once-only
            v
   ┌─────────────────┐
   │   promoted      │  uri updated; urn unchanged
   └─────────────────┘
```

No status enum today. C03 may add one if the spec graduation flow requires (DRAFT vs PROMOTED at artifact tier).

#### 5.4.10 Memory

```
   eawf memory write
            │
            v
   ┌─────────────────┐
   │     ACTIVE      │
   └────────┬────────┘
       ┌────┴────────────────────┐
       │ age + confidence<HIGH    │ promote (→ Decision artifact)
       │ → staleness sweep        │
       v                          v
  ┌────────────┐             ┌────────────┐
  │   STALE    │             │  ACTIVE    │  promoted_to_artifact_id set
  └─────┬──────┘             └────────────┘
        │ memory prune
        v
  ┌────────────┐
  │   PRUNED   │  soft delete; original payload preserved in JSONL
  └────────────┘

   any state ──── eawf memory supersede ────→ SUPERSEDED
                                              (new record with superseded_by backlink)
```

#### 5.4.11 Report (AgentReport)

```
   agent emits AgentReportBody
            │
            v
   ┌─────────────────┐
   │   attempt=N     │  append; if (role, base_id) already has rows, N = max(prior)+1
   └─────────────────┘

   retry / fix:
        same (role, base_id) → new row at attempt=N+1
```

No status enum — reports are events on the JSONL. The "current verdict" for `(role, base_id)` is the body of the row with `max(attempt)`.

#### 5.4.12 AgentSession

```
   /agent-dispatch → daemon starts subprocess
            │
            v
   ┌─────────────────┐
   │     ACTIVE      │
   └────────┬────────┘
       ┌────┴───────────────────┬──────────────┬──────────────┐
       │ checkpoint              │ close        │ stale        │ fail
       │ (interactive operator   │ (subprocess  │ (no I/O for  │ (non-zero
       │  pauses session)        │  exits 0)    │  N minutes)  │  exit)
       v                         v              v              v
  ┌──────────────┐         ┌──────────┐  ┌──────────┐    ┌──────────┐
  │ CHECKPOINTED │ ──┐     │  CLOSED  │  │  STALE   │    │  FAILED  │
  └──────────────┘   │     └──────────┘  └─────┬────┘    └────┬─────┘
                     │ resume (V8 continue)    │              │ retry → new session
                     v                          │              v
                  ACTIVE                   eawf agent reap  new session
```

V8 [1:228-232] makes the CHECKPOINTED→ACTIVE transition a `--continue` invocation; the runtime adapter validates the session handle is still live and falls back to fresh dispatch with a `DispatchAnnotation` if not.

#### 5.4.13 Event

```
   any mutator path
            │
            v
   ┌─────────────────┐
   │    appended     │  one row per state.json write; immutable
   └─────────────────┘
```

No transitions — events are immutable. Compaction (V7 [1:191-192], R3 [29:443-446]) is a snapshot operation, not a status change.

#### 5.4.14 Profile

```
   eawf profile add <id>
            │
            v
   ┌─────────────────┐
   │    LOADED       │  in the project's profiles: list, no conflicts
   └────────┬────────┘
       ┌────┴───────────┐
       │ conflict       │ remove
       │ undeclared     │
       v                v
  ┌──────────────┐  ┌──────────┐
  │ CONFLICTED   │  │ UNLOADED │
  └──────┬───────┘  └──────────┘
         │ later profile overrides this one
         v
  ┌──────────────┐
  │  SHADOWED    │  contributes nothing; loader prefers the overrider
  └──────────────┘
```

Today's `ProfileBody` has no status enum; C08 finalises whether the status lives on the project's `profiles: list` ordering or on a separate `ProfileLoad` record.

#### 5.4.15 Spec

```
   eawf <phase|iter|wave> spec init
            │
            v
   ┌─────────────────┐
   │     DRAFT       │  in .ea/specs/, frontmatter validates, body may be incomplete
   └────────┬────────┘
            │ eawf <tier> spec validate succeeds (implements: non-empty, tests: real paths)
            v
   ┌─────────────────┐
   │     READY       │  feeds into /prep activate or /flow dispatch
   └────────┬────────┘
            │ wave/iter/phase closes
            v
   ┌─────────────────┐
   │  IMPLEMENTED    │  spec body matches delivered code (verify-implements audit-DSL kind, C03)
   └────────┬────────┘
            │ parent phase closes
            v
   ┌─────────────────┐
   │   ARCHIVED      │  daemon `git rm`s the file; cached index in <local-path>
   └─────────────────┘  restorable via `eawf spec show <id> --from-git` (daemon walks git log)
```

Per D3 the ARCHIVED state removes the file from HEAD; the daemon-side cache holds the last SHA + status so `eawf spec show` doesn't need a manual `git log` to recover the body.

#### 5.4.16 Runtime

```
   eawf runtime configure <id>
            │
            v
   ┌─────────────────┐
   │   CONFIGURED    │  config-only, no probe yet
   └────────┬────────┘
            │ first dispatch succeeds OR explicit `eawf doctor`
            v
   ┌─────────────────┐
   │    HEALTHY      │
   └────────┬────────┘
       ┌────┴────────────────┐
       │ V5 fallback triggers │ no-binary / config bad
       │ on error             │
       v                      v
  ┌──────────────┐      ┌──────────────┐
  │  DEGRADED    │      │ UNAVAILABLE  │
  └──────┬───────┘      └──────────────┘
         │ recovery — next call succeeds
         v
      HEALTHY
```

#### 5.4.17 Plugin

```
   eawf plugin install <id>
            │
            v
   ┌─────────────────┐
   │   INSTALLED     │
   └────────┬────────┘
       ┌────┴───────────────┬────────────┐
       │ on-disk drift       │ conflict  │ disable
       │ vs managed_files    │ flag      │
       v                     v            v
  ┌──────────────┐      ┌────────────┐ ┌──────────┐
  │   DRIFTED    │      │ CONFLICTED │ │ DISABLED │
  └──────┬───────┘      └────────────┘ └──────────┘
         │ eawf plugin reinstall
         v
      INSTALLED
```

#### 5.4.18 McpServer + McpGrant

```
   eawf mcp add <id>
            │
            v
   ┌──────────────────┐
   │ NOT_CONFIGURED   │
   └────────┬─────────┘
            │ config written
            v
   ┌──────────────────┐
   │   CONFIGURED     │
   └────────┬─────────┘
            │ eawf mcp install
            v
   ┌──────────────────┐
   │   INSTALLED      │
   └────────┬─────────┘
       ┌────┴───────────┐
       │ health bad      │ operator disable
       v                 v
  ┌────────────┐    ┌────────────┐
  │ DEGRADED   │    │ DISABLED   │
  └────────────┘    └────────────┘

   McpGrant: granted_at-set; lifecycle is the McpServer's lifecycle + the bound scope's lifecycle.
```

#### 5.4.19 Other entities

- **Goal**: OPEN → ACHIEVED / ABANDONED [4:25-28].
- **Outcome**: PENDING → MET / MISSED / WAIVED [4:31-35].
- **BacklogItem**: OPEN → IN_PROGRESS → CLOSED / DEFERRED [4:123-127].
- **EstimateSummary / ActualSummary**: append-only with `current_store_record_id` pointing at the latest store row.
- **WorktreeRecord**: ACTIVE → CONFLICTED / MERGED / ABANDONED [4:191-195].
- **Incident**: OPEN → MITIGATED → RESOLVED / WONT-FIX [4:137-141].
- **Flow**: PENDING → IN_PROGRESS → PAUSED / BLOCKED / DONE / ABANDONED / SUPERSEDED [4:144-151].
- **Principal** (reserved): ACTIVE → REVOKED.

### 5.5 Persona authority matrix

Rows = personas; cols = actions. Cell value:
- ✅ allowed (no operator approval)
- 🟡 allowed with operator approval (recorded in audit)
- 🚫 forbidden

| Action ↓ / Persona → | operator | agent-executor | agent-reviewer | agent-auditor | agent-researcher | agent-planner | agent-polisher | agent-domain-specialist | daemon | watcher | profile-author |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Read `state.json` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Mutate `state.json` | 🟡 (via CLI) | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | ✅ (sole writer per V1) | 🚫 | 🚫 |
| Append to `event.jsonl` | 🚫 directly | 🚫 directly | 🚫 directly | 🚫 directly | 🚫 directly | 🚫 directly | 🚫 directly | 🚫 directly | ✅ (one per state mutation) | 🚫 | 🚫 |
| Append to `audit.jsonl` | 🚫 directly | 🚫 directly | 🚫 directly | ✅ (via `eawf audit run`) | 🚫 directly | 🚫 directly | 🚫 directly | 🚫 directly | ✅ (proxy for audit-DSL runner) | 🚫 | 🚫 |
| Append to `<role>_report.jsonl` | ✅ (operator) | ✅ (executor) | ✅ (reviewer) | ✅ (auditor) | ✅ (researcher) | ✅ (planner) | ✅ (polisher) | ✅ (domain) | proxy | 🚫 | 🚫 |
| Commit on worktree branch | ✅ | ✅ | 🚫 | 🚫 | 🚫 | 🚫 | ✅ (formatting/naming) | 🚫 | 🚫 | 🚫 | 🚫 |
| Cherry-pick onto feature branch | ✅ | 🚫 (operator-only) | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 |
| `eawf wave claim` | ✅ | ✅ | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | proxy | 🚫 | 🚫 |
| `eawf phase activate` (`/prep`) | ✅ | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | proxy | 🚫 | 🚫 |
| `eawf phase close` (`/ship`) | ✅ | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | proxy | 🚫 | 🚫 |
| `eawf phase reopen` | ✅ | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | proxy | 🚫 | 🚫 |
| Add/edit hypothesis | ✅ | 🚫 | 🚫 | 🚫 | ✅ | 🚫 | 🚫 | 🚫 | proxy | 🚫 | 🚫 |
| Set hypothesis verdict | ✅ | 🚫 | 🚫 | ✅ (via audit) | 🚫 | 🚫 | 🚫 | 🚫 | proxy | 🚫 | 🚫 |
| Add decision | ✅ | 🚫 | 🚫 | 🚫 | 🚫 | ✅ (planner adds decision rows) | 🚫 | 🚫 | proxy | 🚫 | 🚫 |
| Supersede decision | ✅ | 🚫 | 🚫 | 🚫 | 🚫 | ✅ | 🚫 | 🚫 | proxy | 🚫 | 🚫 |
| Edit Wave success criteria (PENDING) | ✅ | 🚫 | 🚫 | 🚫 | 🚫 | ✅ | 🚫 | 🚫 | proxy | 🚫 | 🚫 |
| Edit Wave success criteria (ACTIVE) | 🟡 (revise gate) | 🚫 | 🚫 | 🚫 | 🚫 | 🟡 | 🚫 | 🚫 | proxy | 🚫 | 🚫 |
| Merge PR | ✅ | 🚫 (manifesto Rule 2) | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 |
| Approve TUI plan-mode preview | ✅ | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 |
| Subscribe to event bus (read) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Write profile YAML | ✅ | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | ✅ |
| `eawf daemon enable` | ✅ | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 |
| `eawf wave switch <id> --to <runtime>` | ✅ (per V5 [1:148-149]) | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | 🚫 | ✅ (on V5 fallback) | 🚫 | 🚫 |

**Cell legend recap.** "proxy" means the persona acts through the daemon — the daemon is the sole append-er to `event.jsonl` and the sole writer of `state.json`, so other personas' mutations route through it. "🟡 (revise gate)" means the action is allowed only after passing the AGENTS rule 20 revisability gate (PENDING-only for ACTIVE-phase waves [11]).

**Manifesto Rule 2 binding.** "Merge PR" is operator-only across every agent persona. This is the load-bearing trust boundary — every other cell is delegable via the daemon under the right verdict, but merge authority is exclusively human [2:38-42].

### 5.6 Trust + audit-replay model

**Goal.** A future operator MUST be able to retract or confirm any in-state assertion from `state.json` + `event.jsonl` + `audit.jsonl` + per-role `<role>_report.jsonl` files alone, without needing the original CC / Codex / OpenCode conversation log.

#### 5.6.1 Evidence chain shape

Every assertion of "verdict X about thing Y" carries a four-link chain:

```
state.json:audits[audit_id].verdict        ← claim site
    ↑
audit.jsonl:Envelope[audit_id].payload     ← typed CheckResult list
    ↑
agent_report.jsonl (role-specific):
    Envelope[report_id where base_id=audit_id]
                                            ← agent's body + evidence_refs (per role)
    ↑
event.jsonl:Envelope[event_id with after_state_version
                     matching state.audits[audit_id].created_at]
                                            ← mutation envelope
```

Reading direction: claim site (state.json) → typed audit payload (audit.jsonl) → typed agent report body (per-role report jsonl) → mutation envelope (event.jsonl). Each link cites the next via URN.

#### 5.6.2 What counts as evidence

For hypotheses:

- **CONFIRMED** requires `audit_id` set AND the cited audit's verdict is PASS AND the audit's `report_artifact_id` points at an artifact whose URN resolves on-disk (the published research brief).
- **REJECTED** requires the same chain with verdict ∈ {MINOR, MAJOR}.
- **INCONCLUSIVE** requires `audit_id` set; verdict ∈ {MINOR, MAJOR} OR all CheckResult.passed flags false but no MAJOR violation.

For decisions:

- **ACTIVE** requires an entry in `decision.jsonl` (today's store kind `decision` [4:275]) with non-empty `rationale`.
- **SUPERSEDED** requires `superseded_by` set to a `Decision.id` whose own status is ACTIVE.

For waves:

- **CLOSED** requires `commit` field set to a cherry-picked SHA on the parent feature branch AND an `executor_report` row at `attempt = max(prior)` with `verdict ∈ {PASS, PASS_WITH_FOLLOWUPS}`.

#### 5.6.3 Replay

Per R3 [29:264-309] full event-source rebuild is a v0.5+ goal (depends on typed Mutation payload + HLC timestamps). C01 documents the v0.3-v0.5 partial replay surface:

- **State digest.** `state.json` carries an implicit content hash via the `Envelope.after_state_version` field on the most recent EVENT-kind row [26:18-19]. Rebuilding the digest by walking the JSONL gives a 16-hex anchor; mismatch flags drift.
- **Audit replay.** `eawf audit run --replay <audit_id>` re-runs the audit-DSL kind against the *current* state; comparison with the original `check_results` shows whether the world has shifted under the verdict.
- **Reconcile.** R3 [29:347-353] specifies a `reconcile()` sweep that compares state-resident rows to projected rows from `event.jsonl`. C01 catalogues `reconcile` as a future daemon verb; the v0.3 implementation is bounded.

#### 5.6.4 Forgery resistance

Today's `EventPayload.actor = "cli"` literal [26:17] is not forgery-resistant — anyone with shell access can `echo` an event. Per D4 the v0.5+ Principal entity is the migration target. **Until then, v0.3-v0.5 trust is `O(operator-vetted-machine)`** — same as today. C01 documents this honestly so the v0.5+ governance phase doesn't ship false-confidence claims.

### 5.7 SDLC mapping

The full lifecycle a phase walks from idea to ship + close, with the state-mutator and persona named at each step:

```
1. research
    persona: agent-researcher, operator
    output: brief under .ea/local/research/<YYYY-MM-DD>-<slug>.md
    mutator: none (local-only file, no state mutation)
    artifact: store record StoreKind.RESEARCH after operator promotes

2. spike (optional)
    persona: agent-researcher
    output: brief or experimental verdict; same path as research
    mutator: none (local-only)
    purpose: write the wave success criteria afterwards (AGENTS §spike-workflow [11])

3. roadmap propose --phase P<NN> [--from-briefs ...]
    persona: agent-planner
    output: PhaseSpec scaffold + iter+wave DAG sketch
    mutator: daemon (proposes the phase row in PLANNED state)
    AUQ to operator: approve / edit / reject

4. roadmap revise P<NN> --add-wave / --set-deps / --retitle
    persona: agent-planner, operator
    output: refined PhaseSpec + WaveSpec scaffolds
    mutator: daemon (modifies PLANNED phase scope per AGENTS rule 20 [11])

5. roadmap apply P<NN>
    persona: operator
    output: PhaseSpec READY
    mutator: daemon (PLANNED scope confirmed; specs in .ea/specs/<phase>/ READY)

6. /prep P<NN>
    persona: operator
    output: phase ACTIVE; V11 hard gate run
    mutator: daemon
    side effects: spec status PhaseSpec READY → READY; waves under iters → PENDING

7. /flow (per claimed wave)
    persona: agent-executor (dispatch'd by daemon under V8 hybrid policy [1:226-271])
    output: WaveSpec → READY → IMPLEMENTED; commit on worktree branch
    mutator: daemon (Wave PENDING → CLAIMED → IN_PROGRESS → CLOSED)
    side effects: executor_report appended; worktree record created

8. cherry-pick (per closed wave)
    persona: operator
    output: WaveSpec IMPLEMENTED; wave commit on feature branch
    mutator: daemon (Wave.commit set to cherry-picked SHA)

9. /audit (per audit-DSL kind)
    persona: agent-auditor
    output: audit record + verdict + auditor_report
    mutator: daemon (Audit row PENDING → RUNNING → COMPLETE)

10. /ship (phase close)
    persona: operator
    output: phase CLOSED; PR opened/merged; specs ARCHIVED (git rm)
    mutator: daemon (Phase ACTIVE → CLOSED; Iter[*] → CLOSED; Spec[*] → ARCHIVED)
    side effects: ship-gate audit PASS required; commit-prefix lint enforces [P##] grammar [11]

11. close
    persona: operator
    output: PR merged via rebase (per feedback_pr_merge_strategy memory)
    mutator: daemon (final state snapshot; event.jsonl bookmark)
```

**Phase-bundling rule (manifesto Rule 8 [2:75-78]).** One phase produces one PR. Waves cherry-pick into the long-running phase branch; the branch never merges sibling worktree branches — only cherry-picks.

## 6. Failure modes + named edge cases

C01 introduces a vocabulary and a catalog, not new runtime behaviour. The failure modes below are the ones that *break C01-level claims* (URN uniqueness, persona authority, lifecycle integrity, evidence chain) if a downstream cluster gets the implementation wrong.

| # | Failure mode | Trigger | Detection | Repair |
|---|---|---|---|---|
| F1 | URN kind collision | Two entity kinds claim the same single-word token (e.g. C03 ships `spec` and C07 also ships `spec` for a different concept). | `URN_KINDS` is a `frozenset` [17:16-29]; module-load ValueError on duplicate. CI: lint that no two registries register the same kind. | Operator AUQ to pick the canonical owner; loser renames or composes (slash-id under the canonical owner). |
| F2 | URN ID overflow | Phase / iter / wave allocator exhausts the 2-digit suffix (99 entries). | `allocate_next_*` raises `ValueError("all 99 suffixes are in use")` [15:97]. | v0.3 → v0.5 acceptable; v0.5+ pivot to 3-digit suffix (schema_version bump). |
| F3 | Slash-friendly id with embedded path separator on non-_SLASH_KINDS | A caller passes `id="P20/I03"` to `build("phase", ...)`. | `build()` raises `ValueError("id may not contain '/'")` [17:97-98]. | Caller-side: use the right URN kind for the hierarchical concept. |
| F4 | Persona escalation | Agent-executor calls `eawf phase close` directly (forbidden cell). | Daemon checks `actor` (today `"cli"`) against the action's allowed persona table; refuses with structured envelope `status=blocked`. | v0.5+ Principal + caps enforces this cryptographically; v0.3-v0.5 enforces via daemon authority check on the running shell session. |
| F5 | Lifecycle skip | Operator closes a wave that's still PENDING (never CLAIMED / IN_PROGRESS). | `lifecycle.transitions.close_wave` rejects with `LifecycleError` [16]. | Operator claims-then-closes, or marks ABANDONED if work won't be done. |
| F6 | Spec orphan | `.ea/specs/P20/I03/W01.md` exists on disk but no `state.waves["P20-I03-W01"]` row exists. | C03 `verify-implements` audit-DSL kind enumerates spec files and asserts every one maps to a wave/iter/phase row. | Delete the orphan spec (AGENTS deletion rule [11]) or add the missing entity row. |
| F7 | Spec missing | `state.waves["P20-I03-W01"]` exists in ACTIVE phase but no `.ea/specs/P20/I03/W01.md`. | C03 `verify-implements` audit fails. `/flow` claim refuses to dispatch a wave without a READY spec. | Author the spec via `eawf wave spec init`; activate per §5.4.15. |
| F8 | Spec ARCHIVED but cited by an open wave | A phase reopens, its waves move PENDING again, but the spec was already `git rm`'d on the prior close. | `eawf spec show` reads from the daemon cache + `git log` and rehydrates. | Daemon-side cache miss → walk `git log -- .ea/specs/<phase>/` and resurrect. |
| F9 | Profile conflict undeclared | Project lists `profiles: [research, reverse-engineering]` but the two profiles touch the same render-block id without declaring `conflicts_with` or `overrides`. | C08 loader fails fast at compose time. | Add the explicit declaration or drop one of the profiles. |
| F10 | Decision supersedes a REVERSED decision | Caller passes a REVERSED `Decision.id` as the new `superseded_by` target. | Invariant: `superseded_by` must reference an ACTIVE or SUPERSEDED decision, never REVERSED (REVERSED is terminal). | Edit the caller to pick the correct target. |
| F11 | Memory tier mismatch | `MemorySummary.tier = WORKING` but the corresponding `memory.jsonl` body claims `tier=archival` in its text. | C03 `verify-memory-tier` audit (proposed) cross-checks. | Re-write the memory entry with the right tier; soft-delete the mismatched original. |
| F12 | Report attempt skipping | A `(role, base_id)` series has attempts {1, 3, 4} — attempt 2 is missing. | `_next_attempt` rule `max(prior)+1` [23:78-89] forbids this on write. If found on disk, it indicates manual JSONL edit. | Investigate: someone bypassed the writer. Per AGENTS rule 8 [11] verify the actual state. |
| F13 | Worktree commit not cherry-picked | `Wave.commit` is the worktree-side SHA, not the parent-branch cherry-pick SHA. | C03 wave-close audit checks `git merge-base --is-ancestor commit feature/<branch>`. | Operator cherry-picks then resets `Wave.commit` to the post-cherry-pick SHA. |
| F14 | Cross-runtime session reuse | V8 fallback (V5) opens a new session on a different runtime, but the audit log records the old session_id. | Per V8 invariant [1:264-268] new runtime = fresh session; `DispatchAnnotation` documents the switch. | Daemon must enforce the rule; report failure is a daemon bug. |
| F15 | URN-form case mismatch | One caller writes `urn:eawf:v1:wave:eawf/P20-I03-W01`; another writes `urn:eawf:v1:wave:eawf/p20-i03-w01`. | Equality test fails when both forms coexist. | §5.2.3 rule: parser preserves case; equality normalizes via `.lower()`. Encode the rule in the parser. |
| F16 | Audit replay drift | `eawf audit run --replay <id>` re-runs against current state and produces a different verdict. | Operator sees structured diff. | This is *expected* behaviour — the world has changed. Document the drift in a follow-up incident; don't auto-rewrite the audit. |
| F17 | Subproject scope leak | Wave row has `scope_id = urn:eawf:v1:state:eawf` but `current.subproject_id` is set, so reports aggregate under the subproject even though the wave was meant to be repo-scope. | Cross-entity invariant `check_scope_consistency` [29:756] surfaces. | Reset `current.subproject_id = None` before claiming the wave OR retroactively set the wave's `scope_id` to the subproject's URN. |
| F18 | Principal-reserved kind used before v0.5 | Some external integration writes `urn:eawf:v1:principal:user/u-123` into a federation payload v0.3-v0.5 doesn't yet validate. | C07 federation validator (v0.5+) flags the unmatched URN. | The kind is *reserved*; downstream code may emit but the daemon doesn't enforce until v0.5+. Treat as advisory. |

## 7. Migration plan

C01 is mostly a *naming* migration — entity catalog + URN scheme + persona matrix. Code-level renames are bounded; the biggest one is the spec-entity introduction (D3).

### 7.1 What changes (operational steps, one per affected surface)

1. **URN kind enum.** Extend `URN_KINDS` in `src/eawf/state/urn.py:16-29` [17] from today's 10 to the catalogued 26. Loader-side this is a Python set edit; downstream callers that previously emitted strings like `urn:eawf:v1:wave:...` (which today raise `ValueError("unknown URN kind: 'wave'")`) start succeeding.
2. **URN slash-friendly extension.** Extend `_SLASH_KINDS` [17:35] from `{repo, artifact, store}` to `{repo, artifact, store, spec, report, event, memory, session, plugin, mcp}` so slash-bearing ids parse correctly.
3. **AgentReport ↔ Report alias.** Today's `agent_report` token isn't a URN kind, but the C00 prose used the underscored form. C01 standardises on `report` for the URN kind; `agent_report` stays as the Python class name (`AgentReportBody` etc.) so the rename is *URN-only*. The store kind enum `StoreKind.RESEARCHER_REPORT` etc. is unchanged.
4. **Spec entity introduction.** No `State.specs` dict (per D3). Add a `eawf spec` CLI noun (C05) with verbs `init / validate / show / promote / archive`. Storage path is V2's `.ea/specs/<phase>/[<iter>/]<wave|spec>.md`. Lifecycle DAG at §5.4.15. The daemon ARCHIVED transition `git rm`s the file and writes `<local-path>` so `eawf spec show <phase>` from an archived phase works.
5. **Wave session-handle map.** Today's `Wave` lacks `sessions / runtime_preference / dispatch_history` fields. C02 daemon implements; C01 reserves the field names. No code rename until C02 ships.
6. **Runtime entity reification.** Today `runtime` is a free-string field on `AgentSession.runtime` [10:354]. C07 introduces `Runtime` as an entity (§5.3.16); URN kind reserved here. No state-schema change in v0.3.
7. **Principal entity reservation.** No code change. The URN kind `principal` is reserved in `URN_KINDS` so v0.5+ doesn't need to bump the URN version when it introduces the entity.

### 7.2 What doesn't change

- `State.project: Project | None` field name unchanged. Per D2 the *concept* is Repo=Project; the *field name* stays `project` for one full v0.3-v0.5 cycle so the migration cost stays under one phase. v0.5+ may rename to `repo`.
- Every existing Pydantic model in `src/eawf/state/models.py` [10] keeps its current fields and field names.
- Every existing enum in `src/eawf/state/enums.py` [4] keeps its current values.
- `state.json` shape on disk is unchanged. `schema_version: "1.0"` stays.
- The `AGENTS.md` non-negotiable rules [11] are unchanged.

### 7.3 Per-phase rollout

| Phase | Surface | Scope |
|---|---|---|
| **C02 (next cluster)** | Daemon + IPC | Implement Wave.sessions / runtime_preference / dispatch_history reads + writes via daemon RPC. URN_KINDS extension. |
| **C03** | Spec subsystem | PhaseSpec / IterSpec / WaveSpec Pydantic schemas; `eawf <tier> spec ...` CLI verbs; lifecycle DAG ARCHIVED transition with daemon-side cache. |
| **C05** | CLI surface | Add `eawf spec` noun + verbs; add `eawf runtime` noun; surface `eawf daemon` per V6. |
| **C07** | Runtime adapter | Reify Runtime entity (`src/eawf/runtimes/...`). Per-runtime session-handle adapter per V8. |
| **C08** | Profile composition | `conflicts_with` + `overrides` fields on `ProfileBody`. |
| **C09** | Telemetry projection | DuckDB schema keyed off URNs from §5.2.2. |

### 7.4 Rollback

C01 is naming-only — no state mutator changes — so rollback is `git revert` of the C01-tagged commit set. Downstream clusters that already adopted C01 URNs would need to fall back to their pre-C01 form; in practice the URN renames are pure additions, so a revert is safe.

## 8. Open questions for operator

The first four questions were locked in §4 D1-D4 via AskUserQuestion on 2026-05-16. The remaining items below are the next AUQ seed set; the brief promotes from `needs-user` to `accepted` when these are answered.

### Q1 (resolved §4 D1) — URN kind scope. **Locked: broad, single-word tokens.**

### Q2 (resolved §4 D2) — Project ≡ Repo, Subproject retained. **Locked.**

### Q3 (resolved §4 D3) — Spec is filesystem-only, URN-derivable, ARCHIVED on phase close. **Locked.**

### Q4 (resolved §4 D4) — Principal deferred to v0.5+. **Locked.** §5.3.19 explains the entity in detail.

### Q5 — URN versioning policy

C01 fixes `URN_VERSION = "v1"` [17:30]. When does v2 land?

- (a) **Never until breaking change.** v1 stays through v0.3, v0.4, v0.5 (operator-confirmed C00 V4 cluster-sequential batching keeps surface area small). v2 only at the first breaking grammar change.
- (b) **Bump on every new C0N cluster.** Each cluster gets its own URN version (overkill — every consumer needs to read N versions).
- (c) **Bump on schema_version bump.** When `State.schema_version` goes 1.0 → 2.0, URN_VERSION goes v1 → v2 in lockstep.

**Recommendation (a).** v1 covers C01..C11; v2 only when a kind needs an incompatible grammar change.

### Q6 — Subproject feature scope (§4 D2 follow-up)

Operator note: "maintain Subprojects inside too (scope-defined, but need to think what features they change)". Three features Subprojects currently shape:

- (a) **Dispatch envelope title** — wave dispatch title carries `[<subproject>][P##-W##]` when `current.subproject_id` is set.
- (b) **TUI breadcrumb** — operator surface shows the subproject in the scope ladder.
- (c) **Report aggregation** — agent reports under a subproject roll up under the subproject scope.

C01 catalogues (a), (b), (c) as the v0.3-v0.5 surface. Future features that *might* attach to Subproject:

- **Subproject-scoped weekly EU target.** Today `Project.weekly_eu_target` [10:115] is repo-level; could become per-subproject.
- **Subproject-scoped audit-replay window.** Today the audit-replay sweep walks `event.jsonl` for the repo; could narrow to subproject.

**Recommendation:** lock (a), (b), (c) now; mark the weekly-EU + audit-window features as Open Questions for C09 / C10.

### Q7 — Spec ARCHIVED archival path

D3 says daemon `git rm`s the spec file on phase close and caches the SHA at `<local-path>`. Two sub-questions:

- (a) **Cache durability.** Is the cache rebuildable from `git log` alone? **Yes** — the cache only stores the *last SHA before delete* per spec id; `git log --diff-filter=D` finds the deletion commit, then `git show <commit>:<path>` resurrects the body.
- (b) **Restore semantics on `phase reopen`.** When a CLOSED phase reopens (AGENTS rule 20 [11]), should the daemon git-mv the spec back into HEAD, or keep it archived and show from cache?

**Recommendation (b).** Keep ARCHIVED — reopen is for state-machine purposes (audit re-run, decision re-evaluation), not for resuming the implementation. The spec body is what was IMPLEMENTED; further work needs a *new* spec at a *new* wave.

### Q8 — Persona granularity

§5.5 lists 11 personas. Two pairs are close to merging:

- (a) **agent-reviewer + agent-auditor** — both read-only, both emit reports, differ only in scope (reviewer: diff; auditor: audit-DSL kind). Merge? **No** — `AgentSessionRole` already separates them [4:165-173]; keep.
- (b) **agent-polisher + agent-domain-specialist** — polisher does formatting/naming sweeps; domain-specialist takes scoped tasks needing project-specific context. Merge? **No** — they have different authority cells in the matrix.

**Recommendation:** keep all 11.

### Q9 — Memory tier promotion rule

§5.3.10 D6 locks `(scope_id, tier)` as the memory namespace key. Open: when does WORKING auto-promote to ARCHIVAL?

- (a) **Age-based** (today's `staleness` module [28] uses age + confidence).
- (b) **Capacity-based** (working tier has a soft cap; oldest spills to archival).
- (c) **Operator-driven** (`eawf memory promote-tier <id>` explicit).

**Recommendation (a) + (c).** Age sweep covers the common case; operator override for edge cases. C04 owns the algorithm.

### Q10 — Event-vs-Audit log canonical role (§4 D5 follow-up)

D5 locked "two logs, two purposes". Open: should `audit.jsonl` *embed* a pointer to the triggering `event.jsonl` row, or stay separate?

- (a) **Embed.** `AuditPayload` adds `triggered_by_event_id: str | None`. Makes replay walk simpler.
- (b) **Stay separate.** Reconcile-sweep links the two via timestamp + scope match.

**Recommendation (a).** The embed cost is one field; the gain on §5.6 evidence-chain walk is large.

### Q11 — Decision id pattern

§5.3.7 sketches `d-<YYYY-MM-DD>-<slug>` for decision IDs. Today's `Decision.id` is `IdStr` (free-form non-whitespace [10:81]). Lock the pattern in v0.4?

**Recommendation:** lock in C03 with a regex on `Decision.id` matching `^d-\d{4}-\d{2}-\d{2}-[a-z0-9-]{1,32}$`.

### Q12 — Spec `implements:` regex grammar

§5.3.15 sketches `VerdictCitation { verdict_id, brief, line }`. Lock the verdict-id grammar:

- (a) `^V\d+$` — matches V1..V8 today.
- (b) `^V\d+[a-z]?$` — allows V1a / V1b sub-verdicts.
- (c) `^[A-Z]\d+$` — allows H## / D## / etc. (over-broad).

**Recommendation (b).** Sub-verdicts may emerge during a cluster brief (e.g. V3a profile loader vs V3b conflict declaration).

### Q13 — Daemon-cache location

§5.4.15 + Q7 reference `<local-path>`. Confirm:

- (a) `<local-path>` — user-scope; survives repo deletes.
- (b) `.ea/local/cache/` — repo-scope; gitignored.

**Recommendation (a).** Repo-scope risks cache loss on `rm -rf <repo>`; user-scope persists and the daemon can rebuild from `git log` of any repo it knows about.

### Q14 — URN kinds for supplementary entities

The 10 supplementary entities (Goal, Outcome, BacklogItem, EstimateSummary, ActualSummary, WorktreeRecord, SandboxPolicy, Incident, Flow, Subproject) have no first-class URN kind in v0.3-v0.5. v0.5+ federation may want every state-resident row cross-process-addressable. Bump to first-class then, or keep composite-key forever?

**Recommendation:** defer the decision to v0.5+ federation cluster (post-C11). v0.3-v0.5 composite-key form is sufficient for daemon + TUI + CLI surfaces.

## 9. References

[1] `.ea/local/research/long-term/2026-05-16-c00-spec-index.md` — C00 spec architecture index (V1..V8 verdicts; cluster catalog; per-cluster scope contract). Read end-to-end.
[2] `.ea/local/research/long-term/2026-05-15-ea-framework-manifesto.md` — Eä framework manifesto (7 rules + Rule 8 extension; case study; adoption checklist).
[3] `.ea/local/research/long-term/2026-05-15-long-term-features-deep.md` — long-term features deep brief (Pydantic versioned migrations; HLC envelope; Merkle hash-tree; R3 inconsistency; R5 governance).
[4] `src/eawf/state/enums.py` — every `StrEnum` used across the state subsystem (ProjectStatus, PhaseStatus, IterStatus, WaveStatus, HypothesisStatus, AuditKind, AuditStatus, AuditVerdict, MemoryStatus, MemoryTier, AgentSessionRole, AgentReportVerdict, AgentSessionStatus, ScopeKind, StoreKind, ArtifactKind, etc.).
[9] `src/eawf/render/envelope.py` — typed three-part envelope (`OutputEnvelope` / `EnvelopeHeader` / `EnvelopeFooter`).
[10] `src/eawf/state/models.py` — Pydantic v2 state models (Project, Subproject, Phase, Iter, Wave, Hypothesis, Decision, Audit, Artifact, BacklogItem, EstimateSummary, ActualSummary, AgentSession, WorktreeRecord, McpServer, McpGrant, PluginInstall, MemorySummary, Incident, Flow, State).
[11] `AGENTS.md` — non-negotiable rules (CLI is dispatch; state CLI sole mutator; symbol conventions; deletion rule; commit prefix; planned-scope revisability; spike workflow; etc.).
[12] `.ea/local/research/long-term/2026-05-15-long-term-features-deep.md` §6 — R5 governance / Principal Pydantic / capabilities / federation handshake / RBAC.
[13] `src/eawf/profiles/models.py` — `ProfileBody`, `ComposedProfile`, `StateExtensions`, `InstrumentReq`, `RenderBlock`.
[14] `src/eawf/registry/__init__.py` — `<local-path>` index (explicit init/add-repo only per `feedback_explicit_registry_only`).
[15] `src/eawf/state/ids.py` — ID grammar regexes (RE_PROJECT_CODE / RE_PHASE / RE_ITER / RE_WAVE / RE_HYPOTHESIS / RE_HYPOTHESIS_SCOPED); `allocate_next_*` helpers; `parents_of`.
[16] `src/eawf/lifecycle/transitions.py` — pure-functional phase / iter / wave open/close/abandon helpers; `LifecycleError`.
[17] `src/eawf/state/urn.py` — current URN parser/builder (`URN_KINDS`, `URN_VERSION`, `_SLASH_KINDS`, `parse`, `build`, `Urn.identity`).
[22] `src/eawf/store/kinds/memory.py` — `MemoryPayload`.
[23] `src/eawf/store/kinds/agent_report.py` — typed agent report bodies (`AgentReportHeader`, `AgentReportCommonBody`, per-role body classes, `AgentReportPayload`, `report_record_id`, `report_store_urn`).
[25] `src/eawf/store/envelope.py` — top-level JSONL store record `Envelope`.
[26] `src/eawf/store/kinds/event.py` — `EventPayload`.
[27] `src/eawf/store/kinds/audit.py` — `AuditPayload`, `CheckResult`.
[28] `src/eawf/memory/{store,promotion,prune,staleness}.py` — memory subsystem (append, supersede, prune, staleness sweep).
[29] `src/eawf/validate/invariants.py` — cross-entity invariants (`check_parent_ids`, `check_current_pointers`, `check_closure_rules`, `check_audit_evidence`, `check_scope_consistency`, `check_plugin_owners`, `check_wave_blocks_invariant`, `check_artifact_urns`, `check_agent_report_invariants`, etc.).

## 10. Provenance + Scrub

### Provenance

- `store_record=none (local-only research brief)`
- `commit=3b86f7a (parent at brief authoring time; revisions 2026-05-18 against same parent)`
- `cluster=C01`
- `consumes=C00 verdicts V1..V9 (V9 added 2026-05-18 per XB10 / B-01; locked 2026-05-16 [1:22-271])`
- `supersedes=none`
- `last_revised=2026-05-18 (Stage-0 audit-driven; +V9 prior verdict cite; +16 glossary terms per BOT-02; +Principal min model per XB08/Q3; URN_KINDS count fix 25→26 per XB15)`
- `session=eawf-spec-c01-foundations-2026-05-16`
- `audit_consumed=2026-05-17-spec-series-combined-audit.md`
- `operator_decisions_locked=2026-05-16 AskUserQuestion answers — D1 URN broad single-word, D2 Repo=Project + Subproject retained, D3 spec filesystem-only URN-derivable archived-on-close, D4 Principal deferred-and-reserved (D4 superseded 2026-05-18 by Q3: minimum Principal model lands v0.3-v0.5; full enforcement still v0.5+)`
- `wave_commit_drop_note=Wave.commit field drift (BOT-07) — per Q11, drop from src/eawf/state/models.py + git-log walk backfill in v0.4 hygiene wave. AGENTS verify-before-claim block stays authoritative.`

### Scrub

- status: clean
- references: repo-relative or external URL only
- local paths: none
- real emails: none (canonical author block in pyproject only — not present in this brief)
- abstract placeholder names: not applicable (no mockup repos cited; project codes used in URN examples are the real EAWF code, which is published in repo metadata)
- machine identifiers: none
- credentials / API keys: none
