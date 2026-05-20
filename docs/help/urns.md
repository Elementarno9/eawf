# URNs

Eä uses URNs (RFC 8141, informal namespace `eawf`) as persistent,
location-independent names for durable workflow objects. URN references
survive file moves and resolve across workspace / repo boundaries. The full
reference lives in `docs/reference/urn-namespace.md`; the grammar source of
truth is `src/eawf/state/urn.py`.

## Format

```text
urn:eawf:v1:<kind>:<owner>[/<id-or-path>][?=<query>][#<fragment>]
```

- **NID** is `eawf`; **version** `v1` immediately follows it.
- **Kind** is lowercase ASCII (see catalog below).
- **Owner codes** are uppercase where human-defined (`TM`, `QR`).
- The assigned-name (everything before `?=` or `#`) is the identity; query
  and fragment are view / selection hints and are ignored for equality.

## Kind catalog

`workspace`, `repo`, `state`, `artifact`, `store`, `blob`, `pr`, `commit`,
`branch`, `secret` (`URN_KINDS` in `src/eawf/state/urn.py`).

```text
urn:eawf:v1:workspace:TM
urn:eawf:v1:repo:QR/configs/strategies/v1.yaml
urn:eawf:v1:state:QR/P26-I01-W01
urn:eawf:v1:artifact:QR/ART-20260520-audit
urn:eawf:v1:store:QR/executor_report/AR-executor-P26-I01-W06-01
urn:eawf:v1:blob:QR/sha256/2f1a...
urn:eawf:v1:pr:QR/42
urn:eawf:v1:commit:QR/abc1234
urn:eawf:v1:branch:QR/feature/p26-streaming
urn:eawf:v1:secret:SOME_API_KEY
```

## store vs artifact vs blob

- **store** — structured JSONL records: memory, incident, audit, research,
  decision, event, flow, and typed agent reports. Report URNs include the
  role-specific store kind, e.g. `.../reviewer_report/AR-reviewer-...`.
- **artifact** — durable output / evidence metadata: reports, rendered
  markdown, PR bodies, promoted agent reports.
- **blob** — immutable content-addressed payloads (logs, command output,
  screenshots) referenced by sha256 for dedupe + integrity.

## Query and fragment

`?=` carries non-identity view params (`view`, `format`, `rev`, `as_of`,
`limit`, `section`); `#` names a client-side subview after resolution
(`#summary`, `#evidence`, `#failures`). Neither changes identity:

```text
urn:eawf:v1:state:QR/P26-I01?=view=dashboard
urn:eawf:v1:artifact:QR/ART-20260520-audit?=format=markdown#failures
```

## When to use

Use URNs for durable references in outputs, evidence, memory, PR bodies, and
audit findings — state scopes, store records, agent reports, commits,
branches, PRs. Keep ordinary `path:line` for casual code navigation; do not
store plain URLs or absolute file paths as URNs (put those in artifact
metadata fields). Short ids (`P26-I01`, `QR`) are convenience input; eawf
resolves them and persists canonical URNs.
