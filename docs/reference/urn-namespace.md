# URN namespace

*RFC 8141 informal/private namespace `eawf` for durable cross-state references.*

Eä uses URNs (RFC 8141) as persistent, location-independent names for
durable workflow objects. Eä uses informal / private namespace `eawf`
and encodes schema version in the namespace-specific string.

## Format

```text
urn:eawf:v1:<kind>:<owner>[/<id-or-path>][?=<query>][#<fragment>]
```

## Rules

- **NID**: `eawf`.
- **Version**: `v1` immediately after NID in the NSS.
- **Kind** is lowercase ASCII: `workspace`, `repo`, `state`,
  `artifact`, `store`, `blob`, `pr`, `commit`, `branch`, `secret`.
  Source of truth: `URN_KINDS` in `src/eawf/state/urn.py`.
- **Codes** are uppercase where human-defined: `TM`, `QR`, `COLLAR`.
- **Assigned-name** (`urn:eawf:...` before `?=` or `#`) is the
  identity. Query / fragment are view / selection hints and ignored
  for identity comparison.
- Percent-encode unsafe characters; keep the canonical stored form
  ASCII.
- Do not store ordinary URLs or absolute file paths as Eä URNs. Put
  them in artifact metadata fields (`url`, `local_path`) when needed.

## Core URNs

```text
urn:eawf:v1:workspace:TM
urn:eawf:v1:repo:QR
urn:eawf:v1:repo:QR/configs/strategies/collar/v1.yaml
urn:eawf:v1:state:QR/P13-I04-W01
urn:eawf:v1:state:TM/XP01
urn:eawf:v1:artifact:QR/ART-20260506-p13-i04-audit
urn:eawf:v1:store:QR/memory/MEM-20260506-001
urn:eawf:v1:store:QR/executor_report/AR-executor-P18-I01-W04-01
urn:eawf:v1:blob:QR/sha256/2f1a...
urn:eawf:v1:pr:QR/42
urn:eawf:v1:commit:QR/abc1234
urn:eawf:v1:branch:QR/feature/p13-i04-lift
urn:eawf:v1:secret:FRED_API_KEY
```

## Resolution

- `workspace` resolves through workspace registry.
- `repo` with only owner resolves to repo root / state; `repo` with
  path resolves to repo root + relative path.
- `state` resolves to entity in owner state. Owner can be repo code
  (`QR`) or workspace code (`TM`).
- `store` resolves to JSONL record under owner
  `.ea/store/<kind>.jsonl` (singular `store/` directory; per-kind file
  named after the singular `StoreKind` value).
- `artifact` resolves through state artifact index to durable
  evidence / output metadata.
- `blob` resolves through content-addressed blob store for immutable
  large payloads.
- `pr` is project-scoped (`urn:eawf:v1:pr:QR/42`) and resolves through
  that repo's VCS provider config (`github`, `gitlab`, `forgejo`,
  `bitbucket`, etc.) and available git / CLI / API instruments.

## Store roots

```text
repo-local:       <repo>/.ea/store/
workspace-level:  <workspace>/.ea/store/
local/private:    <repo>/.ea/local/store/ or <workspace>/.ea/local/store/
```

## `store` vs `artifact` vs `blob`

- **`store`** is structured record storage: memory, incident,
  audit, research, decision, event, flow, and typed agent reports. Example:
  `urn:eawf:v1:store:QR/audit/AUD-P13-I04` resolves to one JSONL
  record. Agent report store URNs include the role-specific store kind
  before the report id, for example
  `urn:eawf:v1:store:QR/reviewer_report/AR-reviewer-P18-I01-W08-01`.
- **`artifact`** is durable output / evidence metadata: report,
  screenshot set, HTML, data manifest, generated markdown, PR body,
  promoted agent report.
  Example: `urn:eawf:v1:artifact:QR/ART-audit-p13-i04` resolves
  through the artifact index.
- **`blob`** is immutable content-addressed payload storage for large /
  noisy bytes: logs, command outputs, screenshots, HTML reports,
  transcript excerpts. Artifacts and store records point to blobs by
  hash for dedupe and integrity.

Stores are queryable records; artifacts are workflow evidence objects;
blobs are raw immutable payloads.

## Query and fragment components

Use `?=` for non-identity view / render parameters. Use `#` for a
named section, row, cell, or subview inside the resolved resource.
They MUST NOT change the referenced entity identity.

Good uses:

```text
urn:eawf:v1:state:QR/P13-I04?=view=dashboard
urn:eawf:v1:state:QR/P13-I04?=format=json
urn:eawf:v1:artifact:QR/ART-20260506-p13-i04-audit?=format=markdown#failures
urn:eawf:v1:store:QR/memory/MEM-20260506-001#summary
urn:eawf:v1:repo:QR/docs/ROADMAP.md#phase-13
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

- `?=` allowed keys: `view`, `format`, `rev`, `as_of`, `limit`,
  `section`.
- `format` controls representation shape; `view` controls selected
  content / layout.
- `#` names a client-side fragment after resolution: Markdown heading
  slug, JSON pointer alias, table section, or Eä-defined subview like
  `#summary`, `#evidence`, `#failures`, `#risks`.
- State should usually persist assigned-name only. Query / fragment
  are best for CLI / TUI links, PR comments, review notes, and human
  navigation.
- Equality and dedupe ignore query / fragment;
  `urn:eawf:v1:state:QR/P13-I04#summary` and `...#risks` point to the
  same entity with different views.

## Agent usage guidance

Agents should use URNs when referring to durable workflow objects in
outputs, evidence, memory, PR bodies, and audit findings. URN
references survive file moves, resolve across workspace / repo
boundaries, and let `eawf trace / show` navigate directly.

Use URNs for:

- state scopes: phase / iter / wave / hypothesis / audit,
- artifacts and JSONL store records,
- typed agent report rows, for example
  `urn:eawf:v1:store:QR/operator_report/AR-operator-P18-01`,
- commits, branches, PRs,
- evidence links in `/audit`, `/review`, `/ship`, `/polish`,
- durable memory entries.

Do not overuse URNs for casual prose or obvious nearby files. For code
references, keep normal `path:line` for developer navigation and add
URN only when the reference becomes durable evidence.

CLI unification rule: every command option that refers to a durable
scope, entity, artifact, store record, branch, commit, PR, or secret
should accept canonical URNs. Short IDs (`P13-I04`, `QR`, `ART-...`)
are convenience input only; `eawf` resolves them and persists canonical
URNs in state / stores / artifacts.

Migration note: old shorthand refs like `repo:QR` may be accepted in
CLI input, but state should persist canonical URNs.

## Cross-references

- State entities and ID grammar — `docs/architecture/state-model.md`.
- Source: `src/eawf/state/urn.py`.
