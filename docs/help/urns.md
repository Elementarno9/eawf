# URNs

Eä names every durable workflow object with a URN, a persistent and location-independent name. A URN survives file moves and resolves across workspace and repository boundaries. Two forms are in use: the qualified `eawf://` URN addresses the lifecycle entities (Track, Milestone, Batch, Task, Run and their neighbours), and the `urn:eawf:v1:` URN addresses store records, artifacts, blobs and version-control objects. The full reference lives in `docs/reference/urn-namespace.md`.

## Qualified entity URNs

```text
eawf://<workspace-key>/<project-key>/<repository-key-or-_>/<entity-kind>/<entity-key>[#<fragment>]
```

- The repository slot is never omitted. A workspace- or project-level entity (a release, a claim) takes the reserved `_`; a repository-level entity (a Milestone, a Batch, a Task, a Run) names its repository.
- The entity kind is one of the `EntityKind` values in `src/eawf/kernel/identity/keys.py`, and it must agree with the kind of reference that carries the URN.
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

- The namespace is `eawf` and the version `v1` immediately follows it.
- The kind is lowercase ASCII; `URN_KINDS` in `src/eawf/kernel/state/urn.py` is the catalog.
- The owner is the uppercase project code.
- The assigned name (everything before `?=` or `#`) is the identity; the query and fragment are view and selection hints and are ignored for equality.

```text
urn:eawf:v1:store:EAWF/executor_report/AR-executor-EAWF-0042-01
urn:eawf:v1:artifact:EAWF/ART-20260520-audit
urn:eawf:v1:blob:EAWF/sha256/2f1a...
urn:eawf:v1:repo:EAWF/configs/strategies/v1.yaml
urn:eawf:v1:pr:EAWF/42
urn:eawf:v1:commit:EAWF/abc1234
urn:eawf:v1:branch:EAWF/feature/eawf-v0.7
```

## store vs artifact vs blob

- **store**: structured JSONL records such as memory, incident, audit, research, decision, event and typed agent reports. A report URN names its role-specific store kind, e.g. `.../reviewer_report/AR-reviewer-...`.
- **artifact**: durable output and evidence metadata such as reports, rendered markdown, PR bodies and promoted agent reports.
- **blob**: immutable content-addressed payloads (logs, command output, screenshots) referenced by sha256 for dedupe and integrity.

## Query and fragment

On a record URN, `?=` carries non-identity view parameters (`view`, `format`, `rev`, `as_of`, `limit`, `section`) and `#` names a client-side subview after resolution (`#summary`, `#evidence`, `#failures`). Neither changes identity:

```text
urn:eawf:v1:artifact:EAWF/ART-20260520-audit?=format=markdown#failures
```

## When to use

Use URNs for durable references in outputs, evidence, memory, PR bodies and audit findings: lifecycle entities, store records, agent reports, commits, branches and PRs. Keep ordinary `path:line` for casual code navigation; do not store plain URLs or absolute file paths as URNs (put those in artifact metadata fields).
