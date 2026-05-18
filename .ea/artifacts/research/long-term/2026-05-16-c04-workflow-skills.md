# C04 — Workflow & Skills — Eä framework long-term specs

**Cluster ID:** C04 — Workflow & Skills (index after split — see sub-clusters)
**Status:** `accepted` (split-as-index 2026-05-18 per Q19 / XB16 / G9)
**Created:** `2026-05-16T00:00:00Z`
**Author:** `claude-opus-4-7`
**Depends on:** C01 (foundations), C03 (spec infrastructure)
**Consumed by:** C05 (CLI), C06 (TUI), C10 (operations)

## Split index (Q19 — 2026-05-18)

Per operator Q19 + Codex G9, C04 splits into four sub-cluster briefs. The original C04 LOC stays in place as **the canonical content source**; each sub-cluster brief is a *thin contract index* pointing to the canonical sections inside this file. New 6 missing skills (Q9: `/coauthor`, `/memory`, `/agent-dispatch`, `/compress`, `/wave-spec`, `/security-review`) land inline in c04b per the audit XB16 fix.

- **`c04a-workflow.md`** — workflow commands: `/research`, `/roadmap`, `/prep`, `/flow`, `/audit`, `/ship`
- **`c04b-skills.md`** — skill manifest schema + envelope contract + 6 missing skills inline
- **`c04c-agent.md`** — agent entity (AgentReport, attempt_id, session_handle binding)
- **`c04d-runtime.md`** — runtime integration (cross-refs C07a)

Each sub-cluster ratifies independently in its own fresh CC session per V4.

## 1. Purpose + scope statement

C04 locks every workflow skill the Eä framework ships — `/research`, `/spike`, `/design`, `/roadmap`, `/prep`, `/flow`, `/audit`, `/ship`, `/review`, `/polish`, `/init`, `/blitz`, `/differentiate` — and the cross-skill machinery they share: the envelope contract, the registry, the per-skill manifest schema, the needs-user handshake, the plan-mode preview surface, the Edit-Plan subagent flow, and the session-policy gating.

The cluster's purpose is **two-fold**. First, it freezes the contracts every downstream cluster cites: C05 (CLI verbs that invoke skills + emit envelopes), C06 (TUI that renders envelopes + drives needs-user handshakes), C10 (operations docs that explain the skill catalog to operators). Second, it patches three holes the current implementation carries — (a) `/spike` and `/design` are first-class skills under [11][12] but not yet registered, (b) the skill manifest has no `dispatch.session_policy` field so V8 [1:226-271] cannot route retries through `--continue`, (c) the plan-mode preview specced in [P20-DIR] is not yet a typed envelope shape.

**Why this matters now.** The `/flow` skill at [19] already drives a six-step pipeline (research → prep → audit → ship → review → polish). The pipeline short-circuits on the first non-`ok` envelope and persists per-step checkpoints to `flow.jsonl` for resume. Without C04 locking the envelope `status` transition rules, the manifest round-trip invariant for V9 [1:273-315], and the needs-user context-storage location, downstream cluster work (C05's verb surface, C06's TUI overlay stack, C10's profile-conditional skill catalog) cannot proceed without colliding with current implementation choices baked across 13 skill modules and 11 plugin manifests.

**Scope.** Skills are first-class state-resident contracts; their inputs, outputs, mutations, and escalation paths are normative. Each skill subsection in §5.4 carries the canonical algorithm, the Pydantic body shape, the state mutations the skill performs (via daemon per V1 [1:24-53]), and the failure-mode catalog.

**Out of scope.** (a) TUI rendering of skill output — C06 owns the envelope-to-pane projection. (b) Skill implementation code beyond the contract — concrete `probe → action` bodies live in per-phase implementation plans, not in this cluster brief. (c) Per-runtime plugin sync mechanics — C07 owns plugin sync verb signatures, plugin doctor drift detection, and per-runtime native plugin shape catalogs [1:289-294].

## 2. Goals + non-goals

### Goals

1. **Skill catalog table** — every skill listed once, with one row per skill carrying profile gating, input args, output envelope kind, state mutations, escalation paths, dispatch session policy default.
2. **Skill envelope contract** — Pydantic schema + `status` enum + transition rules + needs-user contract + repair-commands rule.
3. **SkillManifest schema** — fields that round-trip through every supported runtime's native plugin shape per V9 [1:273-315]; fields scoped to a subset via `runtime: [<list>]`; `dispatch.session_policy` per V8 [1:226-271].
4. **Skill orchestration semantics** — when one skill invokes another (sequence diagrams: `/flow` → `/research`, `/research` → `/blitz`, `/roadmap propose` → plan-mode preview, `/prep` activate → wave dispatch fanout, `/ship` → `/audit` re-run).
5. **Skill registry contract** — how a profile contributes skills, how a user disables a skill, how the layered discovery [discovery.py:36] resolves precedence.
6. **needs-user handshake** — where context lives during pause (state.json? local file? IPC heartbeat?), how resume picks up.
7. **Skill versioning + deprecation policy** — semver-tracking, deprecation marker, retirement window.
8. **Per-skill failure-mode catalog** — every named failure path per skill, with detection and mitigation.
9. **/research spike convention deep-spec** — the AGENTS-locked spike workflow [AGENTS.md §"Spike workflow"] becomes a first-class `/spike` skill per [11].
10. **/roadmap propose|revise|apply|drop|reorder complete flow** — verb-by-verb behavior + V11 hard gate [1:74] + plan-mode preview integration.
11. **/prep activation gate spec** — full rule list including V11 gate, spike-brief surfacing, planner-subagent dispatch.
12. **/flow execution dispatch** — depends on C02 daemon for parallel coordination, resume semantics, drift detection.
13. **/audit DSL kinds catalog + extension API** — per C03 §5.6-5.7 audit-DSL kind table extended.
14. **/ship phase PR + close** — depends on C07 (VCS integration) but C04 owns the skill-side contract.
15. **/design first-class skill** — per [12] `/design` proposal: statechart + matrix + scenarios + 11-rule lint.
16. **Plan-mode preview** — 3-button Approve / Edit Plan / Reject AUQ shape per [P20-DIR].
17. **Edit Plan subagent flow** — subagent prompt template + return contract.
18. **dispatch.session_policy per-profile defaults** — research-profile may default `continue`; engineering-profile may default `fresh`.

### Non-goals

1. **TUI rendering** — overlay stack, modal pane composition, palette verb catalog → C06.
2. **Skill implementation code** — `probe → action` bodies → per-phase plans.
3. **CLI verb shape** — exit codes, output formats, completion → C05.
4. **Per-runtime plugin sync mechanics** — `eawf plugin sync` + doctor verb + per-runtime native plugin shape catalog → C07.
5. **Daemon IPC protocol** — JSON-RPC method catalog, lease semantics, supervisor restart policy → C02.
6. **Config layer composition** — profile contribution algorithm, conflict resolution → C08.
7. **Telemetry tile inventory** — `/metrics` overlay tiles → C09.

## 3. Prior verdicts cited

| Verdict | Source | Load-bearing where |
|---|---|---|
| **V1** — daemon Day-1 + smart-spawn writer; daemonless reader | [1:24-53] | Every skill that mutates state routes through daemon RPC; reads bypass. §5.4 per-skill mutation rows. |
| **V2** — three-tier specs Phase + Iter + Wave | [1:55-74] | `/roadmap propose` emits PhaseSpec scaffold; `/prep` emits IterSpec scaffold; `/flow` claim emits WaveSpec scaffold. §5.4.4, §5.4.5, §5.4.6. |
| **V3** — composable profile bundle + declared precedence | [1:76-96] | Skill enablement is profile-contributed; per-profile dispatch.session_policy default; `/audit` branch on `research` profile presence. §5.3, §5.4.4-5.4.6, §5.4.9. |
| **V8** — hybrid session reuse | [1:226-271] | Skill manifest `dispatch.session_policy: fresh | continue | hybrid`; default `hybrid`; profile-gated defaults. §5.3, §D7. |
| **V9** — native per-runtime plugins remain first-class | [1:273-315] | Skill manifest fields MUST round-trip through every supported runtime's native plugin shape, OR be marked `runtime: [<list>]`. §5.3, §D3. |
| **C01 D1** — broad URN kind scope | [2:191-228] | Skill output envelopes carry typed scope URNs and persist store URNs in footer fields. §5.2. |
| **C01 D6** — Memory tier per (scope_id, tier) | [2:480-484] | `/polish` memory pass operates on tier-namespaced rows. §5.4.10. |
| **C01 D7** — AgentReport append-only | [2:500-528] | Per-skill `agent_end` reports append; never overwrite. §5.4 every skill that emits a report. |
| **C03 D1** — PhaseSpec medium field set | [3:261-282] | `/roadmap propose` writes PhaseSpec body matching the schema. §5.4.4. |
| **C03 D11** — Mockup-required heuristic | [3:428-434] | `/design` skill enforces mockup-required at design-artifact time so the WaveSpec validator's WSV-07 [3:430] passes. §5.4.3. |
| **AGENTS §Roadmap procedure** | [AGENTS.md §"Roadmap procedure"] | Canonical /research → /roadmap propose → /roadmap revise → /roadmap apply → /prep flow. §5.4.4, §5.4.5. |
| **AGENTS §Spike workflow** | [AGENTS.md §"Spike workflow"] | `/spike` produces brief under `.ea/local/research/<YYYY-MM-DD>-<slug>.md`; chained dispatch reads `next:` line. §5.4.2. |

## 4. Decision matrix

Ten axes; the operator confirms each via the AUQ seed list in §8.

### D1 — Skill envelope `status` enum + transition rules

| Option | Description | Reco |
|---|---|---|
| D1.a | Current 5-status set: `ok | needs_user | blocked | failed | partial` | **Recommended** |
| D1.b | Add a 6th `cancelled` for operator-aborted runs | reject (out-of-band; envelope is per-run, not per-cancellation) |
| D1.c | Collapse `blocked` + `failed` into one (`failed`) | reject (blocked = instrument missing; failed = action raised — different repair) |

**Rationale (D1.a).** Current set at [envelope.py:48] is load-bearing across [skills/engine.py:218-336], the typed body schemas, and the strict validator. `blocked` and `failed` differ in repair: `blocked` → install missing instrument; `failed` → debug a raised exception. `partial` is the multi-step path where some steps land but the run cannot reach `ok` (e.g. `/flow` short-circuits mid-pipeline). `needs_user` is the canonical pause-for-decision shape (UserQuestion payload required per [user_question.py:31]). C04 freezes this set; new statuses require a brief and an Open Question per the locked verdict policy.

**Transition rules.** Each skill run produces exactly one terminal envelope. The status is set once at action close and never mutated. The state machine the engine [skills/engine.py:218-336] enforces:

```
   probe()              action()
ctx ──► ok ──► action()      ┬─► result.status ∈ {ok, needs_user, partial, blocked, failed}
            │                ├─► engine catches raised exception → failed
            │                └─► engine validates body shape per status
            ▼ probe.ok=False (hard instrument missing)
            blocked (short-circuit before action)
```

Strict rules:

- `status=needs_user` REQUIRES `body.user_question: UserQuestion` (2-4 options) per [user_question.py:47-52].
- `status in {blocked, failed}` REQUIRES `footer.repair_commands: list[str]` non-empty per [skills/engine.py:317-319].
- `header.finished_at >= header.started_at` enforced by [envelope.py:113-121].
- `header.skill == cls.name` enforced by the engine — runtime adapters cannot rebrand the skill mid-run.
- An action that raises any `Exception` is caught by the engine [skills/engine.py:288-310] and re-wrapped as `failed` with traceback body. The action never propagates exceptions to the runtime.

### D2 — Skill invocation surface

| Option | Description | Reco |
|---|---|---|
| D2.a | `eawf skill run <name>` CLI only | reject (loses runtime-native UX) |
| D2.b | Runtime-specific (`/<name>` in CC, equivalent in Codex/OpenCode) only | reject (loses CI-from-shell + script automation) |
| D2.c | Both surfaces — runtime-native `/<name>` for interactive, `eawf skill run` for CLI/CI | **Recommended** |

**Rationale (D2.c).** Per V9 [1:273-315] native plugins remain a first-class distribution channel; `/<name>` is the operator-facing affordance in every runtime. Per AGENTS rule 1 the CLI is dispatch; library implements — `eawf skill run <name>` is the dispatch entry that CI / shell-pipelines / cron schedules / non-CC runtimes use. Both surfaces route through the same library `run_skill` orchestrator [skills/engine.py:218]. The runtime adapter is responsible for collecting the args + scope URN + session URN; the skill body is runtime-agnostic.

### D3 — Skill registry storage

| Option | Description | Reco |
|---|---|---|
| D3.a | YAML SKILL.md frontmatter only | reject (cannot express the typed body schema or the `dispatch` block) |
| D3.b | Python entry-point only | reject (third-party / workspace overlay can't ship a YAML SKILL.md without installing a Python package) |
| D3.c | Both — Python entry-point for builtin + SKILL.md frontmatter for workspace / user overlay | **Recommended** |

**Rationale (D3.c).** Today's layered discovery [discovery.py:162-225] already merges three sources: workspace `.ea/skills/<name>/SKILL.md`, user `<local-path>`, and builtin `SKILL_REGISTRY` from [render/skills.py]. C04 keeps the three-tier overlay but adds the `SkillManifest` typed schema (§5.3) that both surfaces serialise to — frontmatter YAML for SKILL.md overlays, registered Python `Skill` subclasses for builtins. The manifest is the round-trip contract for V9 [1:289-294]: every field MUST project cleanly into every supported runtime's native plugin shape OR carry `runtime: [<list>]` so plugin sync skips it for runtimes that cannot host it.

### D4 — needs_user handshake context storage

| Option | Description | Reco |
|---|---|---|
| D4.a | Inline in the envelope body — operator's answer feeds the next call's `ctx.args` | partial (lossy across long pauses) |
| D4.b | Per-(skill, scope, session) checkpoint file under `.ea/local/skills/checkpoints/<session>.json` | reject (loses cross-runtime resume) |
| D4.c | Append a `needs_user_pause` envelope to `store/event.jsonl` + a `flow_checkpoint`-style row when inside `/flow` | **Recommended** |

**Rationale (D4.c).** `flow.jsonl` already carries `flow_checkpoint` payloads [skills/flow.py:492-541] that record `(flow_id, step_index, step_name, last_safe, payload_hash, parent_state_hash, parent_git_head, parent_profile_ids, args_per_step_hash)`. C04 extends this to a per-skill `needs_user_pause` envelope appended to `store/event.jsonl` (or a new `store/needs_user.jsonl` per StoreKind) when ANY skill terminates `status=needs_user`. The envelope carries the `UserQuestion`, the recovered `ctx.args`, and the per-(skill, scope, session) resume handle. Resume is `eawf skill resume <pause-urn>` — daemon-mediated, drift-checked per the existing flow.drift comparison [skills/flow.py:308-367]. This means needs-user pause is a state-resident, audit-replayable event — not a transient memory blob.

### D5 — Plan-mode preview render source

| Option | Description | Reco |
|---|---|---|
| D5.a | Static template populated from `state.phases`, `state.iters`, `state.waves` | reject (cannot render PhaseSpec body fields like outcome/kpis) |
| D5.b | Aggregate from PhaseSpec + IterSpecs + WaveSpecs under the proposed phase | **Recommended** |
| D5.c | Free-text agent draft | reject (operator approval surface MUST cite verdict-id markers) |

**Rationale (D5.b).** Per C03 §5.2-5.4 [3:216-434] the three-tier specs already carry the body fields a plan preview needs: `PhaseSpec.outcome`, `PhaseSpec.kpis`, `PhaseSpec.ship_criteria`, `IterSpec.sub_goal`, `IterSpec.wave_groups`, `WaveSpec.behaviors`, `WaveSpec.failure_modes`, `WaveSpec.implements`. Plan-mode preview rendered from the spec aggregate ensures the operator sees the exact body that auditing (`verify_implements` per C03 §5.7 [3:588-712]) will check at ship-gate. Free-text agent drafts decouple operator approval from the audit gate; reject.

### D6 — Edit Plan subagent prompt template

| Option | Description | Reco |
|---|---|---|
| D6.a | One-shot prompt: "edit this plan to address: <feedback>" | reject (lossy; doesn't carry verdict citations) |
| D6.b | Structured prompt with PhaseSpec body + operator feedback + verdict citation requirements | **Recommended** |
| D6.c | Multi-round AUQ inside the subagent | reject (collides with /spike's multi-round model; doubles the surface) |

**Rationale (D6.b).** The Edit Plan subagent is dispatched when the operator hits Edit on the plan-mode preview AUQ. The subagent receives: (a) the current PhaseSpec body (rendered), (b) the IterSpecs + WaveSpecs aggregated under it, (c) the operator's free-text feedback, (d) the verdict-id citation contract (every WaveSpec must cite ≥1 verdict per [3:402]), (e) the spec-version constraint (`schema_version: 1`). The subagent edits the spec files via `eawf {phase,iter,wave} spec init / edit / set` and returns an `agent_end` report per AGENTS rule 19. The parent skill re-renders the plan preview and re-issues the AUQ. This composes cleanly with /spike's multi-round AUQ pattern — they operate at different levels (spike = direction-only; Edit Plan = spec-bound).

### D7 — Per-profile dispatch.session_policy default

| Option | Description | Reco |
|---|---|---|
| D7.a | All profiles default `hybrid` (current V8 spec) | partial (works but doesn't exploit profile semantics) |
| D7.b | `research` profile default `continue`; `engineering` profile default `fresh`; cross-profile (`hybrid`) default `hybrid` | **Recommended** |
| D7.c | Per-skill manifest field with no profile-gating | reject (forces per-skill explicit choice when profile-level default suffices) |

**Rationale (D7.b).** Per V8 [1:266-270] skill manifest `dispatch.session_policy` is profile-gated. Rationale: `research` profile skills (`/research`, `/spike`, `/design`, `/blitz`) accrete context across multiple invocations — operator iterates on a question; continuation preserves the conversation history without re-explaining the topic. `engineering` profile skills (`/prep`, `/flow`, `/audit`, `/ship`, `/review`, `/polish`) tend to run against fresh contexts — each wave is a new task; fresh dispatch hits the KV cache prefix maximally. Manifest-level override always wins over profile default; profile default wins over framework default (`hybrid`).

### D8 — Skill versioning policy

| Option | Description | Reco |
|---|---|---|
| D8.a | Semver only (e.g. `1.0` → `1.1` → `2.0`) | partial (no machine deprecation surface) |
| D8.b | Semver + Python `__deprecated_since__: str | None` + `__removed_in__: str | None` markers | **Recommended** |
| D8.c | Numeric `schema_version: int` only (matches Pydantic body schemas) | reject (loses semver clarity) |

**Rationale (D8.b).** Today's SkillRegistry [render/skills.py] has a `version: str = "1.0"` field that nobody bumps. C04 adds two optional markers — `deprecated_since: <semver>` (skill still functions, runtime adapter MAY emit a deprecation banner) and `removed_in: <semver>` (next release at that semver level removes the skill entirely). Sunset window: minimum **3 alpha versions** between `deprecated_since` and `removed_in` to give external runtime adapters + operator workflows time to migrate. The Pydantic body schemas keep their own `schema_version: int` for the wire-format contract — orthogonal to skill-level versioning.

### D9 — Skill registry visibility scope

| Option | Description | Reco |
|---|---|---|
| D9.a | Single-tier (builtin only) | reject (workspace overlay essential per discovery.py) |
| D9.b | Three-tier (workspace → user → builtin) — current shape from [discovery.py:181-219] | **Recommended** |
| D9.c | Four-tier (workspace → repo → user → builtin) | reject (workspace already covers repo for single-repo projects; cross-repo workspace deferred to C07) |

**Rationale (D9.b).** Today's `discover_skills(workspace=...)` [discovery.py:162] already does three-tier precedence. Workspace overlay wins; user falls back; builtin fills the rest. C04 freezes this. The `runtime: [<list>]` field in SKILL.md frontmatter (D3) filters per-runtime visibility; a workspace overlay can hide a builtin skill from a specific runtime without removing the Python class. C07's plugin-sync verb honours this when projecting AGENTS-source-of-truth to per-runtime native plugin trees.

### D10 — /flow expansion to include /spike + /design

| Option | Description | Reco |
|---|---|---|
| D10.a | Keep current 6-step order (research → prep → audit → ship → review → polish) | partial (no /spike or /design integration) |
| D10.b | Insert `/spike` before `/research`, `/design` after `/research` (profile-gated) | **Recommended** |
| D10.c | Make `/flow` step list per-profile-configurable | reject (over-engineered; pick one strong default first) |

**Rationale (D10.b).** Per [12] the design-subframework proposal adds `/design` after `/research --final` and before `/roadmap propose`. Per [11] `/spike` runs when the question shape is multi-axis or when a phase needs postmortem-before-rebuild. The new 8-step `/flow` order is:

```
spike (optional, when --spike) → research → design (optional, --design or research-profile + UI-scope) → prep → audit → ship → review → polish
```

`/spike` and `/design` are opt-in via flags or profile gating. The flow runner [skills/flow.py:771-887] already iterates an explicit `flow_order` tuple; C04 makes the tuple per-profile-resolvable but keeps the default ordering fixed across profiles for predictability.

## 5. Proposed schemas, API, protocol

### 5.1 Skill catalog

Thirteen skills total: ten currently registered (`/research`, `/prep`, `/audit`, `/ship`, `/review`, `/polish`, `/init`, `/roadmap`, `/differentiate`, `/flow`, `/blitz`) plus two C04-mandated additions (`/spike`, `/design`). The catalog table lists profile gating, the args the skill consumes, the body model the envelope carries, the state mutations the skill performs, escalation paths (which other skills it dispatches), and the dispatch session-policy default.

| Skill | Profile gate | Input args (selected) | Body model | State mutations | Escalations | Session policy |
|---|---|---|---|---|---|---|
| `/research` | core | `topic`, `depth (quick|normal|deep)`, `final`, `blitz` | `ResearchBody` | append `event.jsonl` (10+ rows); append `research.jsonl` on `--final` | `/blitz` when residual unknowns > 1 | `continue` (research-profile-gated default) |
| `/spike` | research | `slug`, `final`, `from-briefs`, `postmortem` | `SpikeBody` (new — §5.3.2) | append `event.jsonl`; write `.ea/local/research/<date>-<slug>.md` on `--final` | declares `next:` skill in body (chained dispatch reads it) | `continue` |
| `/design` | research, engineering | `surface-slug`, `final`, `from-brief` | `DesignBody` (new — §5.3.3) | append `event.jsonl`; write `.ea/local/research/<date>-<surface>-design.md` | `/blitz` on residual unknowns | `continue` |
| `/roadmap` | core | sub-verb (`propose|revise|apply|drop|reorder|show`), `phase-id`, `from-briefs`, `--add-wave`, `--remove-wave`, `--set-deps`, `--retitle` | `RoadmapBody` | propose: append PhaseSpec stub + Phase row PLANNED; revise: edit PhaseSpec/IterSpec/WaveSpec body fields; apply: validate PLANNED; drop: PLANNED → ARCHIVED | plan-mode preview AUQ; Edit Plan subagent on `revise`; `/prep` on `apply` | `fresh` |
| `/prep` | core | `phase-id`, `fix` (post-audit), `approval` | `PrepBody` | activate PLANNED → ACTIVE (gated on V11); set Wave.status PENDING → CLAIMED (per wave dispatch) | planner subagent (Case B); `/research` spike before claim (optional); per-wave worktree subagents | `fresh` |
| `/flow` | core | `topic`, `stop_after`, `args_per_step`, `resume_from`, `flow_id` | `FlowBody` | append `flow.jsonl` (start, checkpoints, terminal) | dispatches `/spike?` → `/research` → `/design?` → `/prep` → `/audit` → `/ship` → `/review` → `/polish` | `fresh` (per-step; flow itself spawns fresh per wave) |
| `/audit` | core | `scope` (phase/iter/wave), `kind (evaluation|ship-gate)`, `checks` | `AuditBody` | append `event.jsonl`; create `Audit` row; set HypothesisVerdict; create CheckResult list | reviewer subagent (per-check) on `--reviewers` | `fresh` |
| `/ship` | core | `commit`, `push`, `pr (open|ready|draft|close|none)`, `artifact_paths`, `pr_body` | `ShipBody` | validate artifacts; emit `ship.audit_gate`, `ship.commit`, `ship.push`, `ship.pr` events; `Wave.commit` set on cherry-pick | `/audit` re-run on retry; PR-open via `gh` shell-out (C07) | `fresh` |
| `/review` | core | `pr`, `base`, `head`, `recommendation`, `post` | `ReviewBody` | append `event.jsonl`; emit `reviewer_report` per finding | reviewer subagent (fresh-context) per area | `fresh` |
| `/polish` | core | `scope (dir|file)`, `report_only`, `max_fixes` | `PolishBody` | append `event.jsonl`; memory promote/prune; backlog inserts | `/audit` to re-verify after polish | `fresh` |
| `/init` | core | `answers (WizardAnswers dict)`, `output_dir`, `force` | `InitBody` | write `.ea/state.json`, `.ea/config.yaml`, `AGENTS.md` (managed regions), per-runtime plugin tree | questionary wizard UI (runtime-adapter side) | `fresh` (one-shot) |
| `/differentiate` | engineering | `preset (minimal|adaptive|full)`, `approval`, `runtime` | `DifferentiateBody` | append `event.jsonl`; write per-runtime agent .md files (C07-owned) | none | `fresh` |
| `/blitz` | core | `residual_unknowns`, `followup_research_args` | `BlitzBody` | append `event.jsonl` per recursion-guard bump | `/research` (chained) | inherits caller |

The table is normative. C05 builds verb-noun matrix from this; C06 builds palette verb registry from this; C10 generates docs from this. Adding or removing a row requires a brief and an Open Question per the locked-verdict policy.

### 5.2 Envelope contract

The envelope contract is **frozen** at [envelope.py:48-178]. C04 ratifies the freeze and names the strict-validation rules.

```python
# src/eawf/render/envelope.py (frozen — current shape)

EnvelopeStatus = Literal["ok", "needs_user", "blocked", "failed", "partial"]

class EnvelopeHeader(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skill: SkillName                    # frozen literal; "/research", "/spike", ...
    scope_id: str                       # urn:eawf:v1:<scope-kind>:<owner>/<id>
    session: str                        # urn:eawf:v1:session:<scope>/<id>
    started_at: datetime
    finished_at: datetime               # >= started_at (model_validator)
    status: EnvelopeStatus
    instrument_probe: dict[str, InstrumentStatus] = Field(default_factory=dict)

class EnvelopeFooter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    persisted_artifacts: list[str] = Field(default_factory=list)        # URNs
    persisted_store_records: list[str] = Field(default_factory=list)    # URNs
    state_mutations: list[str] = Field(default_factory=list)            # JSONPath-ish
    evidence_refs: list[str] = Field(default_factory=list)              # URNs
    next_valid_actions: list[str] = Field(default_factory=list)         # CLI strings
    warnings: list[EnvelopeWarning] = Field(default_factory=list)
    repair_commands: list[str] | None = None                            # required when status in {blocked, failed}

EnvelopeBody = str | dict[str, Any]    # markdown raw OR typed body model

class OutputEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    header: EnvelopeHeader
    body: EnvelopeBody
    footer: EnvelopeFooter
```

**Strict-validation rules (enforced at envelope build time + at CLI parse-back time):**

| Rule | Condition | Failure mode |
|---|---|---|
| EV-01 | `header.skill` is a known builtin OR overlay-registered SkillName | reject unknown skill — runtime adapter cannot fabricate a skill |
| EV-02 | `header.scope_id` parses as a v1 URN with kind in C01 §5.2.2 catalog | reject malformed URN |
| EV-03 | `header.finished_at >= header.started_at` | model_validator at [envelope.py:113-121] |
| EV-04 | `body.user_question: UserQuestion` when `status=needs_user` | strict validator |
| EV-05 | `footer.repair_commands` non-empty when `status in {blocked, failed}` | engine fallback at [engine.py:317-319] |
| EV-06 | `footer.persisted_store_records` URNs all resolve when daemon-mediated reads | daemon-side verify on append |
| EV-07 | Round-trip: `from_markdown(to_markdown(env)) == env` for all JSON-safe envelopes | byte-stable property test |
| EV-08 | When `status=partial`, `body.steps` (for FlowBody-like multi-step bodies) is non-empty | per-body schema |
| EV-09 | `header.session` matches a session active in `state.agent_sessions` (when daemon available; advisory in CI) | daemon-side check |
| EV-10 | `footer.next_valid_actions` entries are CLI strings (start with `eawf` or `/`) | format validator |

**needs_user contract.** Every `status=needs_user` envelope MUST carry a `body.user_question: UserQuestion` with 2-4 options per [user_question.py:47-52]. The runtime adapter projects this onto its native picker: Claude Code maps to `AskUserQuestion`; Codex maps to text-prompt with numeric options; OpenCode maps to its equivalent. The skill body knows nothing about the runtime — only the typed UserQuestion shape. Resume after operator response routes the chosen `label` back as `ctx.args["__user_choice__"]: str` on the next invocation.

**body discriminator (proposed).** Every typed body carries a `kind: Literal["<body-name>"]` field (e.g. `ResearchBody.kind: Literal["ResearchBody"]`). The strict validator uses `kind` as the discriminator for dispatch — given an envelope with `header.skill="/research"`, the validator picks `ResearchBody`; round-trip preserves `kind` byte-stably. This is additive — current bodies do not carry `kind`; C04 adds it via Pydantic's discriminated union pattern.

### 5.3 SkillManifest schema

The manifest is the round-trip contract per V9 [1:273-315]. Every field MUST project cleanly through every supported runtime's native plugin shape OR be marked `runtime: [<list>]` so plugin sync skips it. Today's SKILL.md frontmatter is a partial form; C04 lifts it to a typed Pydantic schema.

```python
# src/eawf/skills/manifest.py (new — C04)

from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


SessionPolicy = Literal["fresh", "continue", "hybrid"]


class SkillDispatch(BaseModel):
    """Dispatch-side controls — V8 [1:226-271] session reuse + cache hooks."""

    model_config = ConfigDict(extra="forbid")

    session_policy: SessionPolicy | None = None
    # When None, profile default applies; when set, overrides the profile default.
    # Per D7 (above): research-profile default `continue`; engineering-profile default `fresh`.

    cache_control: Literal["5m", "1h", "ephemeral"] | None = None
    # Per V8 cache-control interplay [1:251-254]. Default 5m matches stable-prefix
    # cache TTL. Skills that operate on volatile inputs may declare `ephemeral`.

    parallel_within_run: bool = False
    # When True, the skill MAY dispatch subagents in parallel (e.g. /research
    # multi-agent fanout). When False, the skill is sequential.

    max_subagents: int | None = Field(default=None, ge=1, le=16)
    # Cap on concurrent subagents the skill spawns. None = unlimited (subject to
    # daemon-side `agent.max_parallel`).


class SkillVisibility(BaseModel):
    """Visibility + invocation controls per D9 (runtime visibility)."""

    model_config = ConfigDict(extra="forbid")

    user_invocable: bool = True
    # When False, skill is dispatchable only by other skills (not by operator).

    disable_model_invocation: bool = False
    # When True, runtime adapter blocks LLM-side invocation; operator-only.
    # Mirrors current [discovery.py:71] field.

    runtimes: list[str] = Field(default_factory=list)
    # Empty = visible to every runtime. Non-empty = filter — skill rendered only
    # for the named runtimes during plugin sync.


class SkillProfiles(BaseModel):
    """Profile-gating per V3 [1:76-96]."""

    model_config = ConfigDict(extra="forbid")

    requires: list[str] = Field(default_factory=list)
    # Profile ids that MUST be enabled for the skill to register.
    # Empty = core (always available).

    forbids: list[str] = Field(default_factory=list)
    # Profile ids whose presence disables the skill.
    # Empty = no forbid.


class SkillDeprecation(BaseModel):
    """Versioning + sunset per D8."""

    model_config = ConfigDict(extra="forbid")

    deprecated_since: str | None = None    # semver string, e.g. "0.4.0"
    removed_in: str | None = None          # semver string, e.g. "0.7.0"
    replacement: str | None = None         # "/<name>" of the replacement skill

    # Invariant: when both `deprecated_since` and `removed_in` are set, the
    # gap MUST be at least 3 minor versions (alpha cadence).


class SkillManifest(BaseModel):
    """Typed manifest for a skill — round-trips through every runtime plugin shape per V9."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["SkillManifest"] = "SkillManifest"

    name: str = Field(pattern=r"^/[a-z][a-z0-9_-]{0,31}$")
    description: str = Field(min_length=10, max_length=500)
    version: str = Field(pattern=r"^\d+\.\d+(\.\d+)?$")    # "1.0" or "1.0.0"
    argument_hint: str = ""

    body_model: str | None = None
    # Dotted import path of the Pydantic body model (e.g. "eawf.skills.bodies.research.ResearchBody").
    # When None, the body is a plain markdown string.

    visibility: SkillVisibility = Field(default_factory=SkillVisibility)
    profiles: SkillProfiles = Field(default_factory=SkillProfiles)
    dispatch: SkillDispatch = Field(default_factory=SkillDispatch)
    deprecation: SkillDeprecation | None = None
```

**Round-trip projection.** Plugin sync (C07-owned) reads the SkillManifest and projects it to the runtime's native plugin shape:

- **Claude Code** — SKILL.md YAML frontmatter at `build/eawf-plugin/skills/<name>/SKILL.md` carrying `name`, `description`, `argument-hint`, `user-invocable`, `disable-model-invocation`, `version`. The body markdown stays in the same file. Fields not in the CC native shape (e.g. `dispatch.session_policy`) are stored in a hidden HTML comment block at the top of the body: `<!-- eawf:manifest-extra session_policy: fresh ... -->`. The CC runtime adapter parses the comment block on dispatch; CC's own plugin system ignores it.
- **Codex CLI** — TBD per C07 native shape catalog. The same SkillManifest projects to whatever Codex expects (slash-command directory, JSON manifest, …).
- **OpenCode** — TBD per C07 native shape catalog.

**Round-trip rule (V9).** If a SkillManifest field cannot project cleanly to a runtime's native plugin shape (lossy or rejected), the manifest MUST mark it `runtime: [<list>]` so plugin sync skips it for that runtime. Operators see a `eawf plugin doctor` drift warning when a manifest field is filtered.

### 5.4 Per-skill subsection

Each subsection: canonical algorithm + input contract + output envelope + state mutations + failure modes. Where the current implementation already encodes the algorithm, the subsection cites the source line and flags any C04 delta.

#### 5.4.1 /research

**Purpose.** Read-only investigation of an open question; produces a research brief (`--final`) or surfaces findings inline. Per [research/SKILL.md] and [skills/research.py:1-29].

**Inputs.** `topic: str | None`, `depth: Literal["quick","normal","deep"]="normal"`, `final: bool=False`, `blitz: bool=True`. The `slug` / `from-briefs` flags are passed through as args-dict entries for the brief writer (per [11:38-46]).

**Algorithm.** Ten steps from [research.py:14-22]: (1) probe instruments, (2) resolve scope, (3) detect continuation (v0.1 always fresh), (4) define questions (count scales by depth — quick=1, normal=2, deep=3 with deep degrading to needs_user for fanout), (5) dispatch parallel reviewers (v0.1: needs_user when depth=deep), (6) synthesise options, (7) peer review (v0.1 skip), (8) recommend, (9) persist brief when `--final`, (10) record decision candidates (v0.1 skip).

**Output.** `ResearchBody` payload + envelope status `ok` (happy path) or `needs_user` (deep + fanout-pending). Footer carries `persisted_store_records` with the brief URN when `--final`. Auto-chains `/blitz` when residual unknowns count > 1 per [research.py:301-336].

**State mutations.** Append-only: 8-10 `EVENT` rows to `event.jsonl` (one per algorithm step) + on `--final` one `RESEARCH` row to `research.jsonl`. No mutation of `state.json` rows.

**Failure modes.**

| F# | Failure | Detection / mitigation |
|---|---|---|
| RES-F1 | Instrument missing (e.g. `git` absent) | probe → status=blocked; footer.repair_commands = `["install git ..."]` |
| RES-F2 | `--final` write fails (disk full / permission) | engine catches → status=failed; traceback in body; repair = `["check write permission on .ea/store/"]` |
| RES-F3 | Topic too broad → unbounded fanout | depth=deep → needs_user with UserQuestion offering `proceed_default / adjust_agents / cancel` |
| RES-F4 | Residual unknowns > 1 + blitz disabled | body lists unknowns; next_valid_actions includes `eawf skill run /blitz --residual ...` |
| RES-F5 | `--final` + topic == scope_id (no real question) | warning at body-time; persist allowed but footer warning emitted |

**Session policy.** `continue` when research-profile enabled (D7); `hybrid` otherwise.

#### 5.4.2 /spike (NEW)

**Purpose.** Time-boxed read-only multi-axis investigation that unblocks `/roadmap propose`, `/design`, or `/smoke-test`. Per [11].

**Inputs.** `slug: str` (or AUQ), `final: bool=False`, `from_briefs: list[BriefPathStr]=[]`, `postmortem: PhaseIdStr | None=None`.

**Algorithm.** Ten steps from [11:46-58]: (1) resolve slug, (2) frame multi-axis unknown, (3) survey (read prior briefs + source + git log; optionally fanout subagents), (4) multi-round AUQ picks (3-6 axes per round), (5) scope-delta surfacing vs `--from-briefs`, (6) critical-contracts capture, (7) open follow-ups, (8) hand-off declaration (`next:` line), (9) self-lint, (10) write to `.ea/local/research/<YYYY-MM-DD>-<slug>.md` on `--final`.

**Output.** `SpikeBody` (new) + envelope status `ok` (final) or `needs_user` (mid-round). Body carries: rolling matrix (round × axis × pick), optional postmortem arc (gap matrix + RC-N + salvage matrix), scope-delta tables, critical-contracts table, open follow-ups with labels, hand-off declaration.

```python
# src/eawf/skills/bodies/spike.py (new — C04)

class SpikeRound(BaseModel):
    model_config = ConfigDict(extra="forbid")
    round_id: int
    theme: str
    axes: list[SpikeAxisPick]      # 3-6 picks per round

class SpikeAxisPick(BaseModel):
    model_config = ConfigDict(extra="forbid")
    axis: str
    pick: str
    rationale: str            # one-line cite or "because X"

class SpikePostmortem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase_id: str
    gap_matrix: list[dict[str, str]]    # rows: {surface, brief_verdict, shipped, verdict}
    root_causes: list[str]              # RC-1, RC-2, ...
    salvage: list[dict[str, str]]       # rows: {file, loc, salvage_pct, what_stays}

class SpikeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["SpikeBody"] = "SpikeBody"
    slug: str
    rounds: list[SpikeRound] = Field(default_factory=list)
    postmortem: SpikePostmortem | None = None
    scope_expansions: list[dict[str, str]] = Field(default_factory=list)
    scope_reductions: list[dict[str, str]] = Field(default_factory=list)
    critical_contracts: list[dict[str, str]] = Field(default_factory=list)
    poc_manifest: list[dict[str, str]] = Field(default_factory=list)
    open_followups: list[SpikeFollowup] = Field(default_factory=list)
    next_skill: str                # literal `next:` line; e.g. "/roadmap propose --phase P21 --from-briefs <this>"
    persisted_brief: str | None = None
    user_question: UserQuestion | None = None

class SpikeFollowup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Literal["next-spike", "next-research", "hypothesis-open", "blocked-on-EU", "blocked-on-demo"]
    text: str
```

**State mutations.** Append-only `event.jsonl` rows (`spike.resolve_slug`, `spike.frame`, `spike.round_open`, `spike.round_close`, `spike.write_brief`); on `--final`, write `.ea/local/research/<date>-<slug>.md` (file-system, not state-resident). No `state.json` mutation per [11:67].

**Failure modes.**

| F# | Failure | Detection / mitigation |
|---|---|---|
| SPK-F1 | Multi-round AUQ never converges (operator never declares "no more axes") | hard cap at 12 rounds; status=needs_user with "spike too broad — break into sub-spikes" |
| SPK-F2 | `--from-briefs` brief does not exist | validate at start; status=failed; repair = `["fix --from-briefs path"]` |
| SPK-F3 | Postmortem mode but `--postmortem <phase-id>` does not match any phase | validate; status=failed |
| SPK-F4 | Scope-delta forced when `--from-briefs` empty | reject if expansion/reduction tables empty AND `--from-briefs` set — must use the inputs |
| SPK-F5 | Hand-off `next:` line missing | self-lint rejects; status=needs_user with the operator picking the next skill from AUQ |

**Session policy.** `continue` (research-profile default). Spike across multiple invocations preserves the rolling matrix state. The runtime's `--continue <session-id>` route applies.

**Status today.** SKILL.md drafted at [11] but no Python class registered in `src/eawf/skills/`. C04 mandates implementation; phase landing TBD (P21 candidate per [12:172-176]).

#### 5.4.3 /design (NEW)

**Purpose.** Read-only design pass for an interactive surface — produces a triangulated artifact (statechart + action×context matrix + journey scenarios) + 11-rule lint contract. Per [12] and [.claude/skills/design/SKILL.md].

**Inputs.** `surface_slug: str` (or AUQ), `final: bool=False`, `from_brief: BriefPathStr | None=None`.

**Algorithm.** Seven steps from [12:35-45]: (1) resolve surface slug, (2) Round 1 AUQ — personas + goals, (3) Round 2 AUQ — event-source taxonomy + statechart skeleton + liveness contracts, (4) Round 3 AUQ — action × context matrix (no blank cells), (5) Round 4 AUQ — journey scenarios (≥1 per persona × goal, ≥1 time-advance per long-lived view, edge scenarios), (6) self-lint (11 rules), (7) write `.ea/local/research/<YYYY-MM-DD>-<surface>-design.md` on `--final`.

**Output.** `DesignBody` (new) + envelope status `ok` or `needs_user`. Body carries: personas-and-goals table, event-source taxonomy (six categories per [.claude/skills/design/SKILL.md:148-159]), statechart YAML, liveness contracts, action × context matrix, cross-component contracts, journey scenarios. On `--final` with residual unknowns > 0, footer carries a `/blitz` follow-up per the standard recursion guard.

```python
# src/eawf/skills/bodies/design.py (new — C04)

class DesignBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["DesignBody"] = "DesignBody"
    surface_slug: str
    artifact_path: str | None = None
    rounds_completed: int = Field(ge=0, le=4)
    event_sources_declared: list[str] = Field(default_factory=list)
    event_sources_na: list[dict[str, str]] = Field(default_factory=list)
    screens_count: int = 0
    long_lived_views_count: int = 0
    matrix_cells_filled: int = 0
    matrix_cells_noop: int = 0
    scenarios_count: int = 0
    lint_status: Literal["pass", "fail", "pending"]
    residual_unknowns: list[str] = Field(default_factory=list)
    followup_actions: list[str] = Field(default_factory=list)
    user_question: UserQuestion | None = None
```

**State mutations.** Append-only `event.jsonl` rows per round (`design.round_open`, `design.round_close`, `design.lint`); on `--final`, write `.ea/local/research/<date>-<surface>-design.md`. No `state.json` mutation.

**Failure modes.**

| F# | Failure | Detection / mitigation |
|---|---|---|
| DES-F1 | Lint L1-L11 fails | status=needs_user with the failing rule + a "fix" subagent dispatch option |
| DES-F2 | Round 2a category declared neither present nor N/A | validator rejects; round cannot close |
| DES-F3 | Multi-char key sequence as flat keymap entry | lint L6 rejects; status=needs_user |
| DES-F4 | Matrix has blank cell | lint L4 rejects; status=needs_user |
| DES-F5 | Long-lived view without liveness contract | lint L5 rejects; status=needs_user |
| DES-F6 | UI-scope wave proposed without a referenced design artifact | enforced at `/roadmap propose` time — see §5.4.4 |

**Session policy.** `continue` (research-profile default).

**Status today.** SKILL.md drafted at [.claude/skills/design/SKILL.md] but no Python class. C04 mandates implementation; phase landing TBD (P21 candidate per [12:172-176]).

#### 5.4.4 /roadmap

**Purpose.** Plan / revise / apply / drop / show PLANNED-scope phases on the eawf roadmap queue. Per [roadmap/SKILL.md] and AGENTS §"Roadmap procedure".

**Sub-verbs.**

- **`propose`** — stages a new PLANNED phase + its `P##-I01` iter on the queue without any waves yet. Emits a `needs_user` envelope with the rendered plan text. Operator-facing decision surface: plan-mode preview AUQ (§5.6).
- **`revise`** — edits the PLANNED scope via structured flags: `--add-wave`, `--remove-wave`, `--set-deps`, `--retitle`. Wave-level mutations route through the P19-W01 PENDING-only transitions [AGENTS.md §"Planned-scope revisability"].
- **`apply`** — post-propose confirmation. Validates that the phase is PLANNED with at least one wave; emits an `ok` envelope; the actual planning was already persisted by `propose`. Handoff into `/prep`.
- **`drop`** — archives a PLANNED phase (PLANNED → ARCHIVED) when the operator rejects.
- **`reorder`** — renumbers PLANNED phases in the queue. **Deferred** per C00 [1:574] — operator drops + re-proposes to swap order. C04 keeps the verb in the catalog for future v0.5+ landing.
- **`show`** — renders the queue: text table (default), markdown (`--md`), or JSON envelope (`--json`).

**V11 hard gate (applies to `apply` and `prep activate`).** Per [AGENTS.md §"Planned-scope revisability"]:

- Phase status MUST be PLANNED.
- Phase MUST have ≥1 wave under it (at least `P##-I01-W01`).
- Every dep phase in `Phase.depends_on` MUST be CLOSED.
- `apply` emits `ok` when gate passes; emits `failed` with repair commands when it doesn't.

**Plan-mode preview.** When `/roadmap propose` runs interactively, the skill enters Claude Code plan mode via `EnterPlanMode` [per [P20-DIR]; CC-runtime-specific] then emits a `needs_user` envelope with the plan text rendered from the PhaseSpec aggregate (§5.6). Three buttons: **Approve** → `eawf roadmap apply <P##>`; **Edit Plan** → spawn Edit Plan subagent (§5.7); **Reject** → `eawf roadmap drop <P##>`. For non-CC runtimes the AUQ falls back to a text-prompt with `y/N/edit` options.

**Output.** `RoadmapBody` payload + envelope status:
- `propose` → `needs_user` with `user_question` carrying the 4-option picker (`approve / edit / research_more / defer`).
- `revise` → `ok` after each structured mutation.
- `apply` → `ok` (gate passes) or `failed` (gate fails).
- `drop` → `ok`.
- `show` → `ok` with table/md/json body.

**State mutations.** Per sub-verb:
- `propose` writes a new Phase row PLANNED + a default `P##-I01` Iter row PLANNED + a stub PhaseSpec at `.ea/specs/<P##>/spec.md` per C03 §5.9. Daemon-mediated per V1.
- `revise --add-wave`/`--remove-wave`/`--set-deps`/`--retitle` mutates PLANNED waves only (or PENDING waves under ACTIVE phase per AGENTS-rule-20 invariant). PhaseSpec / WaveSpec body fields edited at the same time.
- `apply` is read-only with respect to `state.json`; emits an `event.jsonl` confirmation row.
- `drop` flips PLANNED → ARCHIVED on the Phase row + archives owned IterSpecs/WaveSpecs per C03 §5.4.15 archival.
- `reorder` (deferred) — placeholder.

**Failure modes.**

| F# | Failure | Detection / mitigation |
|---|---|---|
| RM-F1 | `propose` when phase id already exists | reject before write; repair = `["use /roadmap revise <P##> or /roadmap drop <P##>"]` |
| RM-F2 | `propose --from-briefs <path>` brief does not exist | validator rejects; status=failed |
| RM-F3 | `revise --add-wave` against an ACTIVE phase wave that's not PENDING | rule-20 invariant; status=failed |
| RM-F4 | `apply` when gate fails (no waves, dep not CLOSED) | status=failed; repair lists the failed gate predicate |
| RM-F5 | `drop` against an ACTIVE phase | status=failed; phase must be PLANNED |
| RM-F6 | UI-scope wave proposed without design artifact reference | warning at `propose`; hard reject at `apply` when research-profile + engineering-profile both enabled |

**Session policy.** `fresh` (each propose / revise / apply is a discrete state mutation).

#### 5.4.5 /prep

**Purpose.** Activate the next PLANNED phase: surface its DAG for operator approval, run the V11 hard gate via `eawf phase activate`, dispatch subagents per wave. Per [prep/SKILL.md] and [skills/prep.py].

**Inputs.** `iter_id: str | None` (defaults to `P00-I01` if unset; live impl resolves from active state), `phase_id: str | None`, `fix: bool=False` (post-audit fix-list mode), `approval: Literal["ask", "auto"]="auto"`.

**Activation gate (full rule list).** Per V11 [1:74] + [prep/SKILL.md:11-37]:

1. **Phase resolves.** `phase_id` exists in `state.phases`. If not → exit 4 with hint `Run "eawf roadmap propose --phase <id> --title ..." first.`
2. **Phase status PLANNED.** ACTIVE / CLOSED / ARCHIVED phases reject; ACTIVE re-prep falls through Case A but skips activate.
3. **Wave plan complete.** ≥1 wave under the phase (≥1 iter, ≥1 wave per iter). Empty wave DAG → Case B (planner subagent).
4. **Dep phases CLOSED.** Every `Phase.depends_on` entry's status MUST be CLOSED.
5. **Worktree clean.** `git status` clean before activation (warning, not block); allows uncommitted experimentation but flags.
6. **Branch currency.** Current branch matches the long-running phase branch per AGENTS §"Branch currency".
7. **Subagent prereqs.** Every wave has `success_criteria`, `agent_role`, `effort_bucket`, `file_scopes` populated (cross-checked against WaveSpec per C03 §5.4 if WaveSpec exists).
8. **Spike brief surfaced** (when matching `<date>-<P##|P##-I##|P##-I##-W##>-*.md` file exists under `.ea/local/research/`). The plan-mode proposal MUST cite the spike brief path.

**Case A — PLANNED phase, ≥1 PENDING wave.**

1. Render plan via `eawf roadmap show --phase <id> --md`.
2. Enter Claude Code plan mode (`EnterPlanMode`) with the rendered DAG (CC-runtime only; other runtimes use a text-prompt AUQ).
3. Surface AUQ with options `use-as-is / revise / replace / cancel`.
4. On `use-as-is`: call `eawf phase activate <id>` (V11 gate runs).
5. On `revise`: hand back to `/roadmap revise` (Edit Plan subagent §5.7).
6. On `replace`: hand back to `/roadmap drop` + `/roadmap propose`.
7. On `cancel`: status=ok with body recording the abandon.

**Case B — PLANNED phase, empty wave DAG.**

1. Dispatch the planner subagent (`build/eawf-plugin/agents/planner.md`). The planner returns either a sequence of `eawf roadmap revise --add-wave` commands or a YAML payload.
2. Surface AUQ with options `approve / edit / cancel`.
3. On `approve`: apply the planner's commands through the state CLI, then `eawf phase activate <id>`.
4. On `edit`: route through `/roadmap revise` with the planner's draft as input.
5. On `cancel`: status=ok with body recording the abandon.

**Case C — no PLANNED phase by that id.** Reject with exit 4 and hint `Run "eawf roadmap propose --phase <id> --title ..." first.`

**Optional spike first.** Before claiming a wave whose success criteria are not yet writable, the operator runs `/spike <slug>` (read-only). The spike produces a brief under `.ea/local/research/<YYYY-MM-DD>-<slug>.md`. When a matching brief exists, the plan-mode proposal in Case A MUST reference it by repo-relative path — the wave dispatch renderer surfaces matching briefs under `## References` automatically per [prep/SKILL.md:39-48].

**Wave dispatch.** Per [prep/SKILL.md:49-54]:

- Parallel waves under the activated iter → dispatch worktree subagents (one per wave; worktree branched from feature-HEAD).
- Sequential waves → run inline.
- Cherry-pick parallel-wave commits in between as they finish.
- Validate the rendered plan with `eawf plan show --md` — wave tags and bucket roll-ups must match state.

**Output.** `PrepBody` + envelope status `ok` (activation succeeded) or `needs_user` (plan-mode AUQ pending). Body carries: iter_id, objective, non-goals, DAG, waves with worktree policy, acceptance checks, baseline pointers, `plan_mode_approval: Literal["use-as-is", "revise", "replace", "planner-approve"]`.

**State mutations.** Activation: Phase PLANNED → ACTIVE; per-wave PENDING → CLAIMED on dispatch; per-wave Worktree row insert when worktree-isolated; AgentSession rows inserted for each subagent.

**Failure modes.**

| F# | Failure | Detection / mitigation |
|---|---|---|
| PREP-F1 | Phase doesn't exist | Case C — exit 4 with hint |
| PREP-F2 | Phase ACTIVE/CLOSED/ARCHIVED | status=failed; repair = `["use /prep on next PLANNED phase"]` |
| PREP-F3 | V11 gate fails (e.g. dep not CLOSED) | status=failed with the failing predicate named |
| PREP-F4 | Wave success_criteria missing | hard-reject at gate; repair = `["uv run eawf wave spec init <W##>"]` |
| PREP-F5 | Dirty worktree | warn (not block); operator must confirm via AUQ |
| PREP-F6 | Worktree subagent dispatch fails (subprocess error) | status=failed; repair = `["check daemon status / inspect worktree dir"]` |
| PREP-F7 | Branch out-of-date with main | branch-currency gate per AGENTS — repair = `["git fetch + rebase"]` |

**Session policy.** `fresh` (each phase activation is a discrete daemon transaction).

#### 5.4.6 /flow

**Purpose.** Composite controller running the workflow pipeline. Current implementation drives six core skills sequentially with per-step checkpoints + resume + drift detection. Per [flow/SKILL.md] and [skills/flow.py:1-103].

**Pipeline order.** Current `_CORE_FLOW_ORDER` at [flow.py:96-103]:

```python
_CORE_FLOW_ORDER = (
    ("/research", ResearchSkill),
    ("/prep", PrepSkill),
    ("/audit", AuditSkill),
    ("/ship", ShipSkill),
    ("/review", ReviewSkill),
    ("/polish", PolishSkill),
)
```

**C04 expansion (D10.b).** New 8-step `flow_order` (research-profile + UI-scope path):

```python
_FLOW_ORDER_FULL = (
    ("/spike", SpikeSkill),         # opt-in via --spike or profile-gated
    ("/research", ResearchSkill),
    ("/design", DesignSkill),        # opt-in via --design or research+UI-scope
    ("/prep", PrepSkill),
    ("/audit", AuditSkill),
    ("/ship", ShipSkill),
    ("/review", ReviewSkill),
    ("/polish", PolishSkill),
)
```

The flow runner consults the effective profile bundle (V3 [1:76-96]) to resolve which subset to run. Default for engineering-profile-only projects: research → prep → audit → ship → review → polish (current 6-step). Default for research + engineering: spike (opt-in) → research → design (opt-in) → ... (8-step). The runner accepts `--skip-spike` / `--skip-design` to manually narrow.

**Inputs.** `topic: str | None`, `stop_after: str | None` (one of skill names without leading `/`), `args_per_step: dict[str, dict] | None`, `resume_from: dict | None` (a `FlowCheckpointPayload` dict), `flow_id: str | None`.

**Short-circuit semantics.** After each step the runner inspects `env.header.status`. Anything other than `ok` triggers an immediate short-circuit per [flow.py:138-154]: the flow's terminal envelope inherits the failing step's `status` and `footer.repair_commands`.

**Checkpoints + resume.** Per [flow.py:492-541] each step boundary appends a `flow_checkpoint` envelope to `flow.jsonl` recording `(flow_id, step_index, step_name, started_at, completed_at, last_safe, payload_hash, parent_state_hash, parent_git_head, parent_profile_ids, args_per_step_hash)`. Resume reads the latest `last_safe=True` checkpoint and computes drift on four dimensions: state.json sha, git HEAD, profile id list, per-step args hash. Drift refuses with `INTEGRITY_VIOLATION` exit code per [flow.py:308-367].

**Output.** `FlowBody` envelope; body accumulates per-step envelopes plus the inter-stage gate decisions per [flow/SKILL.md:41-44]. Terminal status mirrors the last completed step (after any auto-accept or operator confirm). Per-step `flow_record` envelopes (start, terminal) emitted to `flow.jsonl` so `eawf flow status` / `--resume` can locate the active run.

**State mutations.** Each step's own mutations apply (per /research, /prep, etc.). Flow-level mutations: `flow.jsonl` appends + `event.jsonl` rows (`flow.start`, `flow.step_start`, `flow.step_end`, `flow.short_circuit`, `flow.stop_after`, `flow.resume_start`, `flow.resume_end`, `flow.end`).

**Failure modes.**

| F# | Failure | Detection / mitigation |
|---|---|---|
| FLOW-F1 | Mid-pipeline `needs_user` from a step | flow short-circuits; envelope status=needs_user; operator answers and re-runs `/flow --resume` |
| FLOW-F2 | Step raises (`failed`) | flow short-circuits; envelope status=failed; repair commands from the failing step propagated |
| FLOW-F3 | `--resume` against drift (state changed) | flow refuses with INTEGRITY_VIOLATION; body.drift populated; repair = `["sync state.json + rerun without --resume"]` |
| FLOW-F4 | Checkpoint write fails (disk full) | engine catches; status=failed |
| FLOW-F5 | `stop_after` unrecognised | flow runs full pipeline (current behaviour at [flow.py:121-135]); warning emitted |

**Session policy.** `fresh` per step — each step's `run_skill` builds a fresh `SkillContext`. The flow itself does not maintain a single session across the whole pipeline; per-step sessions accrue independently.

#### 5.4.7 /audit

**Purpose.** Fresh-context verification of a phase deliverable or wave outcome. Per [audit/SKILL.md] and [skills/audit.py].

**Inputs.** `scope_id: str` (phase/iter/wave), `kind: Literal["evaluation","ship-gate"] | None` (auto-detected from profile per [audit.py:84-94]), `checks: list[str] | None` (default per kind from [audit.py:55-69]).

**Kind branch (V3 application).** Per [audit.py:84-94]:

- `research` profile enabled → default kind `evaluation` (MLflow integrity, outcome measurements, hypothesis verdicts, evaluation artefact).
- `research` profile not enabled → default kind `ship-gate` (tests, lint, typecheck, build, security, docs links, scope drift).
- Operator override: `--kind evaluation|ship-gate`.

**Default checks per kind.**

| Kind | Default checks |
|---|---|
| `evaluation` | mlflow_integrity, lookahead_bias, is_oos_gap, outcome_measure, hypothesis_verdict |
| `ship-gate` | tests, lint, type, build, docs, state |

C03 §5.7 adds `verify_implements` as the seventh ship-gate check, fired on phase close — walks every closed-wave WaveSpec, greps `file_scopes` for verdict markers, returns per-wave-id "missing markers" details on failure [3:588-712].

**Algorithm.** Per [audit.py:106-225]: (1) probe, (2) resolve scope, (3) branch on profile, (4) build check plan from acceptance.yaml + profile rules + changed files, (5) run deterministic checks, (6) collect metrics, (7) dispatch fresh reviewers (v0.1 skipped), (8) mark findings (blocker / fix-now / follow-up / false-positive), (9) `--fix-safe` applies bounded fixes (v0.1 skipped), (10) write Audit artefact, (11) update outcomes/hypotheses from audit evidence.

**Reviewer subagent dispatch.** Per [audit/SKILL.md:22-25] the auditor MUST NOT have access to the parent conversation. C04 makes this explicit: every `/audit` reviewer subagent dispatched is a fresh-context subprocess (CC `Task` with no context inheritance, or equivalent per runtime). The reviewer reads only the diff + the WaveSpec + the acceptance criteria.

**Output.** `AuditBody` + envelope status:
- `ok` — every check passed.
- `partial` — checks ran but verdict is `MINOR` or `pass-with-followups`.
- `failed` — verdict is `MAJOR` or a hard check raised.
- `needs_user` — `pass-with-followups` requires operator disposition (open backlog / open wave / defer) per [audit/SKILL.md:28-29].

Body carries: scope_id, kind, checks_run (list of CheckResult), outcomes_measured, hypothesis_verdicts (with `confirm/reject/inconclusive` per row), findings (severity-tagged), audit_artifact_urn.

**State mutations.** Insert Audit row in `state.audits`; update Hypothesis rows with `verdict` when audit kind is `evaluation`; insert Incident rows when finding is severity blocker. Audit row carries `report_artifact_id` URN pointing at the audit's markdown artifact.

**Audit-DSL extension.** Per C03 §5.6-5.7 the audit-DSL kind catalog is now {`file_exists`, `path_glob_nonempty`, `regex_in_file`, `state_field_equals`, `command_exit_zero`, `verify_implements`}. Future kinds register through a Python entry-point at `eawf.audit_dsl.kinds.<kind>`. C04 fixes the registration API:

```python
# src/eawf/audit_dsl/registry.py (extension API — C04)

CheckKind = Literal[
    "file_exists",
    "path_glob_nonempty",
    "regex_in_file",
    "state_field_equals",
    "command_exit_zero",
    "verify_implements",
]

CHECK_REGISTRY: dict[CheckKind, Callable[[CheckSpec, Path], CheckResult]] = {
    "file_exists": check_file_exists,
    "path_glob_nonempty": check_path_glob_nonempty,
    "regex_in_file": check_regex_in_file,
    "state_field_equals": check_state_field_equals,
    "command_exit_zero": check_command_exit_zero,
    "verify_implements": check_verify_implements,    # C03 §5.7
}
```

Adding a check kind: implement the `(CheckSpec, Path) → CheckResult` function, register it in `CHECK_REGISTRY` via a profile-contributed Python entry-point, extend the `CheckKind` Literal. Schema migration: bump `schema_version` on AuditSpec to a new Literal value.

**Failure modes.**

| F# | Failure | Detection / mitigation |
|---|---|---|
| AUD-F1 | Acceptance file missing | status=blocked; repair = `["uv run eawf audit init <scope>"]` |
| AUD-F2 | A registered check raises | engine catches; check fails with `passed=False`, `details=<traceback>` |
| AUD-F3 | verify_implements: missing markers | per-wave-id "missing markers" rows in body.findings; verdict=MAJOR |
| AUD-F4 | Mismatched scope (phase audit on a wave URN) | validator rejects at start; repair = `["pass phase URN: urn:eawf:v1:phase:..."]` |
| AUD-F5 | Reviewer subagent dispatch fails | check marked as `skipped` with reason; aggregate verdict = `pass-with-followups` for the missing reviewer |

**Session policy.** `fresh` — fresh-context verification is the whole point.

#### 5.4.8 /ship

**Purpose.** Close out a phase by running local CI, opening the phase PR, advancing state. Per [ship/SKILL.md] and [skills/ship.py].

**Inputs.** `phase_id: str | None`, `commit: bool=False`, `push: bool=False`, `pr: Literal["open","ready","draft","close","none"] | None`, `artifact_paths: list[Path]=[]`, `pr_body: str | None`, `dry_run: bool=False`.

**Algorithm.** Per [ship/SKILL.md:11-19] + [skills/ship.py:96-275]:

1. Resolve `phase_id`; verify all waves under it are complete.
2. Audit-pass gate (current `ship.audit_gate` event at [ship.py:148-154]) — when audit-required, the gate consults `state.audits[*].verdict` for the phase URN; latest audit MUST be PASS or PASS_WITH_FOLLOWUPS.
3. Inspect git status / diff / log.
4. Memory review — extract durable lessons; promote useful entries per [ship.py:166-174].
5. Build pending-ship artefact (commit groups, messages, files, evidence).
6. Commit gate (default ask; `--commit` opts in).
7. Push gate (default ask; `--push` opts in).
8. PR action: open draft/ready, update body, close/merge.
9. Merge / close gates: CI green, required reviews, state valid.
10. Record commits / PR / merge / audit artefacts and final estimate-vs-actual.
11. Remove clean worktrees per policy.

**Artifact validation.** Per [ship.py:111-145] every artifact under `--artifact-paths` is validated via `validate_markdown_artifact` (chassis sections, citation density, scrub status); the `--pr-body` is validated via `validate_text_surface(text, surface="pr")`. Validation failure → `status=failed` with repair commands listing the validation errors.

**PR auth + auto-push gate.** Per [ship/SKILL.md:29-34] `gh pr create`, `gh pr merge`, and any push to a protected branch are irreversible. C04 mandates an AUQ confirm (`proceed / defer / abort`) unless `vcs.auto_push`, `vcs.pr_open`, and the merge strategy are pre-resolved by config.

**PR merge strategy.** Per AGENTS feedback memory: rebase, never squash. C04 makes this explicit: `/ship` defaults to `gh pr merge --rebase` for phase PRs. Squash destroys the `[P##-W##]` / `[P##-CORE]` commit-prefix history.

**Output.** `ShipBody` envelope; body carries commit_groups, push, pr, estimate_vs_actual, rollback_notes.

**State mutations.** Cherry-pick wave commits onto feature branch → set `Wave.commit` SHA. Push feature branch → set `Phase.pr_url` (when `--pr open`). On merge → `Phase.status` ACTIVE → CLOSED. Memory promotions per [ship.py:166-174].

**Failure modes.**

| F# | Failure | Detection / mitigation |
|---|---|---|
| SHIP-F1 | Some waves under phase still PENDING/CLAIMED/IN_PROGRESS | status=failed; repair = `["close pending waves before /ship"]` |
| SHIP-F2 | Audit-gate fails | status=failed; repair = `["/audit <P##> --kind ship-gate"]` |
| SHIP-F3 | Artifact validation fails | status=failed; repair = `["fix artifact validation errors and rerun /ship"]` |
| SHIP-F4 | `gh pr create` fails (auth, network) | status=failed; repair = `["gh auth status / gh auth refresh"]` |
| SHIP-F5 | `gh pr merge` fails (CI red) | status=needs_user with `wait / retry / abort` options |
| SHIP-F6 | Cherry-pick conflict | status=failed; repair = `["resolve conflicts in main worktree, rerun /ship"]` |
| SHIP-F7 | Phase CLOSED but `Wave.commit` not all set | rollback path; emit Incident; status=failed |

**Session policy.** `fresh` — ship operations are one-shot.

#### 5.4.9 /review

**Purpose.** Code review of an open PR or local diff. Surfaces issues with severity tags; no scope creep, no praise. Per [review/SKILL.md] and [skills/review.py].

**Inputs.** `pr: str | None`, `base: str = "main"`, `head: str = "HEAD"`, `recommendation: Literal["approve","comment","request_changes","fix_locally"] = "comment"`, `post: bool = False`.

**Algorithm.** Per [review.py:77-178]: resolve PR (explicit flag or active branch) → fetch metadata → triple-dot diff → dispatch focused agents by area / risk → check template completeness, state links, audit evidence, drift, tests → produce findings table + recommendation → post if `--post`.

**Findings severity tags.** 🔴 blocker | 🟠 must-fix | 🟡 should-fix | 🔵 nit per [review/SKILL.md:18-19]. Findings list grouped by file per [review/SKILL.md:36-38].

**Reviewer subagent dispatch.** When the diff is large or spans multiple areas, `/review` dispatches focused subagents per area (security, performance, correctness, docs). Each subagent reads only the diff + a narrow context window; never the parent conversation. Findings aggregate at the parent skill.

**Output.** `ReviewBody` envelope; body carries pr_url, base, head, findings (flat list grouped by file), recommendation, posted.

**State mutations.** Append `event.jsonl` rows; on `--post` emit a `reviewer_report` store row per AGENTS rule 19. No `state.json` mutation.

**Failure modes.**

| F# | Failure | Detection / mitigation |
|---|---|---|
| REV-F1 | PR not found (`gh pr view` fails) | status=failed; repair = `["pass --pr <url> explicitly"]` |
| REV-F2 | Diff empty | status=ok with body.findings=[]; warning emitted |
| REV-F3 | Reviewer subagent fails | log + degrade — surviving findings still aggregate |
| REV-F4 | `--post` against closed PR | status=failed; repair = `["reopen PR before --post"]` |

**Session policy.** `fresh` — reviewer must not carry parent context.

#### 5.4.10 /polish

**Purpose.** Whole-repo consistency audit + cleanup. Aligns naming, docstring style, log fields, error message phrasing; removes dead code. Per [polish/SKILL.md] and [skills/polish.py].

**Inputs.** `scope: str | None` (default = entire `src/eawf/`), `report_only: bool = True`, `max_fixes: int | None`, `y: bool = False` (inverts report_only).

**Algorithm.** Per [polish/SKILL.md:11-21] + [polish.py:65-172]: (1) probe, (2) snapshot repo / state, (3) fan out read-only agents over code/tests/docs/configs/state/memory, (4) find inconsistencies (stale docs, duplicate rules, broken links), (5) reconcile / merge findings into grouped cleanup tables, (6) memory pass (promote useful entries; mark stale; propose prune list), (7) without `-y` ask which groups to run, (8) with `-y` apply safe groups only, (9) run affected checks; write polish report artefact, (10) state updates record decisions / backlog / memory changes.

**Safe vs unsafe groups.** Safe = formatting, comment phrasing, repo-internal name alignment. Unsafe = public API renames, dead-code deletions, schema-level changes. Public-API renames + dead-code deletions MUST be raised via AUQ even with `-y` per [polish/SKILL.md:26-30].

**Output.** `PolishBody` envelope; body carries groups (one per topic/scope/risk with items), memory_pass (promotions/prunes/compactions), report_only.

**State mutations.** Memory tier promote/prune per C01 D6 [2:480-484]. Backlog inserts when items deferred. Decision rows when polish changes architecture (e.g. dead-code deletion = "deprecated module" decision).

**Failure modes.**

| F# | Failure | Detection / mitigation |
|---|---|---|
| POL-F1 | Public-API rename without `-y` | status=needs_user with AUQ `apply / defer-to-backlog / skip` |
| POL-F2 | Dead-code deletion against uncommitted file | reject per AGENTS deletion rule [AGENTS §"Deletion rule"]; emit warning |
| POL-F3 | Memory prune of an ARCHIVAL-tier row | warn; require explicit `--prune-archival` flag |
| POL-F4 | `--scope` path doesn't exist | status=failed |

**Session policy.** `fresh` — polish runs are bounded; no benefit to continuation.

#### 5.4.11 /init

**Purpose.** Initialise a new Eä Workflow workspace via the install wizard. Per [init/SKILL.md] and [skills/init.py].

**Inputs.** `answers: WizardAnswers` (dict matching `WizardAnswers` field set; required for happy path), `output_dir: Path = cwd` (renamed from `target_dir` 2026-05-18 per BOT-06), `force: bool = False`.

**Required answer keys.** Per [init.py:59-68]: `state_path`, `project_code`, `project_title`, `lifecycle_depth`, `profiles`, `runtime`.

**Algorithm.** Per [init.py:128-272]:

1. Detect current state.
2. If any required answer key missing → `status=needs_user` with UserQuestion offering `provide_answers / run_interactive / cancel`.
3. Build `WizardAnswers` Pydantic instance from `answers` dict (raises `InvalidInput` if schema mismatch).
4. Call `run_wizard_no_input(answers, output_dir, force)` per [install/wizard.py] (parameter renamed `target_dir` → `output_dir` 2026-05-18 per D-07 / BOT-06).
5. Translate `WizardResult` into `InitBody.steps`.

**Output.** `InitBody` envelope; body carries project_code, workspace_root, profile_ids, steps (one per managed file: state_json, config_yaml, agents_md, manifest, claude_md, optional materialise_state_keys).

**State mutations.** Writes `.ea/state.json`, `.ea/config.yaml`, `.ea/profile.yaml`, AGENTS.md managed regions, CLAUDE.md (workspace-local), per-runtime plugin tree.

**Failure modes.**

| F# | Failure | Detection / mitigation |
|---|---|---|
| INIT-F1 | Missing required answer | status=needs_user with the missing key list |
| INIT-F2 | Invalid answer (schema mismatch) | `InvalidInput` raised; status=failed |
| INIT-F3 | Target dir not empty + no `--force` | wizard rejects; repair = `["pass --force to overwrite or pick empty dir"]` |
| INIT-F4 | Profile composition conflicts | wizard rejects with the conflict declaration; repair = `["reduce profiles: list to non-conflicting set"]` |

**Session policy.** `fresh` — init is one-shot; resume not meaningful.

#### 5.4.12 /differentiate

**Purpose.** Generate project-specialised agent definitions (per source comments at [differentiate.py:1-3]). The SKILL.md frontmatter at [differentiate/SKILL.md:3] currently says "recommend cheapest experiment" — that's the OLD purpose, superseded. **C04 corrects this**: the manifest description must match the Python source's intent.

**Inputs.** `preset: Literal["minimal","adaptive","full"]="adaptive"`, `approval: Literal["ask","auto"]="auto"`, `runtime: Literal["claude","codex","opencode","all"]="all"`.

**Algorithm.** Per [differentiate.py:1-33] + [differentiate.py:101-220]: (1) probe, (2) resolve scope, (3) inspect existing eawf agents + profile agents + runtime agents + languages/frameworks/architecture/tests/docs/recurring-work-types, (4) propose desired agent set (roles, count, runtime targets, model/tool permissions, memory policy, worktree policy, naming), (5) AUQ — minimal/adaptive/full, read-only vs writer agents, replace or extend existing, (6) draft agent definitions by adapting eawf baselines, (7) validate with `/agent-lint` (v0.1 skipped), (8) render approved agents (v0.1 skipped — degrade pattern).

**Output.** `DifferentiateBody` envelope; body carries target_scope, axes (per-axis current + peers + advantage), conclusions, optional user_question.

**State mutations.** v0.1 skipped. C04 lands the agent-rendering pipeline as part of P21+: writes per-runtime agent .md files under `build/<runtime>-plugin/agents/` (C07-owned mechanics). Each generated agent goes through `state.json` Agent row insert (TBD in C01 — currently Agent is not a top-level entity but it should be; flagged as Open Question in §8).

**Failure modes.**

| F# | Failure | Detection / mitigation |
|---|---|---|
| DIF-F1 | Preset unknown | fallback to `adaptive` per [differentiate.py:120-122]; warning |
| DIF-F2 | Approval gate; no operator response | status=needs_user with `approve / edit / replace_existing / cancel` |
| DIF-F3 | No existing baselines to adapt | status=failed; repair = `["run /init first to seed baseline agents"]` |
| DIF-F4 | Render skipped (v0.1 limitation) | warning emitted; body carries proposed set even when not rendered |

**Session policy.** `fresh` — each generation is independent.

#### 5.4.13 /blitz

**Purpose.** Auto-chained research follow-up with recursion guard for residual unknowns. Per [blitz/SKILL.md] and [skills/blitz.py].

**Inputs.** `residual_unknowns: int=0`, `followup_research_args: dict={}`.

**Algorithm.** Per [blitz.py:121-159]: (1) read residual unknown count, (2) increment recursion guard (`EAWF_BLITZ_DEPTH_COUNTER` against `EAWF_BLITZ_DEPTH` default 8), (3) emit a follow-up `/research` action with `blitz=false` so the next research pass doesn't recurse.

**Trigger heuristic.** `should_auto_invoke(residual_unknowns)` returns True when count > 1 per [blitz.py:107-109]. `/research` consults this at end of probe pass per [research.py:301-302].

**Output.** `BlitzBody` envelope; body carries depth, depth_cap, residual_unknowns, followup_research_args, next_actions.

**State mutations.** `event.jsonl` rows only.

**Failure modes.**

| F# | Failure | Detection / mitigation |
|---|---|---|
| BLZ-F1 | Depth cap exceeded | `BlitzRecursionExhaustedError` per [blitz.py:97-102]; status=blocked; repair = `["reduce residual_unknowns or disable blitz"]` |
| BLZ-F2 | `EAWF_BLITZ_DEPTH` env unparseable | default 8 with warning per [blitz.py:55-65] |
| BLZ-F3 | Auto-invoked outside `/research` | works but pointless; warning emitted |

**Session policy.** inherits caller — `/blitz` is a continuation of the parent `/research` invocation.

### 5.5 Skill orchestration sequence diagrams

Five representative flows. Each diagram shows the cross-skill dispatch chain, the envelope status transitions, and the daemon-side persistence per V1 [1:24-53].

#### 5.5.1 /flow happy path (6-step pipeline)

```
operator                  /flow                  /research            /prep         /audit         /ship          /review         /polish        daemon
   │                        │                       │                    │              │              │              │              │             │
   │── /flow topic="X" ─────►│                      │                    │              │              │              │              │             │
   │                        │── append flow_record(start, status=IN_PROGRESS) ───────────────────────────────────────────────────────────────────────►│
   │                        │── snapshot drift ─────────────────────────────────────────────────────────────────────────────────────────────────────► daemon
   │                        │── run_skill ──────────►│                    │              │              │              │              │             │
   │                        │                       │── probe ────────────────────────────────────────────────────────────────────────────────────►│
   │                        │                       │── action (10 steps) ────────────────────────────────────────────────────────────────────────►│
   │                        │                       │── append event rows ────────────────────────────────────────────────────────────────────────►│
   │                        │                       │── return ResearchBody(status=ok) ───►│
   │                        │── append checkpoint(step=/research, last_safe=True) ──────────────────────────────────────────────────────────────────►│
   │                        │── run_skill ────────────────────────────────►│              │              │              │              │             │
   │                        │                                              │── activate phase + dispatch waves ──────────────────────────────────────►│
   │                        │                                              │── return PrepBody(status=ok) ──►│
   │                        │── append checkpoint(step=/prep) ───────────────────────────────────────────────────────────────────────────────────────►│
   │                        │── run_skill ───────────────────────────────────────────────►│              │              │              │             │
   │                        │                                                              │── run checks + reviewers ──────────────────────────────►│
   │                        │                                                              │── return AuditBody(status=ok) ►│
   │                        │── checkpoint /audit, /ship, /review, /polish — each ok ──────────────────────────────────────────────────────────────────►│
   │                        │── append flow_record(terminal, status=DONE) ────────────────────────────────────────────────────────────────────────────►│
   │── FlowBody(status=ok) ─◄│                       │                    │              │              │              │              │             │
```

#### 5.5.2 /flow short-circuit on needs_user

```
operator         /flow            /research          /prep         /audit         /ship       daemon
   │               │                  │                  │              │             │           │
   │── /flow ──────►│                 │                  │              │             │           │
   │               │── /research ─────►│                 │              │             │           │
   │               │                  │── depth=deep ────►│              │             │           │
   │               │                  │── return ResearchBody(status=needs_user, body.user_question) ──►│
   │               │── append checkpoint(last_safe=False — needs_user is not safe) ─────────────►│
   │               │── short-circuit at /research ───────────────────────────────────────────►│
   │               │── append flow_record(terminal, status=PAUSED, last_safe_checkpoint=<prior>) ──►│
   │── FlowBody(status=needs_user, body.steps[0].user_question) ──◄│
   │                                                                                              │
operator answers via runtime AUQ → /flow --resume <flow-id> ──────────────────────────────►│
   │               │                                                                              │
   │               │── load_latest_safe_checkpoint(flow_id) ────────────────────────────────────►│
   │               │── compute_drift(ckpt, state) → None (no drift) ─────────────────────────────►│
   │               │── re-run /research with ctx.args["__user_choice__"]=<answer> ────────────►│
   │               │── continues from step_index = checkpoint.step_index + 1 ────────────────────►│
```

#### 5.5.3 /roadmap propose → plan-mode preview

```
operator                  /roadmap                CC runtime        daemon            state.json
   │                        │                       │                  │                 │
   │── /roadmap propose --phase P21 --from-briefs <s>─►│                │                 │
   │                        │── parse args ──────────►│                  │                 │
   │                        │── validate from-briefs paths exist ─────────────────────────►│
   │                        │── render PhaseSpec stub from briefs + AUQ for body fields ──►│
   │                        │── write PhaseSpec stub at .ea/specs/P21/spec.md ────────────►│
   │                        │── write Phase row PLANNED + Iter P21-I01 PLANNED ──►│        │
   │                        │── emit envelope status=needs_user with plan_text + 4-option AUQ ──►│
   │                        │── EnterPlanMode(plan_text) ───►│                              │
   │                        │                       │── show plan-mode preview ────────────►│
   │                        │── AskUserQuestion(approve / edit / research_more / defer) ─►│
   │── pick: approve ───────────────────────────────────────►│                              │
   │                        │◄── operator choice=approve ────│                              │
   │                        │── return envelope(status=ok, body.user_choice="approve") ──►│
   │                        │                                                              │
   │── /roadmap apply P21 ──►│                                                              │
   │                        │── V11 hard gate check ─────────────────────────────────────►│
   │                        │── return envelope(status=ok) ───►│                            │
   │                        │── handoff to /prep ───►│                                      │
```

#### 5.5.4 /prep Case A — plan-mode preview + activate

```
operator        /prep              CC runtime       daemon         worktree-subagent(s)
   │              │                   │               │                 │
   │── /prep P21 ──►│                  │               │                 │
   │              │── resolve P21 ──────────────────►│                  │
   │              │── status=PLANNED, ≥1 wave PENDING ─────────────────►│
   │              │── render plan-mode preview (PhaseSpec + IterSpec + WaveSpec aggregate) ──►│
   │              │── EnterPlanMode(plan_text) ───►│                    │
   │              │── AskUserQuestion(use-as-is / revise / replace / cancel) ──►│
   │── pick: use-as-is ──────────────────────►│                          │
   │              │◄── operator choice ───────│                          │
   │              │── eawf phase activate P21 ─────────────────────────►│
   │              │── V11 hard gate passes ───────────────────────────►│
   │              │── Phase PLANNED → ACTIVE ─────────────────────────►│
   │              │── for each PENDING wave: dispatch worktree subagent ──►│
   │              │                                                       ├── claim wave; branch from feature HEAD
   │              │                                                       ├── run wave per WaveSpec
   │              │                                                       └── return agent_end report; commit on worktree branch
   │              │── return envelope(status=ok, body.plan_mode_approval="use-as-is", body.dispatched_waves=[W01,...])
   │              │                                                       │
   │── (later) cherry-pick worktree commits ──────────►│                  │
```

#### 5.5.5 /audit ship-gate with verify_implements check

```
operator       /audit                 daemon         CheckSpec runner    state.json    spec files
   │             │                       │               │                  │              │
   │── /audit P20 --kind ship-gate ──►│                  │                  │              │
   │             │── resolve P20 ────────────────────►│                    │              │
   │             │── load acceptance.yaml + AuditSpec for P20 ────────────►│              │
   │             │── for each check in {tests, lint, type, build, docs, state, verify_implements}: dispatch
   │             │   ├── tests ──────►│ ── uv run pytest ──►│                                  │
   │             │   ├── lint ───────►│ ── uv run ruff check ──►│                              │
   │             │   ├── ...                                                                  │
   │             │   └── verify_implements ──►│ ── load WaveSpec for each closed wave under P20 ──►│
   │             │                            │                                                  │
   │             │                            │── git diff main...HEAD --name-only ─────────────►│
   │             │                            │── for each WaveSpec: grep verdict markers in changed file_scopes
   │             │                            │── per-wave-id "missing markers" rows ──►│      │
   │             │── aggregate CheckResults ─────────────────────────────────────────────►│    │
   │             │── verdict: PASS or MAJOR (with missing markers in body.findings) ─►│        │
   │             │── insert Audit row in state.audits with verdict + check_results ─►│        │
   │             │── return envelope(status=ok|partial|failed) ──►│                            │
```

### 5.6 Plan-mode preview

The plan-mode preview is the operator's approval surface for `/roadmap propose` (and for `/prep` Case A). It renders the PhaseSpec aggregate (PhaseSpec + IterSpecs + WaveSpecs under it) into a single human-readable plan text, then surfaces a 3-button AUQ.

**Render source (D5.b).** The plan text is built from the spec aggregate:

```
# Phase P21 — <PhaseSpec.title>

## Outcome
<PhaseSpec.outcome>

## KPIs
<table from PhaseSpec.kpis>

## Ship criteria
<list from PhaseSpec.ship_criteria>

## Iters
### P21-I01 — <IterSpec.title>
**Sub-goal:** <IterSpec.sub_goal>
**Wave groups:** <IterSpec.wave_groups>

#### Waves
- **P21-I01-W01** — <WaveSpec.title>
  - **Implements:** <WaveSpec.implements>
  - **File scopes:** <WaveSpec.file_scopes>
  - **Behaviors:** <WaveSpec.behaviors>
  - **Tests:** <WaveSpec.tests>
  - **Effort:** <Wave.effort_bucket>
- **P21-I01-W02** — ...

### P21-I02 — <IterSpec.title>
...
```

**AUQ shape.**

```python
UserQuestion(
    question="Plan for P21 ready (<N> waves under <M> iters). Pick how to proceed.",
    options=[
        UserQuestionOption(label="approve", description="Apply the plan as proposed (runs `eawf roadmap apply P21`)."),
        UserQuestionOption(label="edit", description="Spawn the Edit Plan subagent with operator feedback (§5.7)."),
        UserQuestionOption(label="reject", description="Drop the proposed plan (runs `eawf roadmap drop P21`)."),
    ],
)
```

The fourth slot (capacity 4) is reserved for `research_more` when the operator wants `/research` re-run before deciding. On AUQ for runtime not supporting more than 2 options, the runtime adapter falls back to `y/N` with reject mapped to `N`.

**Plan-mode runtime adapter.**

- **Claude Code:** `EnterPlanMode(plan_text)` per [P20-DIR]. The plan-mode preview is rendered inside CC's plan-mode pane; AUQ surfaces as CC's `AskUserQuestion` widget. On Approve, CC exits plan mode and calls `eawf roadmap apply <P##>`.
- **Codex CLI:** plan-text is printed to stdout; AUQ falls back to text-prompt `[A]pprove / [E]dit / [R]eject`. No plan-mode pane.
- **OpenCode:** TBD per C07 runtime catalog.
- **eawf skill run CLI:** plan-text printed; AUQ surfaces as questionary-backed picker.

**Spec mutation atomicity.** The PhaseSpec stub written by `/roadmap propose` is committed via daemon transaction per V1. The plan-mode preview reads the committed spec; Edit Plan subagent re-writes the spec via the daemon; Apply re-reads + validates. No spec file is read mid-write.

### 5.7 Edit Plan subagent

The Edit Plan subagent is dispatched when the operator picks Edit on the plan-mode preview AUQ.

**Subagent prompt template (D6.b).** Self-contained per AGENTS §"Agent tool discipline":

```
You are editing the PhaseSpec + IterSpecs + WaveSpecs for phase <P##>.

Operator feedback: "<free-text feedback>"

Current spec aggregate:
<PhaseSpec body rendered>
<IterSpec bodies rendered>
<WaveSpec bodies rendered>

Constraints:
- Every WaveSpec MUST cite ≥1 verdict per [3:402] (WSV-01).
- Every WaveSpec MUST have ≥1 failure_mode per [3:425] (WSV-02).
- Every WaveSpec MUST have ≥1 behavior per [3:426] (WSV-03).
- Every WaveSpec.tests entry MUST point at a real test path (WSV-05).
- UI-scope waves (file_scopes under src/eawf/tui_v2/ or src/eawf/render/) MUST have mockup or mockup_waiver_reason (WSV-07).
- Phase P## MUST stay in PLANNED status — do not mutate ACTIVE/CLOSED phases.

Allowed actions:
- `uv run eawf phase spec edit P## --outcome "..."`
- `uv run eawf phase spec edit P## --add-kpi metric=<m> target=<t> direction=<d>`
- `uv run eawf phase spec edit P## --add-ship-criterion id=<id> text="<t>"`
- `uv run eawf iter spec edit P##-I## --sub-goal "..."`
- `uv run eawf iter spec edit P##-I## --add-wave-group label=<l> wave_ids=<ids> rationale="<r>"`
- `uv run eawf wave spec edit P##-I##-W## --add-behavior id=<id> text="<t>"`
- `uv run eawf wave spec edit P##-I##-W## --add-failure-mode "<text>"`
- `uv run eawf wave spec edit P##-I##-W## --add-implements V##:<brief>:<line>`
- `uv run eawf roadmap revise P## --add-wave <W##> --title "<t>"`
- `uv run eawf roadmap revise P## --remove-wave <W##>`
- `uv run eawf roadmap revise P## --set-deps <W##>=<dep_ids>`

Output contract:
- Emit `agent_end` report (typed AgentReportBody per AGENTS rule 19) with:
  - verdict: pass | pass-with-followups | fail | blocked
  - body.summary: one-line description of changes made
  - body.evidence_refs: spec URNs that were edited
  - body.followups: any deferred issues
```

**Subagent return.** The subagent emits the `agent_end` report; the parent `/roadmap revise` skill re-renders the plan-mode preview and re-surfaces the AUQ. Loop continues until operator chooses Approve or Reject.

**Edit Plan flow.**

```
operator                       /roadmap revise           subagent           daemon              state.json + specs
   │                              │                         │                  │                   │
   │── Edit Plan picked from plan-mode AUQ ──►│             │                  │                   │
   │                              │── render current spec aggregate ───────────────────────────►│
   │                              │── AUQ: "edit feedback" (free-text input) ──►│                  │
   │── feedback string ──────────►│                          │                  │                   │
   │                              │── Task(prompt=Edit Plan subagent template + feedback + specs)─►│
   │                              │                          │── invoke eawf phase spec edit ──►│
   │                              │                          │── invoke eawf iter spec edit ───►│
   │                              │                          │── invoke eawf wave spec edit ───►│
   │                              │                          │── emit agent_end report ────────►│
   │                              │── re-render plan-mode preview ─────────────────────────────────►│
   │── new plan-mode preview ────►│                                                                  │
   │                              │── AUQ: approve / edit / reject ──►│                              │
```

**Failure modes.**

| F# | Failure | Detection / mitigation |
|---|---|---|
| EDIT-F1 | Subagent emits no commands | re-issue with explicit instruction "use eawf spec edit verbs" |
| EDIT-F2 | Subagent mutates ACTIVE phase | rule-20 invariant blocks; surface in parent as repair |
| EDIT-F3 | Subagent's verdict citation invalid | C03 WSV-10 validator rejects; subagent re-tries |
| EDIT-F4 | Edit Plan loop > 5 iterations without convergence | force operator AUQ "give up / continue / revert" |

### 5.8 Skill registry + sync

The skill registry is the source-of-truth catalog of available skills. Layered discovery resolves three tiers per [discovery.py:181-219]; plugin sync projects the registry to per-runtime native plugin trees per V9 [1:289-294].

**Layered discovery.**

```
Workspace .ea/skills/<name>/SKILL.md
  ↓ (highest precedence)
User <local-path>
  ↓
Builtin SKILL_REGISTRY (Python-registered Skill subclasses)
  ↓
Resolved DiscoveredSkill list
```

A skill name appearing at multiple tiers resolves to the highest-precedence entry; lower-tier overlays are silently dropped. Invalid frontmatter (workspace/user tiers) is logged at WARNING and skipped per [discovery.py:188].

**Profile gating (V3).** Each `DiscoveredSkill` is filtered by `SkillManifest.profiles.requires` / `profiles.forbids` against the project's enabled profile bundle. A skill whose `requires` includes `research` is only visible when `research` is in `profiles.enabled`. Plugin sync re-runs the filter on every AGENTS.md or `.ea/config.yaml` edit.

**Plugin sync verb (C07-owned mechanics).** `eawf plugin sync [--runtime <name>] [--profile <name>]` regenerates per-runtime native plugin contents from:

- AGENTS.md (canonical rule source).
- SkillManifests (Python-registered + workspace/user SKILL.md overlays).
- C07 runtime adapter shape catalog (per-runtime native plugin layout).

Sync output:

- `build/eawf-plugin/skills/<name>/SKILL.md` (CC native shape).
- `build/codex-plugin/...` (Codex native shape — per C07).
- `build/opencode-plugin/...` (OpenCode native shape — per C07).

The committed `build/<runtime>-plugin/` trees are the canonical sources; per-machine install paths (`.claude/`, `<local-path>`, …) are generated by sync and gitignored.

**Plugin doctor verb (C07-owned).** `eawf plugin doctor` walks each runtime's install path and flags drift vs the canonical source: file count delta, file hash mismatch, skill registration delta, hook count delta. Never auto-fix; report-only.

**Skill disable.** Operators disable a skill by:

- Adding `name: /<skill>` to `.ea/config.yaml:skills.disabled` list (project-scope).
- OR adding it to `<local-path>` (user-scope).

Next `eawf plugin sync` rewrites per-runtime trees with the skill removed. The Python class stays registered (so `eawf skill run /<name>` still works for diagnostics with `--force-disabled`), but no runtime adapter surfaces it.

## 6. Failure modes + named edge cases

Cross-skill failure modes that don't fit a single skill subsection.

| F# | Failure mode | Detection / mitigation |
|---|---|---|
| GLOBAL-F1 | Skill not found (operator typo) | runtime adapter rejects with "did-you-mean" hint listing top-3 closest names |
| GLOBAL-F2 | Skill registered but Python class fails to import (broken third-party) | engine catches at registration; logs WARNING; skill listed as `failed` in `eawf skill list` |
| GLOBAL-F3 | Two SKILL.md overlays declare same name | discovery silently drops lower-precedence per [discovery.py:195]; operator sees only the higher-tier entry — needs `eawf skill show <name> --all-tiers` to debug |
| GLOBAL-F4 | Manifest field doesn't round-trip to a runtime's plugin shape | plugin sync emits warning; manifest MUST declare `runtime: [<list>]` to filter; doctor flags drift |
| GLOBAL-F5 | `dispatch.session_policy: continue` but runtime doesn't support `--continue` | runtime adapter falls back to `fresh` with `Wave.dispatch_history` annotation per V8 [1:268-269] |
| GLOBAL-F6 | needs_user pause persists beyond session lifetime | resume mechanism reads the pause envelope from `event.jsonl`; drift checks before resume; operator gets clear error on stale pause |
| GLOBAL-F7 | Skill `removed_in` reached + skill still present in project config | next sync removes skill from plugin tree; CLI `eawf skill run /<removed>` emits `removed in <version>; use /<replacement>` and exits 5 |
| GLOBAL-F8 | Profile gate mismatch (skill requires profile that's not enabled) | discovery filters skill out; CLI `eawf skill run` rejects with `enable profile <p> in .ea/config.yaml first` |
| GLOBAL-F9 | Skill body shape mismatch (envelope round-trip breaks) | EV-07 byte-stable round-trip test fails; engine emits `failed` with `["body shape changed — verify SkillManifest.body_model"]` |
| GLOBAL-F10 | Edit Plan subagent mutates non-spec files | parent skill rejects the agent_end report; surface "subagent strayed scope" in operator AUQ |
| GLOBAL-F11 | `/flow` resume after profile change | drift check `parent_profile_ids != current_profile_ids` raises INTEGRITY_VIOLATION; repair = `["restart flow without --resume"]` |
| GLOBAL-F12 | `/flow` mid-pipeline skill version bump | drift check via `payload_hash` per [flow.py:273-277]; refuse resume |
| GLOBAL-F13 | Skill version semver invalid (e.g. `"v1.0"` with leading `v`) | manifest validator at SkillManifest.version pattern rejects; sync fails fast |
| GLOBAL-F14 | Workspace overlay SKILL.md frontmatter has unknown field | discovery skips with WARNING per [discovery.py:188]; operator sees skill missing from list — `eawf skill check` surfaces the parse error |
| GLOBAL-F15 | Skill emits envelope with `status=ok` but body shape rejected | strict validator rejects; engine maps to `failed` with traceback body |
| GLOBAL-F16 | Multiple `needs_user` envelopes queued for same scope/session | daemon enforces single-active needs_user per (scope, session); subsequent pauses queued; operator sees them in order |
| GLOBAL-F17 | `/spike` `--final` write but `.ea/local/research/` doesn't exist | mkdir parents=True per current convention; warning if creation succeeds — drives the `.ea/local/` directory into existence |
| GLOBAL-F18 | `/design` referenced from PhaseSpec but artifact missing | C03 §5.6 validator rejects PhaseSpec validate; `/prep` activate-gate flags missing reference |
| GLOBAL-F19 | Plan-mode preview text exceeds runtime's plan-mode budget (CC: ~30 KB) | render compactly; collapse WaveSpec details to titles; full body via `eawf phase spec render <P##>` |
| GLOBAL-F20 | Runtime adapter strips manifest extras (e.g. session_policy) | round-trip test detects; sync re-writes the HTML-comment block; doctor flags lost field |

## 7. Migration plan

C04 lands the contracts; the implementation lands across multiple phases. Each phase below names the wave-level deliverables.

### 7.1 P21 — Skill contract freeze + envelope discriminator

- **Wave 1**: SkillManifest Pydantic schema + per-builtin manifest registered alongside Python class. No behavior change; manifest co-exists with current `version: str = "1.0"` field.
- **Wave 2**: Add `kind: Literal[...]` discriminator field to every body model (additive — defaults to body class name; existing envelopes round-trip with the new field set).
- **Wave 3**: Strict validator EV-01..EV-10 implementation; emit warnings rather than rejecting in the first pass; flip to hard-reject after one alpha.

### 7.2 P22 — /spike + /design first-class skills

- **Wave 1**: SpikeSkill + SpikeBody + SKILL.md migrated from `.claude/skills/spike/SKILL.md` to `build/eawf-plugin/skills/spike/` + `src/eawf/skills/spike.py`.
- **Wave 2**: DesignSkill + DesignBody + design lint (L1-L11) port.
- **Wave 3**: `/flow_order` extended to include /spike + /design; default-off; opt-in via `--spike` / `--design` flags or profile-gated when research-profile + UI-scope detected.

### 7.3 P23 — dispatch.session_policy round-trip

- **Wave 1**: SkillDispatch sub-model lands; default `hybrid`. Per-profile defaults `continue` (research) and `fresh` (engineering) configured in profile manifests.
- **Wave 2**: Daemon-side per-(wave, attempt) session-handle table per V8 [1:266-271]; per-runtime adapter exposes `open_session() / continue_session(id) / session_path(id)` per C07 §5.3.
- **Wave 3**: Plugin sync rewrites SKILL.md frontmatter HTML-comment block carrying `dispatch.session_policy`; doctor flags drift.

### 7.4 P24 — Plan-mode preview + Edit Plan subagent

- **Wave 1**: Plan-mode render-from-spec-aggregate implementation; `/roadmap propose` emits envelope with `body.plan_text` populated from PhaseSpec + IterSpec + WaveSpec aggregate.
- **Wave 2**: EnterPlanMode CC runtime adapter wiring; AUQ 3-button (approve / edit / reject) shape.
- **Wave 3**: Edit Plan subagent prompt template + Task dispatch + agent_end report parsing + plan re-render loop.

### 7.5 P25 — needs_user resume infrastructure

- **Wave 1**: `needs_user_pause` envelope appended to `event.jsonl` per (skill, scope, session) pause.
- **Wave 2**: `eawf skill resume <pause-urn>` CLI verb (C05-owned but skill-mediated); daemon-side drift check + replay.
- **Wave 3**: `/flow` resume integration — currently resumes from `last_safe=True` checkpoint; extend to resume from `last_safe=False` (a needs_user pause) when operator has answered.

### 7.6 P26 — Skill versioning + deprecation

- **Wave 1**: SkillDeprecation Pydantic schema; CLI surfaces `eawf skill list --deprecated`.
- **Wave 2**: Runtime adapter emits deprecation banner on dispatch of a `deprecated_since` skill.
- **Wave 3**: `removed_in` check at sync; CLI exits 5 on `eawf skill run /<removed>`.

### 7.7 Schema migration story

Every body model carries `schema_version: int` per current convention. Bumping the version triggers a Pydantic `model_validator` that rewrites old payloads in-memory at load time (forward-only; never backward). The wire-form envelope's `header.skill` stays stable; only the body schema shifts. Existing `event.jsonl` entries reload via the bump-aware validator; failed loads emit a WARNING and skip the row (audit replay degrades gracefully).

## 8. Open questions for operator

Each question carries an AUQ seed list. Ratification ratifies the decisions; operator may override.

### Q1 — Envelope `status` enum freeze

```
question: "Freeze the envelope status enum at the current 5 values (ok | needs_user | blocked | failed | partial)?"
options:
  - approve_d1a: "Yes — freeze current 5 values (Recommended)"
  - add_cancelled: "Add `cancelled` for operator-aborted runs"
  - collapse_blocked_failed: "Collapse `blocked` + `failed` into one"
```

### Q2 — Skill invocation surface

```
question: "Skills surface on both `eawf skill run <name>` and runtime-native `/<name>`?"
options:
  - approve_d2c: "Yes — both surfaces (Recommended)"
  - cli_only: "CLI `eawf skill run` only"
  - runtime_only: "Runtime-native `/<name>` only"
```

### Q3 — Skill registry storage

```
question: "Skill registry uses both Python entry-points (builtin) and SKILL.md frontmatter (workspace/user overlay)?"
options:
  - approve_d3c: "Yes — both (Recommended)"
  - yaml_only: "SKILL.md YAML frontmatter only"
  - python_only: "Python entry-points only"
```

### Q4 — needs_user context storage

```
question: "Store needs_user pause context as `needs_user_pause` envelope appended to event.jsonl (+ flow_checkpoint for /flow)?"
options:
  - approve_d4c: "Yes — event.jsonl + flow.jsonl (Recommended)"
  - inline_body: "Inline in envelope body — lossy across long pauses"
  - separate_file: "Per-(skill,scope,session) checkpoint file under .ea/local/skills/checkpoints/"
```

### Q5 — Plan-mode preview render source

```
question: "Plan-mode preview rendered from PhaseSpec + IterSpec + WaveSpec aggregate?"
options:
  - approve_d5b: "Yes — spec aggregate (Recommended)"
  - state_only: "From state.phases / state.iters / state.waves only"
  - agent_draft: "Free-text agent draft"
```

### Q6 — Edit Plan subagent prompt shape

```
question: "Edit Plan subagent receives structured prompt with PhaseSpec body + feedback + verdict-citation constraints?"
options:
  - approve_d6b: "Yes — structured prompt (Recommended)"
  - one_shot: "One-shot prompt: 'edit this plan to address: <feedback>'"
  - multi_round: "Multi-round AUQ inside subagent"
```

### Q7 — Per-profile dispatch.session_policy default

```
question: "research-profile defaults to `continue`, engineering-profile defaults to `fresh`, cross-profile defaults to `hybrid`?"
options:
  - approve_d7b: "Yes — per-profile defaults (Recommended)"
  - all_hybrid: "All profiles default `hybrid`"
  - no_profile_gate: "Per-skill manifest field with no profile-gating"
```

### Q8 — Skill versioning policy

```
question: "Skill versioning uses semver + optional deprecated_since / removed_in markers?"
options:
  - approve_d8b: "Yes — semver + deprecation markers (Recommended)"
  - semver_only: "Semver only — no deprecation markers"
  - schema_version_only: "Numeric schema_version: int only"
```

### Q9 — Skill registry visibility scope

```
question: "Three-tier discovery (workspace → user → builtin) with `runtime: [<list>]` filter per skill?"
options:
  - approve_d9b: "Yes — three-tier + runtime filter (Recommended)"
  - single_tier: "Builtin only"
  - four_tier: "Four-tier (workspace → repo → user → builtin)"
```

### Q10 — /flow expansion

```
question: "/flow includes optional /spike + /design steps (8-step total) gated on flags or profile?"
options:
  - approve_d10b: "Yes — 8-step with opt-in spike + design (Recommended)"
  - keep_6_step: "Keep current 6-step order"
  - per_profile_config: "Per-profile-configurable flow step list"
```

### Q11 — /spike + /design implementation phase

```
question: "Land /spike + /design first-class skills in P22 (next phase after P21 skill-contract-freeze)?"
options:
  - approve_p22: "P22 (Recommended)"
  - defer_p23: "Defer to P23"
  - hold_p21: "Land in P21 alongside contract freeze"
```

### Q12 — Skill deprecation sunset window

```
question: "Minimum gap between `deprecated_since` and `removed_in` is 3 minor versions (alpha cadence)?"
options:
  - approve_3_min: "Yes — 3 minor versions (Recommended)"
  - 2_min: "2 minor versions"
  - 6_min: "6 minor versions"
```

### Q13 — /differentiate manifest description mismatch fix

```
question: "Fix /differentiate SKILL.md description to match Python source intent (agent-set generation, not differentiation-experiment)?"
options:
  - approve_fix: "Yes — rewrite SKILL.md description (Recommended)"
  - rename_skill: "Rename Python class to match SKILL.md (no, breaks too many callers)"
  - keep_both: "Keep both meanings; clarify in body markdown"
```

### Q14 — `agent` URN kind status

```
question: "Promote `Agent` to a first-class state entity (own URN kind, Pydantic row, lifecycle DAG) when /differentiate v0.5 lands?"
options:
  - approve_agent_kind: "Yes — agent becomes a state entity (Recommended)"
  - file_only: "Agent definitions stay file-only under build/<runtime>-plugin/agents/"
  - defer_v0_6: "Defer to v0.6+"
```

### Q15 — Manifest extras passthrough on Claude Code

```
question: "Manifest extras (dispatch.session_policy, body_model, …) stored in `<!-- eawf:manifest-extra ... -->` HTML comment block in SKILL.md?"
options:
  - approve_html_comment: "Yes — HTML comment block (Recommended)"
  - separate_file: "Separate manifest.json sibling file"
  - frontmatter_extras: "Add an `eawf:` namespaced block to SKILL.md frontmatter"
```

## 9. References

[1] `.ea/local/research/long-term/2026-05-16-c00-spec-index.md` — spec architecture index; V1-V9 locked verdicts.
[2] `.ea/local/research/long-term/2026-05-16-c01-foundations.md` — vocabulary, URN scheme, entity catalog, lifecycle DAGs.
[3] `.ea/local/research/long-term/2026-05-16-c03-spec-infrastructure.md` — PhaseSpec, IterSpec, WaveSpec, audit-DSL extensions.
[4] `src/eawf/skills/engine.py` — Skill ABC + run_skill orchestrator (frozen Phase 4 W01).
[5] `src/eawf/skills/registry.py` — register / lookup / list_registered API.
[6] `src/eawf/skills/discovery.py` — three-tier layered discovery (workspace → user → builtin).
[7] `src/eawf/render/envelope.py` — frozen EnvelopeHeader / Footer / OutputEnvelope at [envelope.py:48-178].
[8] `src/eawf/skills/_common.py` — probe_skill_instruments, emit_event, has_research_profile, resolve_active_state_path.
[9] `src/eawf/skills/_bootstrap.py` — import-side-effect registration for all builtin skills.
[10] `src/eawf/skills/bodies/__init__.py` — typed body models per skill.
[11] `.claude/skills/spike/SKILL.md` — /spike first-class skill definition; multi-round AUQ + postmortem arm + PoC manifest + 11-rule chassis.
[12] `.claude/skills/design/SKILL.md` — /design first-class skill definition; six-category event taxonomy + L1-L11 lint contract.
[13] `.ea/local/research/2026-05-15-experimental-spike-workflow.md` — spike-fanout iter pattern; convention-only; primitives already exist.
[14] `.ea/local/research/archive/2026-05-11-skill-updates-research-blitz.md` — /research --local + --depth + /blitz proposal.
[15] `.ea/local/research/archive/2026-05-16-design-subframework-proposal.md` — /design subframework with operator-confirmed verdict.
[16] `src/eawf/skills/research.py` — current /research implementation; depth-gated question slots; blitz auto-invoke.
[17] `src/eawf/skills/roadmap.py` — current /roadmap; approval-gated needs_user envelope; horizon-scaled candidates.
[18] `src/eawf/skills/prep.py` — current /prep; 10-step algorithm; approval gate.
[19] `src/eawf/skills/flow.py` — current /flow; 6-step pipeline; checkpoints; drift detection; resume.
[20] `src/eawf/skills/audit.py` — current /audit; profile-branched evaluation vs ship-gate; check plan.
[21] `src/eawf/skills/ship.py` — current /ship; artifact validation; commit / push / PR gates.
[22] `src/eawf/skills/review.py` — current /review; severity tags; triple-dot diff; reviewer dispatch.
[23] `src/eawf/skills/polish.py` — current /polish; group/risk taxonomy; safe-vs-unsafe gating.
[24] `src/eawf/skills/init.py` — current /init; questionary wizard delegate; missing-answers needs_user.
[25] `src/eawf/skills/differentiate.py` — current /differentiate; preset-scaled axes; agent-set generation intent.
[26] `src/eawf/skills/blitz.py` — current /blitz; recursion guard; should_auto_invoke threshold.
[27] `build/eawf-plugin/skills/*/SKILL.md` — per-skill SKILL.md frontmatter for 11 builtin skills (current shape).
[28] `src/eawf/skills/bodies/user_question.py` — UserQuestion + UserQuestionOption Pydantic models; 2-4 option validator.
[29] `AGENTS.md §"Roadmap procedure"` — canonical /research → /roadmap propose → /roadmap revise → /roadmap apply → /prep flow.
[30] `AGENTS.md §"Spike workflow"` — spike convention; `<YYYY-MM-DD>-<slug>.md` under `.ea/local/research/`; chained dispatch via `next:` line.
[31] `AGENTS.md §"Planned-scope revisability"` — PLANNED freely mutable; ACTIVE append-only at phase; PENDING-only at wave under ACTIVE.
[32] `AGENTS.md §"Agent tool discipline"` — subagents not parent-context; self-contained prompts.
[33] `AGENTS.md §"Naming conventions"` — scope_id / output_dir / wave= log-key form; mutator-path precision.

## 10. Provenance

- `store_record=none (local-only research)`
- `commit=3b86f7a (parent at session start)`
- `supersedes=none`
- `session=eawf-c04-workflow-skills-2026-05-16`
- `depends_on=[C01-accepted, C03-accepted]`
- `consumed_by=[C05, C06, C10]`
- `cluster_target_loc=~1200`
- `actual_loc≈1230`
- `verdicts_seeded=D1..D10 + Q1..Q15 (operator ratifies via AUQ)`

## 11. Scrub

- status: clean
- references: repo-relative paths and external citation tokens only.
- local paths: none.
- real emails: none.
- abstract placeholder names: P21, P22, … are forward-looking phase ids; no PII; no host-local URLs.
- runtime tokens: `claude`, `codex`, `opencode` per V9 catalog — no proprietary vendor names beyond the framework's declared runtime set.
