# No PLAN.md / DECISIONS.md / BACKLOG.md

*Operational state lives in `state.json` and JSONL stores, not in markdown files.*

## Policy

Drop these files from Eä defaults:

- `PLAN.md`
- `DECISIONS.md`
- `BACKLOG.md`

Everything operational is available from `eawf` reading the active Eä
state: workspace-level by default, repo sub-state when enabled.

## Replacements

```bash
eawf status                  # Shows current project / subproject / phase / iter / waves, blockers, next actions.
eawf plan show               # Shows active generated plan / spec from state-backed records.
eawf decision list           # Lists decisions stored in state.
eawf backlog list            # Lists backlog items stored in state.
eawf hypothesis list         # Lists hypothesis status from state.
eawf audit list              # Lists audit reports and verdicts from state.
```

Generated markdown specs / reports may still exist as **artifacts**
when useful, but they are not source of truth for status. State points
to them via artifact IDs.

## Why

State-first lets agents and humans both read the same source. A
`PLAN.md` written by one session and edited by another session creates
two competing truths; reconciling them after the fact is pure
overhead. Eä keeps the decision history in `decisions.jsonl`, the
backlog in `backlog` state entries, and the current plan rendered on
demand from active iter / wave records.

## Storage model

```text
.ea/
  state.json                  # compact authoritative index / current pointers
  config.yaml                 # human-editable project settings
  schema.json                 # pinned schema
  stores/
    research.jsonl            # large research briefs / events
    audits.jsonl              # audit reports / results
    incidents.jsonl           # timelines / root cause records
    estimates.jsonl           # estimate history, superseded estimates, actual segments
    memory.jsonl              # durable memory entries
    decisions.jsonl           # decision records if too large for state
    events.jsonl              # append-only state / event history
  artifacts/
    blobs/sha256/<hash>       # large payloads, command output, rendered files
    rendered/*.md             # optional generated human views
  indexes/
    artifacts.json            # compact lookup cache, rebuildable
    generated.json            # sidecar manifest for managed regions
```

Rules:

- `state.json` stores IDs, status, summaries, pointers, current fields,
  metrics, evidence refs.
- Estimate / actual state entries store current summaries only; complete
  estimate versions and actual segments live in
  `.ea/stores/estimates.jsonl`.
- JSONL stores append-friendly large records. Each line is one
  validated object with `id`, `kind`, `schema_version`, timestamps,
  scope, summary, payload or blob refs.
- Large command outputs or transcripts go to content-addressed blobs;
  JSONL stores reference hash / path / summary.
- Markdown is generated from state / JSONL for review, PR bodies,
  reports, or docs; it is not source of truth unless explicitly curated
  as an artifact.
- CLI / TUI provides human views: `eawf memory view`,
  `eawf incident view`, `eawf audit show`, `eawf research show`.
- Compaction is explicit: `eawf store compact --kind memory`; never
  silently drops history.

JSONL is acceptable for memories / incidents / research / audits /
estimates because human readability is provided by CLI views, not raw
files.

Default policy: commit all nonlocal stores (`research.jsonl`,
`audits.jsonl`, `incidents.jsonl`, `estimates.jsonl`, `memory.jsonl`,
`decisions.jsonl`, `events.jsonl`) when project policy allows;
local / session scratch and large blobs stay under `.ea/local/` or
gitignored blob storage. `events.jsonl` is an append-only audit log
only, not a replay source of truth.

## Cross-references

- State entities — `docs/architecture/state-model.md`.
- JSONL store record envelope and event payload —
  `docs/architecture/envelope.md`.
- AGENTS.md / CLAUDE.md generation — `docs/policy/agents-claude-md.md`.
