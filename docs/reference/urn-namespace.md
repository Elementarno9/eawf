# URN namespace

*Qualified `eawf://` entity URNs and the RFC 8141 informal/private namespace `eawf` for durable cross-state references.*

Eä uses URNs as persistent, location-independent names for durable workflow objects. Two forms are in use: the qualified `eawf://` URN addresses the lifecycle entities (Track, Milestone, Batch, Task, Run and their neighbours), and the RFC 8141 `urn:eawf:v1:` URN addresses store records, artifacts, blobs and version-control objects.

## Qualified entity URNs

```text
eawf://<workspace-key>/<project-key>/<repository-key-or-_>/<entity-kind>/<entity-key>[#<fragment>]
```

- The repository slot is never omitted. A workspace- or project-level entity (a release, a claim) takes the reserved `_`; a repository-level entity (a Milestone, a Batch, a Task, a Run) names its repository. `ENTITY_SCOPES` in `src/eawf/kernel/identity/keys.py` decides which slot addresses each kind.
- The entity kind is one of the `EntityKind` values in `src/eawf/kernel/identity/keys.py`, and it must agree with the kind of reference that carries the URN, so a field that promises a Milestone cannot hold a Batch.
- The entity key follows its kind's grammar: a Task is `<PROJECT>-####`, a Run `RUN-` with eight digits, a Milestone `MLS-####`, a Batch `BAT-####`, a Track `TRK-<symbol>`, a release `REL-<version>`.
- The only fragment is `#rung-<n>` on a claim URN, selecting evidence rung 1 to 4.
- A re-keyed tree spells the same address with one key in every container slot: `eawf://<project-key>/<entity-kind>/<entity-key>`.

```text
eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/milestone/MLS-0030
eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0007
eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042
eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010
eawf://WSP-MAIN/PRJ-EAWF/_/release/REL-0.7.0
eawf://WSP-MAIN/PRJ-EAWF/_/claim/CLM-0004#rung-2
eawf://EAWF/task/EAWF-0042
```

The parser is `parse_qualified_urn` in `src/eawf/kernel/identity/urn.py`.

## Record URNs

```text
urn:eawf:v1:<kind>:<owner>[/<id-or-path>][?=<query>][#<fragment>]
```

### Rules

- **NID**: `eawf`.
- **Version**: `v1` immediately after NID in the NSS.
- **Kind** is lowercase ASCII, one of `URN_KINDS` in `src/eawf/kernel/state/urn.py`: `workspace`, `repo`, `artifact`, `store`, `blob`, `memory`, `report`, `hypothesis`, `decision`, `audit`, `pr`, `commit`, `branch`, `secret` and the other catalogued kinds. The catalog also keeps the kinds of the retired epoch-1 lifecycle so an imported record's URN still parses.
- **Codes** are uppercase where human-defined: `EAWF`, `QR`, `AO-SERVER`.
- **Assigned-name** (`urn:eawf:...` before `?=` or `#`) is the identity. Query / fragment are view / selection hints and ignored for identity comparison.
- Percent-encode unsafe characters; keep the canonical stored form ASCII.
- Do not store ordinary URLs or absolute file paths as Eä URNs. Put them in artifact metadata fields (`url`, `local_path`) when needed.

### Core record URNs

```text
urn:eawf:v1:workspace:TM
urn:eawf:v1:repo:QR
urn:eawf:v1:repo:QR/configs/strategies/collar/v1.yaml
urn:eawf:v1:artifact:QR/ART-20260506-audit
urn:eawf:v1:store:QR/memory/MEM-20260506-001
urn:eawf:v1:store:QR/executor_report/AR-executor-QR-0042-01
urn:eawf:v1:blob:QR/sha256/2f1a...
urn:eawf:v1:pr:QR/42
urn:eawf:v1:commit:QR/abc1234
urn:eawf:v1:branch:QR/feature/qr-v0.7
urn:eawf:v1:secret:FRED_API_KEY
```

## Resolution

- A qualified `eawf://` URN resolves to the row of its kind's collection in the selected generation document, or in that collection's ledger once the record reached a terminal status.
- `workspace` resolves through the workspace registry.
- `repo` with only owner resolves to the repo root; `repo` with path resolves to repo root + relative path.
- `store` resolves to JSONL record under owner `.ea/store/<kind>.jsonl` (singular `store/` directory; per-kind file named after the singular `StoreKind` value).
- `artifact` resolves through the artifact index to durable evidence / output metadata.
- `blob` resolves through content-addressed blob store for immutable large payloads.
- `pr` is project-scoped (`urn:eawf:v1:pr:QR/42`) and resolves through that repo's VCS provider config (`github`, `gitlab`, `forgejo`, `bitbucket`, etc.) and available git / CLI / API instruments.

## Store roots

```text
repo-local:       <repo>/.ea/store/
workspace-level:  <workspace>/.ea/store/
local/private:    <repo>/.ea/local/store/ or <workspace>/.ea/local/store/
```

## `store` vs `artifact` vs `blob`

- **`store`** is structured record storage: memory, incident, audit, research, decision, event, and typed agent reports. Example: `urn:eawf:v1:store:QR/audit/AUD-0012` resolves to one JSONL record. Agent report store URNs include the role-specific store kind before the report id, for example `urn:eawf:v1:store:QR/reviewer_report/AR-reviewer-QR-0042-01`.
- **`artifact`** is durable output / evidence metadata: report, screenshot set, HTML, data manifest, generated markdown, PR body, promoted agent report. Example: `urn:eawf:v1:artifact:QR/ART-20260506-audit` resolves through the artifact index.
- **`blob`** is immutable content-addressed payload storage for large / noisy bytes: logs, command outputs, screenshots, HTML reports, transcript excerpts. Artifacts and store records point to blobs by hash for dedupe and integrity.

Stores are queryable records; artifacts are workflow evidence objects; blobs are raw immutable payloads.

## Query and fragment components

On a record URN, use `?=` for non-identity view / render parameters and `#` for a named section, row, cell, or subview inside the resolved resource. They MUST NOT change the referenced entity identity.

Good uses:

```text
urn:eawf:v1:artifact:QR/ART-20260506-audit?=format=markdown#failures
urn:eawf:v1:store:QR/memory/MEM-20260506-001#summary
urn:eawf:v1:repo:QR/docs/ROADMAP.md#milestone-30
```

View / format examples:

- `?=format=json`: raw machine output for agents / CI.
- `?=format=table`: compact terminal table for humans.
- `?=format=markdown`: PR / comment / report-friendly rendered section.
- `?=format=text`: plain fallback for non-TTY / logs.
- `?=view=dashboard`: small TUI / status pane summary.
- `?=view=trace`: event / evidence chain for a scope.
- `?=view=summary`: short one-screen view.
- `?=view=detail`: expanded view with linked evidence.

Rules:

- `?=` allowed keys: `view`, `format`, `rev`, `as_of`, `limit`, `section`.
- `format` controls representation shape; `view` controls selected content / layout.
- `#` names a client-side fragment after resolution: Markdown heading slug, JSON pointer alias, table section, or Eä-defined subview like `#summary`, `#evidence`, `#failures`, `#risks`.
- State should usually persist assigned-name only. Query / fragment are best for CLI / TUI links, PR comments, review notes, and human navigation.
- Equality and dedupe ignore query / fragment; `urn:eawf:v1:artifact:QR/ART-20260506-audit#summary` and `...#risks` point to the same record with different views.

## Agent usage guidance

Agents should use URNs when referring to durable workflow objects in outputs, evidence, memory, PR bodies, and audit findings. URN references survive file moves, resolve across workspace / repo boundaries, and let `eawf why` navigate directly.

Use URNs for:

- lifecycle entities: Track, Milestone, Batch, Task, Run, Release, Hypothesis, Decision,
- artifacts and JSONL store records,
- typed agent report rows, for example `urn:eawf:v1:store:QR/operator_report/AR-operator-MLS-0030-01`,
- commits, branches, PRs,
- evidence links in `/verify`, `/integrate` and `/accept` outputs,
- durable memory entries.

Do not overuse URNs for casual prose or obvious nearby files. For code references, keep normal `path:line` for developer navigation and add URN only when the reference becomes durable evidence.

CLI unification rule: every command option that refers to a durable scope, entity, artifact, store record, branch, commit, PR, or secret should accept canonical URNs. Short keys (`MLS-0030`, `QR-0042`, `ART-...`) are convenience input only; `eawf` resolves them and persists canonical URNs in the document, stores and artifacts.

Migration note: old shorthand refs like `repo:QR` may be accepted in CLI input, but state should persist canonical URNs.

## Cross-references

- State entities and key grammar — `docs/architecture/state-model.md`.
- Sources: `src/eawf/kernel/identity/urn.py` (qualified URNs) and `src/eawf/kernel/state/urn.py` (record URNs).
