# State model

*Normalized, strict-Pydantic ledger for projects, workspaces, and lifecycle entities.*

## Workspace-level state and repo sub-states

Default mode for `eawf init` inside a repo: **repo-owned `.ea/state.json`
plus workspace index link when a workspace exists**. Every initialized
repo remains portable and commit-ready, while a parent workspace can
orchestrate multiple repos through an index/rollup.

Direction:

```text
workspace/.ea/state.json  ──links──>  repo/.ea/state.json
```

Workspace state SHOULD link to repo states; repos do not link back.

### Modes

1. **Repo-owned state + workspace index link** (recommended default):

   ```text
   workspace/.ea/state.json    # workspace ledger + repo registry + cross-repo views
   repo/.ea/state.json         # repo-local ledger, committed with repo if not ignored
   ```

   Workspace stores links of the form:

   ```json
   {
     "repos": {
       "QR": {
         "path": "<absolute-repo-path>",
         "state_urn": "urn:eawf:v1:repo:QR",
         "project_code": "QR",
         "title": "Quant Research",
         "status": "active"
       }
     }
   }
   ```

   Repo-project relation is 1-to-1. `project_code` is the short symbolic
   name used in human-facing refs, commits, statusline, and
   `urn:eawf:v1:repo:<code>` references.

2. **Repo standalone mode**: only `repo/.ea/state.json`. Used for OSS
   repos or repos cloned outside a workspace.

3. **Workspace-only mode** (discouraged): all state in
   `workspace/.ea/state.json`; repo loses portability and cannot work
   fully after standalone clone.

4. **Repo sub-state mode**: repo state coordinated by workspace state for
   cross-repo roadmap with repo-local execution details.

### State resolution order

```text
1. EA_STATE env var, if set
2. explicit --workspace / -w: nearest parent .ea/state.json with scope_kind: workspace
3. current repo .ea/state.json with scope_kind: repo
4. nearest parent .ea/state.json only when current dir is not inside a repo with repo-owned state
5. error with suggestion: `eawf init` or `eawf workspace init`
```

Invariants:

- Every state file has `scope_kind`: `workspace` or `repo`.
- Every entity has an owning state file.
- Workspace may reference repo entities by URN
  (e.g. `urn:eawf:v1:state:QR/P13-I04-W01`).
- Workspace cannot mutate repo-owned entities unless the command
  explicitly targets the repo and takes the repo lock.
- Repo cannot mutate workspace-owned entities unless `-w` /
  `--workspace` is explicit.
- `eawf validate --strict` validates the active state only.
- `eawf workspace validate --strict` validates workspace + linked repos.

## Hierarchy and orthogonal entities

```text
Project
  └─ Subproject
      └─ Phase
          └─ Iter
              └─ Wave
```

Orthogonal (cross-cutting) entities: Goal, Outcome, Hypothesis, Decision,
Audit, Incident, Artifact, AgentSession, BacklogItem.

## Top-level state fields

All Pydantic v2 models use `extra="forbid"`. IDs are immutable strings.
Timestamps are UTC ISO-8601. URNs use `urn:eawf:v1:*`. Lifecycle IDs
use two-digit padding by default (`P01`, `P01-I01`, `P01-I01-W01`).

| Field | Type | Required | Notes |
|---|---|---:|---|
| `schema_version` | literal `"1.0"` | yes | current schema |
| `scope_kind` | `repo` \| `workspace` | yes | workspace detection marker |
| `urn` | urn | yes | state owner URN |
| `updated_at` | datetime | yes | last mutation |
| `project` | Project or null | yes | repo states require project |
| `current` | CurrentPointers | yes | active pointers |
| `workspace` | WorkspaceIndex or null | yes | workspace states require workspace; null on repo states |
| `health` | enum `ok` \| `needs_setup` \| `degraded` or null | no | populated by `eawf doctor` / `validate` when applicable |
| `subprojects` | map[id, Subproject] | no | materialized when multi-workstream depth chosen |
| `goals` | map[id, Goal] | no | materialized once first goal defined |
| `outcomes` | map[id, Outcome] | no | profile-gated (research / quant) |
| `phases` | map[id, Phase] | yes | lifecycle |
| `iters` | map[id, Iter] | yes | lifecycle |
| `waves` | map[id, Wave] | yes | lifecycle |
| `estimates` | map[id, EstimateSummary] | no | materialized once estimation enabled |
| `actuals` | map[id, ActualSummary] | no | materialized once estimation enabled |
| `hypotheses` | map[id, Hypothesis] | no | research profile materializes as `{}` |
| `audits` | map[id, Audit] | no | research profile materializes as `{}` |
| `incidents` | map[id, Incident] | no | materialized on first `eawf incident open` |
| `artifacts` | map[id, Artifact] | yes | artifact index |
| `decisions` | map[id, Decision] | no | materialized on first decision |
| `backlog` | map[id, BacklogItem] | no | materialized on first backlog item |
| `agent_sessions` | map[id, AgentSession] | yes | work provenance |
| `worktrees` | map[id, WorktreeRecord] | no | materialized once worktree policy enables them |
| `mcp_servers` | map[id, McpServer] | no | Eä-owned MCP config refs (config-gated) |
| `plugins` | map[id, PluginInstall] | yes | runtime integration refs (always materialized after `/init`) |
| `memory_index` | map[id, MemorySummary] | no | materialized on first memory entry |
| `indexes` | object | yes | derived lookup caches |

`eawf validate --strict` ignores absent optional keys. Once a key
materializes (any record written), its schema is enforced. A profile
that requires a key (e.g., `research` requires `hypotheses` + `audits`)
materializes the key as `{}` on profile composition, even before the
first record. Adding a profile later
(`eawf config profile enable <name>`) materializes any newly-required
keys as `{}` during the next `eawf sync`.

## Core records

| Entity | Key fields | Store / detail location | Notes |
|---|---|---|---|
| Project | `code`, `slug`, `title`, `description`, `domains`, `default_branch`, `status`, `repo_urn` | state | code matches `^[A-Z][A-Z0-9_-]{1,15}$` |
| WorkspaceIndex | `code`, `title`, `repos`, `current_repo_code` | state | repo paths absolute, local/workspace-owned |
| WorkspaceRepoRef | `code`, `path`, `state_urn`, `project_code`, `title`, `status` | state | path absolute by default |
| CurrentPointers | `project_code`, `subproject_id`, `phase_id`, `iter_id`, `active_wave_ids`, `active_session_ids` | state | nullable IDs |
| Subproject | `id`, `code`, `slug`, `title`, `kind`, `domains`, `status`, `owner`, `goal_ids` | state | code unique; same regex as Project |
| Goal | `id`, `scope_id`, `title`, `summary`, `status`, `outcome_ids`, `created_at`, `closed_at` | state | status enum |
| Outcome | `id`, `scope_id`, `metric`, `threshold`, `direction`, `value`, `status`, `audit_id`, `updated_at` | state | status `pending` \| `met` \| `missed` \| `waived` |
| Phase | `id`, `scope_id`, `title`, `status`, `iter_ids`, `outcome_ids`, `opened_at`, `closed_at`, `audit_id` | state | no close with open children |
| Iter | `id`, `phase_id`, `title`, `status`, `wave_ids`, `estimate_id`, `audit_id`, `opened_at`, `closed_at` | state | parent inferred by ID |
| Wave | `id`, `iter_id`, `title`, `status`, `deps`, `file_scopes`, `success_criteria`, `agent_role`, `effort_bucket`, `claim_session_id`, `worktree_id`, `commit`, `outcome`, `opened_at`, `closed_at` | state | atomic execution unit |
| Hypothesis | `id`, `scope_id`, `text`, `metric`, `confirm`, `reject`, `status`, `verdict`, `audit_id`, `source_artifact_id` | state + optional artifact | thresholds concrete |
| Audit | `id`, `scope_id`, `kind`, `status`, `report_artifact_id`, `check_results`, `integrity_results`, `created_at`, `verdict` | state + `audit.jsonl` | evidence only |
| Artifact | `id`, `kind`, `uri`, `local_path`, `urn`, `sha256`, `size_bytes`, `created_at`, `metadata` | state / index | hash for files / blobs |
| Decision | `id`, `scope_id`, `summary`, `rationale`, `alternatives`, `status`, `created_at`, `superseded_by` | state + `decision.jsonl` | rationale required |
| BacklogItem | `id`, `scope_id`, `title`, `priority`, `status`, `created_at`, `closed_at`, `resolution`, `commit` | state | priority enum |
| EstimateSummary | `id`, `scope_id`, `expected_eu`, `pessimistic_eu`, `confidence`, `current_store_record_id`, `updated_at` | state + `estimate.jsonl` | history in store |
| ActualSummary | `id`, `scope_id`, `status`, `elapsed_eu`, `attention_eu`, `agent_runtime_eu`, `current_store_record_id`, `updated_at` | state + `actual.jsonl` | segments in store |
| AgentSession | `id`, `role`, `runtime`, `scope_id`, `status`, `claimed_wave_ids`, `worktree_ids`, `artifact_ids`, `started_at`, `ended_at`, `summary` | state + events | provenance |
| WorktreeRecord | `id`, `wave_id`, `branch`, `path`, `base_branch`, `status`, `owner_session_id`, `created_at`, `merged_commit` | state | git-owned path |
| McpServer | `id`, `owner`, `command`, `args`, `env_refs`, `risk`, `write_capable`, `status`, `installed_targets` | state / config | `owner` must be `eawf` for managed servers |
| PluginInstall | `id`, `runtime`, `scope`, `target_path`, `status`, `managed_files`, `installed_at`, `updated_at` | state / config | Claude only v0.1 |
| MemorySummary | `id`, `scope_id`, `summary`, `confidence`, `status`, `store_record_id`, `review_due` | state + `memory.jsonl` | JSONL authoritative |
| Incident | `id`, `scope_id`, `severity`, `title`, `status`, `opened_at`, `closed_at`, `root_cause`, `corrective_action_ids`, `report_artifact_id` | state + `incident.jsonl` | severity enum `low` \| `medium` \| `high` \| `critical` |
| Flow | `id`, `goal`, `budgets`, `status`, `current_pointers`, `policy`, `last_safe_checkpoint`, `next_action`, `started_at`, `updated_at` | state pointer + `flow.jsonl` | status enum `pending` \| `in_progress` \| `paused` \| `blocked` \| `done` \| `abandoned` \| `superseded` |

## ID grammar

```text
Project:       <PROJECT_CODE>              # e.g. QR, EA
Subproject:    <SUBPROJECT_CODE>           # e.g. COLLAR, PLATFORM
Goal:          G<NNN> or <SUBPROJECT>-G<NNN>
Phase:         P<NN>
Iter:          P<NN>-I<NN>
Wave:          P<NN>-I<NN>-W<NN>
Hypothesis:    H<NN>-<NN> or <SUBPROJECT>-H<NN>-<NN>
Decision:      D<NNN>
Audit:         AUD-P<NN>[-I<NN>][-W<NN>][-<slug>]
Incident:      INC-YYYYMMDD-<slug>
Artifact:      ART-YYYYMMDD-<slug>
AgentSession:  SES-YYYYMMDDTHHMMSSZ-<role-or-shortid>
Estimate:      EST-<SCOPE_ID>             # e.g. EST-P13-I04-W01
Actual:        ACT-<SCOPE_ID>             # e.g. ACT-P13-I04-W01
Backlog:       B<NNN>
```

**Project / subproject code regex**: `^[A-Z][A-Z0-9_-]{1,15}$` —
uppercase ASCII first character, optional digits / underscore / hyphen,
total 2–16 characters. Validates `QR`, `EA`, `COLLAR`, `PLATFORM`,
`AO-SERVER`. Rejects `Q` (single char), `qr` (lowercase), `1Q`
(digit-leading).

All IDs are strings and immutable. Phase / iter / wave IDs encode
parentage; commands do not require redundant parent flags:
`eawf iter open P13-I04` implies `phase=P13`. Parent flags are only
accepted for auto-allocation: `eawf iter open P13` lets the allocator
choose the next `P13-Ixx`.

### Phase scoping: project vs subproject

Phases are **project-scoped by default**. A phase opened with
`eawf phase open P13` belongs to the project, not the current subproject
pointer; iters opened under it can rebind to any subproject via
`--subproject`.

Subproject-scoped phases require explicit opt-in:

```bash
eawf phase open P13 --subproject COLLAR --title "Collar volatility refit"
```

A subproject-scoped phase records `phases.P13.subproject_id = "COLLAR"`.
Child iters inherit the subproject unless explicitly overridden.
`eawf phase close P13` requires the subproject pointer to match (or
`--force` with an audit reason). When a subproject is added while a
project-scoped phase is already open, the open phase stays
project-scoped — it does not silently rebind.

## Estimation model

Eä includes an operator-time budget estimator. The estimator answers:
**how much active operator + agent session time should the user
budget?** It is not a delivery-date promise and not a replacement for
evidence.

```text
1 EU = 30 minutes active operator + agent session time
```

Clocks tracked separately:

- `elapsed_EU`: wall-clock session time after idle / break policy;
  primary budget metric.
- `attention_EU`: optional operator attention estimate; nullable / manual
  in v0.1.
- `agent_runtime_EU`: total agent / subagent runtime, including parallel
  work; useful for cost / load, not user waiting time.

Storage:

- `state.json` keeps the current estimate / actual summary per scope.
- `.ea/store/estimate.jsonl` stores all estimate versions and supersession
  links; `.ea/store/actual.jsonl` stores actual segments and recovery
  events.

Actuals are append-only segment records. Never overwrite prior measured
session time. If a session stops, the runtime crashes, or the PC powers
off, close the current segment as `interrupted` when the next Eä
process can observe it. Calibration uses `done` scopes only;
interrupted, blocked, abandoned, failed, and superseded scopes feed
risk / fallback statistics.

Estimation by scope: wave (direct, highest quality) → iter (rollup of
waves) → phase (rollup of iters). Wave plans may carry an
`effort_bucket` (`XS=0.25`, `S=0.5`, `M=1.0`, `L=2.0`, `XL=3.5` EU)
so plan renderers can show both `sum_wave_eu` and `critical_path_eu`.
Closed wave timestamps derive a provisional `actual_elapsed_eu` until
richer actual-segment instrumentation is present. Roadmap-level shows
directional envelopes only.

## Large entity handling

`state.json` stores **index fields and summaries**, not full long-form
documents. Long-form details (research briefs, plan specs, long
hypothesis rationale, full audit metric tables, incident timelines) live
as artifacts under `.ea/artifacts/` and are referenced by artifact ID +
URN. Markdown artifacts use the standard chassis: `Summary`,
`References`, `Provenance`, and `Scrub`. Local drafts under `.ea/local/`
carry an `eawf-template` sentinel and lose it on promotion.

Suggested thresholds:

- `title`: 120 chars
- `summary`: 500 chars
- `details`: 2,000 chars max inline, then artifact required
- metric tables, long logs, and transcripts: artifact required

## URN namespace

Implemented kinds (source of truth: `URN_KINDS` in
`src/eawf/state/urn.py`):

```text
urn:eawf:v1:workspace:<code>
urn:eawf:v1:repo:<code>
urn:eawf:v1:state:<owner>/<scope-id>
urn:eawf:v1:artifact:<owner>/<id>
urn:eawf:v1:store:<owner>/<kind>/<id>
urn:eawf:v1:blob:<owner>/sha256/<hash>
urn:eawf:v1:pr:<owner>/<number>
urn:eawf:v1:commit:<owner>/<sha>
urn:eawf:v1:branch:<owner>/<name>
urn:eawf:v1:secret:<NAME>
```

See `docs/reference/urn-namespace.md` for the full URN specification
(rules, query / fragment components, agent usage guidance).

## Validation invariants

- Every child parent ID exists and matches encoded parentage.
- Current pointers reference open / active entities or null.
- Closed phases / iters have no open children.
- Verdict / outcome updates reference an audit.
- Artifact file refs exist or are explicitly external.
- Store refs resolve to a JSONL record with matching ID / kind / scope.
- Eä-managed MCP / plugin records never overwrite non-Eä owner entries.
- Auto-ID allocation happens while holding the state sibling lockfile.
- Secret / env refs use `${ENV:NAME}` and never store values.
- Checkpoint commit required when command declares a checkpoint
  boundary.

## Cross-references

- Enums (full canonical list) — see `docs/reference/enums.md`.
- JSONL store record envelope, event payload, config schema sections,
  lockfile semantics — see `docs/reference/lockfile-semantics.md` and
  `docs/architecture/envelope.md`.
- URN format details — see `docs/reference/urn-namespace.md`.
- Authoritative-mutator policy and the state CLI — see
  `docs/architecture/cli-surface.md`.
