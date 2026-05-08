# eawf state enums

This file is the canonical reference for all `StrEnum` classes defined in
`src/eawf/state/enums.py`. Every enum class in that module cites this document.

| Entity | Field | Class | Values | Notes |
|---|---|---|---|---|
| project | status | `ProjectStatus` | `active`, `archived`, `retired` | Top-level project lifecycle |
| subproject | status | `SubprojectStatus` | `active`, `planned`, `deferred`, `retired` | Sub-scope of a project |
| goal | status | `GoalStatus` | `open`, `achieved`, `abandoned` | Strategic goal tracking |
| outcome | status | `OutcomeStatus` | `pending`, `met`, `missed`, `waived` | Measurable outcome result |
| outcome | direction | `OutcomeDirection` | `min`, `max`, `equal`, `range` | Optimization direction for metric |
| phase | status | `PhaseStatus` | `planned`, `active`, `closed`, `archived` | Roadmap phase lifecycle |
| iter | status | `IterStatus` | `planned`, `active`, `closed`, `abandoned` | Iteration (sprint) lifecycle |
| wave | status | `WaveStatus` | `pending`, `claimed`, `in_progress`, `closed`, `failed`, `abandoned` | Wave agent work unit lifecycle |
| hypothesis | status | `HypothesisStatus` | `pending`, `confirmed`, `rejected`, `inconclusive`, `deferred` | Research hypothesis tracking |
| hypothesis | verdict | `HypothesisVerdict` | `confirmed`, `rejected`, `inconclusive` | Final verdict on a hypothesis |
| audit | kind | `AuditKind` | `evaluation`, `ship-gate`, `incident`, `review` | Category of audit event |
| audit | status | `AuditStatus` | `pending`, `running`, `complete`, `failed` | Audit execution state |
| audit | verdict | `AuditVerdict` | `pass`, `minor`, `major`, `fail` | Audit outcome quality |
| decision | status | `DecisionStatus` | `active`, `superseded`, `reversed` | ADR / decision record lifecycle |
| backlog | priority | `BacklogPriority` | `P0`, `P1`, `P2`, `P3` | Backlog item urgency (P0 = highest) |
| backlog | status | `BacklogStatus` | `open`, `in_progress`, `closed`, `deferred` | Backlog item workflow state |
| incident | severity | `IncidentSeverity` | `low`, `medium`, `high`, `critical` | Impact severity of an incident |
| incident | status | `IncidentStatus` | `open`, `mitigated`, `resolved`, `wont-fix` | Incident resolution lifecycle |
| flow | status | `FlowStatus` | `pending`, `in_progress`, `paused`, `blocked`, `done`, `abandoned`, `superseded` | Workflow / flow run state |
| agent_session | role | `AgentSessionRole` | `researcher`, `planner`, `executor`, `auditor`, `reviewer`, `polisher`, `operator`, `domain-specialist` | Agent role within a session |
| agent_session | status | `AgentSessionStatus` | `active`, `checkpointed`, `closed`, `stale`, `failed` | Agent session lifecycle |
| worktree | status | `WorktreeStatus` | `active`, `conflicted`, `merged`, `abandoned` | Git worktree lifecycle |
| mcp_server | risk | `McpRisk` | `read`, `read-write`, `admin` | MCP server permission tier |
| mcp_server | status | `McpStatus` | `not_configured`, `configured`, `installed`, `degraded`, `disabled` | MCP server install state |
| plugin_install | status | `PluginInstallStatus` | `installed`, `drifted`, `conflicted`, `disabled` | Plugin / skill install state |
| memory_summary | status | `MemoryStatus` | `active`, `stale`, `superseded`, `pruned` | Memory file lifecycle |
| memory_summary | confidence | `Confidence` | `high`, `medium`, `low` | Confidence level (also used for estimates) |
| skill_envelope | status | `SkillEnvelopeStatus` | `ok`, `needs_user`, `blocked`, `failed`, `partial` | Skill invocation result envelope |
| health | — | `Health` | `ok`, `needs_setup`, `degraded` | Doctor / system health check result |
| scope | kind | `ScopeKind` | `repo`, `workspace` | Scope of a config or profile |
| store | kind | `StoreKind` | `research`, `audit`, `incident`, `estimate`, `actual`, `memory`, `decision`, `event`, `flow` | Store / artifact category |
