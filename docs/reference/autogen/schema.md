# Eä JSON Schema reference

Auto-generated from the canonical Pydantic models. The full JSON
Schema of each model is dumped to a sibling `.schema.json` file by
`eawf schema dump`; this page summarises the top-level properties.

## `State`

Full schema: [`state.schema.json`](state.schema.json)

| Property | Required |
|---|---|
| `actuals` | no |
| `agent_sessions` | yes |
| `artifacts` | yes |
| `audits` | no |
| `backlog` | no |
| `claims` | no |
| `current` | yes |
| `decisions` | no |
| `estimates` | no |
| `goals` | no |
| `health` | no |
| `hypotheses` | no |
| `incidents` | no |
| `indexes` | yes |
| `iters` | yes |
| `mcp_grants` | no |
| `mcp_servers` | no |
| `memory_index` | no |
| `open_questions` | no |
| `outcomes` | no |
| `phases` | yes |
| `plugins` | yes |
| `project` | yes |
| `sandbox_policies` | no |
| `schema_version` | yes |
| `scope_kind` | yes |
| `subprojects` | no |
| `updated_at` | yes |
| `urn` | yes |
| `waves` | yes |
| `workspace` | yes |
| `worktrees` | no |

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
