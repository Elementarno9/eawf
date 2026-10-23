# Eä JSON Schema reference

Auto-generated from the canonical schemas. The full JSON Schema of each is dumped to a sibling `.schema.json` file by `eawf schema dump`; this page summarises the top-level properties.

## `EawfGenerationDocument`

Full schema: [`state.schema.json`](state.schema.json)

| Property | Required |
|---|---|
| `actual` | no |
| `artifact` | no |
| `audit` | no |
| `batch` | no |
| `campaign` | no |
| `campaign_finding` | no |
| `canonical_sequence` | no |
| `capability` | no |
| `claim` | no |
| `current` | no |
| `decision` | no |
| `dispatch_paused` | no |
| `draft` | no |
| `estimate` | no |
| `event` | no |
| `evidence` | no |
| `fleet_run` | no |
| `health_view` | no |
| `hypothesis` | no |
| `incident` | no |
| `indexes` | no |
| `lease` | no |
| `legacy` | no |
| `memory` | no |
| `milestone` | no |
| `open_question` | no |
| `pending_action` | no |
| `permission` | no |
| `plan_revision` | no |
| `project` | no |
| `receipt` | no |
| `release` | no |
| `repository` | no |
| `run` | no |
| `sandbox_policy` | no |
| `schema_version` | yes |
| `task` | no |
| `telemetry` | no |
| `tool_authority` | no |
| `track` | no |
| `track_outcome` | no |
| `workspace` | no |

## `Event`

Full schema: [`event.schema.json`](event.schema.json)

| Property | Required |
|---|---|
| `id` | yes |
| `idempotency_key` | no |
| `occurred_at` | yes |
| `payload` | yes |
| `schema_version` | no |
| `scope_id` | yes |

## `OutputEnvelope`

Full schema: [`output-envelope.schema.json`](output-envelope.schema.json)

| Property | Required |
|---|---|
| `body` | yes |
| `footer` | yes |
| `header` | yes |
