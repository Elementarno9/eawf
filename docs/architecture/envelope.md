# Skill output envelope

*Uniform JSON envelope returned by every workflow skill; markdown is rendered from JSON via `eawf render-output`.*

Every workflow skill returns a uniform envelope. Canonical form is JSON;
markdown is rendered from JSON via `eawf render-output --format
markdown`. Statusline, hooks, and runtime adapters parse JSON only —
they never grep markdown.

## Envelope schema (shared)

```yaml
ea_skill_output:
  header:
    skill: /research | /prep | /audit | /ship | /review | /polish | /init | /roadmap | /differentiate | /flow
    scope: urn:eawf:v1:state:<owner>/<scope-id>
    session: urn:eawf:v1:store:<owner>/sessions/<SES-id>
    started_at: 2026-05-08T12:00:00Z
    finished_at: 2026-05-08T12:34:56Z
    status: ok | needs_user | blocked | failed | partial
    instrument_probe:
      git: ok
      claude: ok
      gh: missing
  body: <skill-specific schema; see "Per-skill body schemas" below>
  footer:
    persisted_artifacts: [urn:eawf:v1:artifact:QR/ART-...]
    persisted_store_records: [urn:eawf:v1:store:QR/research/...]
    state_mutations: [phases.P13.status=active]
    evidence_refs: [urn:eawf:v1:audit:..., urn:eawf:v1:commit:QR/abc123]
    next_valid_actions: [eawf prep P13-I04, /audit P13-I04]
    warnings:
      - { code: instrument_missing, detail: "gh not installed; PR open skipped" }
```

Render rules:

- `--json` emits the envelope verbatim.
- `--plain` / TUI renders header as one status line, body as markdown,
  footer as a "next actions" list.
- `status=needs_user` MUST include `body.user_question` with a 2–4-option
  `AskUserQuestion` payload.
- `status=blocked` or `status=failed` MUST include
  `footer.repair_commands`.
- All durable references use Eä URNs.

## Per-skill body schemas

Each workflow skill defines a typed `body` payload validated by the
JSON Schema below. Body schemas:

- **`/research`**: `{ brief_id, questions: [{q, answer, confidence,
  sources}], options: [{name, tradeoffs, complexity, reversibility,
  risks}], recommendation: {choice, confidence, fallback}, peer_review:
  {reviewer_id, findings: [], no_flaws_checks: []}, persisted_brief?:
  urn }`.
- **`/prep`**: `{ iter_id, objective, non_goals, dag: [{task_id, deps,
  file_scope, commands, evidence, risk}], waves: [{wave_id, tasks,
  worktree_policy, estimate_eu}], acceptance: {checks, baselines},
  approval_required: bool }`.
- **`/audit`**: `{ scope_id, kind: evaluation|ship-gate, checks_run:
  [{check_id, command, status, output_blob}], outcomes_measured:
  [{outcome_id, value, threshold, verdict}], hypothesis_verdicts:
  [{hypothesis_id, verdict, evidence_commit}], findings: [{severity,
  location, summary, kind: blocker|fix-now|follow-up|false-positive}],
  audit_artifact_urn }`.
- **`/ship`**: `{ commit_groups: [{message, files, evidence_refs}],
  push: {ref, status}, pr: {action, url, template, gates: {ci, reviews,
  state_valid}}, estimate_vs_actual: {…}, rollback_notes }`.
- **`/review`**: `{ pr_url, base, head, findings: [{severity, location,
  comment, suggested_fix}], recommendation: approve|comment|
  request_changes|fix_locally, posted: bool }`.
- **`/polish`**: `{ groups: [{topic, scope, risk, items: [{kind:
  stale_doc|duplicate_rule|broken_link|orphan_artifact|stale_memory|
  naming_drift, location, action, applied: bool}]}], memory_pass:
  {promotions, prunes, compactions}, report_only: bool }`.

Skills outside the canonical six (`/init`, `/roadmap`, `/differentiate`,
`/flow`) reuse the envelope with skill-specific bodies. `/flow` body
wraps a list of nested per-skill envelopes for each phase of the run.

## JSON Schema artifact

The canonical schema lives at `src/eawf/schemas/skill-output.schema.json`
in the framework repo. `eawf validate --strict` accepts a skill output
JSON and verifies envelope conformance. `eawf render-output` round-trips
between JSON and markdown:

```bash
# JSON → markdown
echo '<envelope-json>' | eawf render-output --format markdown

# markdown → JSON (parses frontmatter and reconstructs envelope)
cat brief.md | eawf render-output --format json
```

## JSONL store record envelope

Every JSONL store record (research, audit, incident, estimate, actual,
memory, decision, event, flow) shares this envelope:

| Field | Type | Required | Notes |
|---|---|---:|---|
| `schema_version` | literal `"1.0"` | yes | store schema |
| `id` | string | yes | unique in store |
| `kind` | enum | yes | `research`, `audit`, `incident`, `estimate`, `actual`, `memory`, `decision`, `event`, `flow` |
| `scope_id` | string / null | yes | lifecycle or owner scope |
| `created_at` | datetime | yes | append time |
| `updated_at` | datetime / null | yes | null for immutable events |
| `summary` | string | yes | <= 500 chars |
| `payload` | typed object | yes | strict per kind |
| `blob_refs` | list[sha256] | yes | large payload refs |
| `artifact_ids` | list[string] | yes | linked artifacts |

## Event payload fields

`events.jsonl` is audit-log only. Event payload requires:
`event_type`, `actor`, `command`, `args_hash`, `scope_id`,
`before_state_version`, `after_state_version`, `status`, `message`,
`artifact_ids`, `timestamp`.

## Config schema required sections

The composed `.ea/config.yaml` schema covers these sections: `cli`,
`project`, `workspace`, `profiles`, `runtime`, `ui`, `storage`,
`research`, `planning`, `estimation`, `audit`, `ship`, `review`,
`polish`, `memory`, `vcs`, `worktrees`, `acceptance`, `security`,
`hooks`, `mcp`, `statusline`, `docs`, `commands`, `state_schema`.

## Cross-references

- Hook events — `docs/reference/hook-events.md`.
- Skill algorithms — `docs/architecture/workflow.md`.
- State entities — `docs/architecture/state-model.md`.
