# Skill output envelope

*Uniform JSON envelope returned by every workflow skill; markdown is rendered from JSON via `eawf render-output`.*

Every workflow skill returns a uniform envelope. Canonical form is JSON; markdown is rendered from JSON via `eawf render-output --format markdown`. Statusline, hooks, and runtime adapters parse JSON only — they never grep markdown.

## Envelope schema (shared)

```yaml
ea_skill_output:
  header:
    skill: /plan | /dispatch | /verify | /integrate | /research | ...   # open: overlays emit envelopes too
    scope: eawf://<workspace>/<project>/<repository>/<kind>/<key>
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
    state_mutations: [milestone/MLS-0030.status=ACTIVE]
    evidence_refs: [urn:eawf:v1:audit:..., urn:eawf:v1:commit:QR/abc123]
    next_valid_actions: [eawf status, /verify]
    warnings:
      - { code: instrument_missing, detail: "gh not installed; PR open skipped" }
```

Render rules:

- `--json` emits the envelope verbatim.
- `--plain` / TUI renders header as one status line, body as markdown, footer as a "next actions" list.
- `status=needs_user` MUST include `body.user_question` with a 2–4-option `AskUserQuestion` payload.
- `status=blocked` or `status=failed` MUST include `footer.repair_commands`.
- All durable references use Eä URNs.

## Per-skill body schemas

Each workflow skill may define a typed `body` payload under `eawf.workflow.skills.bodies`; a skill without one returns a markdown body. The three lifecycle bodies that drive the native delivery path:

- **`/dispatch`** (`DispatchBody`): `{ kind, batch_ref, source_cursor, contract, plan, dispatched, frontier, stopped_on, outcome, reason }`.
- **`/integrate`** (`IntegrateBody`): `{ kind, action, subject_ref, method, candidates, generation_ids, conflict_refs, verification_receipts, unresolved_request_fields, refusal_code, outcome, reason }`.
- **`/verify`** (`VerifyBody`): `{ kind, subject_ref, mode, method, head_generation, stage, blocking_criterion_ids, settled_criterion_ids, rows, merge_ready, approval_ref, bundle_digest, acceptance_bundle, unresolved_request_fields, refusal_code, outcome, reason }`.

Other typed bodies:

- **`/research`** (`ResearchBody`): `{ brief_id, questions: [{q, answer, confidence, sources}], peer_review: {reviewer_id, findings: [], no_flaws_checks: []}, persisted_brief?: urn }`.
- **`/memory`** (`MemoryBody`): `{ kind, verb, name, tier, reason }`.
- **`/mockup`** (`MockupBody`): `{ target_scope, variants }`.

Every body carries the shared optional `user_question` payload, which strict validation requires when `header.status == "needs_user"`.

## JSON Schema artifact

The canonical schema lives at `src/eawf/schemas/skill-output.schema.json` in the framework repo. `eawf validate --strict` accepts a skill output JSON and verifies envelope conformance. `eawf render-output` round-trips between JSON and markdown:

```bash
# JSON → markdown
echo '<envelope-json>' | eawf render-output --format markdown

# markdown → JSON (parses frontmatter and reconstructs envelope)
cat brief.md | eawf render-output --format json
```

## JSONL store record envelope

Every JSONL store record (research, audit, incident, estimate, actual, memory, decision, event) shares this envelope:

| Field | Type | Required | Notes |
|---|---|---:|---|
| `schema_version` | literal `"1.0"` | yes | store schema |
| `id` | string | yes | unique in store |
| `kind` | enum | yes | the store kind, such as `research`, `audit`, `incident`, `estimate`, `actual`, `memory`, `decision`, `event` |
| `scope_id` | string / null | yes | lifecycle or owner scope |
| `created_at` | datetime | yes | append time |
| `updated_at` | datetime / null | yes | null for immutable events |
| `summary` | string | yes | <= 500 chars |
| `payload` | typed object | yes | strict per kind |
| `blob_refs` | list[sha256] | yes | large payload refs |
| `artifact_ids` | list[string] | yes | linked artifacts |

## Event payload fields

`.ea/store/event.jsonl` is audit-log only. Event payload requires: `event_type`, `actor`, `command`, `args_hash`, `scope_id`, `before_state_version`, `after_state_version`, `status`, `message`, `artifact_ids`, `timestamp`.

## Config schema required sections

The composed `.ea/config.yaml` schema covers these sections: `cli`, `project`, `workspace`, `profiles`, `runtime`, `ui`, `storage`, `research`, `planning`, `estimation`, `audit`, `ship`, `review`, `polish`, `memory`, `vcs`, `worktrees`, `acceptance`, `security`, `hooks`, `mcp`, `statusline`, `docs`, `commands`, `state_schema`.

## Cross-references

- Hook events — `docs/reference/hook-events.md`.
- Skill algorithms — `docs/architecture/workflow.md`.
- State entities — `docs/architecture/state-model.md`.
