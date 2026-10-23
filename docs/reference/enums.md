# eawf state enums

## Lifecycle statuses

The native lifecycle entities keep their stored states beside their models under `src/eawf/kernel/state/epoch2/`.

| Entity | Class | Values | Notes |
|---|---|---|---|
| Track | `TrackStatus` | `ACTIVE`, `RETIRED` | `RETIRED` is terminal and never reactivated |
| Milestone | `MilestoneStatus` | `PLANNED`, `ACTIVE`, `ACCEPTANCE_REVIEW`, `COMPLETED`, `CANCELLED` | the unit an operator accepts |
| Batch | `BatchStatus` | `PLANNED`, `ACTIVE`, `READY_TO_MERGE`, `MERGING`, `MERGED_PENDING_RECONCILIATION`, `COMPLETED`, `CANCELLED`, `FAILED` | one repository's integration unit |
| Task | `TaskStatus` | `DRAFT`, `DEFERRED`, `DROPPED`, `PLANNED`, `CLAIMED`, `RUNNING`, `READY_TO_INTEGRATE`, `COMPLETED`, `CANCELLED`, `FAILED` | the draft head is the backlog |
| Run | `RunStatus` | `QUEUED`, `RUNNING`, `SUSPENDED`, `COMPLETED`, `FAILED`, `CANCELLED` | a suspended Run names its reason |
| PlanRevision | `PlanRevisionStatus` | `DRAFT`, `VALIDATED`, `APPROVED`, `APPLIED`, `REJECTED`, `SUPERSEDED` | an approval binds the content digest |

## Shared enums

This section covers `StrEnum` classes defined in `src/eawf/kernel/state/enums.py`; the generated [enum catalog](autogen/enums.md) lists every one of them with its members.

| Entity | Field | Class | Values | Notes |
|---|---|---|---|---|
| project | status | `ProjectStatus` | `active`, `archived`, `retired` | Top-level project lifecycle |
| subproject | status | `SubprojectStatus` | `active`, `planned`, `deferred`, `retired` | Sub-scope of a project |
| goal | status | `GoalStatus` | `open`, `achieved`, `abandoned` | Strategic goal tracking |
| outcome | status | `OutcomeStatus` | `pending`, `met`, `missed`, `waived` | Measurable outcome result |
| outcome | direction | `OutcomeDirection` | `min`, `max`, `equal`, `range` | Optimization direction for metric |
| hypothesis | status | `HypothesisStatus` | `pending`, `confirmed`, `rejected`, `inconclusive`, `deferred` | Research hypothesis tracking |
| hypothesis | verdict | `HypothesisVerdict` | `confirmed`, `rejected`, `inconclusive` | Final verdict on a hypothesis |
| audit | kind | `AuditKind` | `evaluation`, `ship-gate`, `incident`, `review` | Category of audit event |
| audit | status | `AuditStatus` | `pending`, `running`, `complete`, `failed` | Audit execution state |
| audit | verdict | `AuditVerdict` | `pass`, `minor`, `major` | Audit outcome quality |
| decision | status | `DecisionStatus` | `active`, `superseded`, `reversed` | ADR / decision record lifecycle |
| backlog | priority | `BacklogPriority` | `P0`, `P1`, `P2`, `P3` | Backlog item urgency (P0 = highest) |
| backlog | status | `BacklogStatus` | `open`, `in_progress`, `closed`, `deferred` | Backlog item workflow state |
| incident | severity | `IncidentSeverity` | `low`, `medium`, `high`, `critical` | Impact severity of an incident |
| incident | status | `IncidentStatus` | `open`, `mitigated`, `resolved`, `wont-fix` | Incident resolution lifecycle |
| flow | status | `FlowStatus` | `pending`, `in_progress`, `paused`, `blocked`, `done`, `abandoned`, `superseded` | Workflow / flow run state |
| actual | status | `ActualStatus` | `planned`, `active`, `done`, `interrupted`, `blocked`, `abandoned`, `failed`, `superseded` | Actual segment lifecycle |
| agent_session | role | `AgentSessionRole` | `researcher`, `planner`, `executor`, `auditor`, `reviewer`, `polisher`, `operator`, `domain-specialist` | Agent role within a session |
| agent_report | verdict | `AgentReportVerdict` | `pass`, `pass-with-followups`, `fail`, `blocked` | Typed report outcome emitted by agent-end hooks |
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
| store | kind | `StoreKind` | `research`, `audit`, `incident`, `estimate`, `actual`, `memory`, `decision`, `event`, `flow`, `researcher_report`, `planner_report`, `executor_report`, `auditor_report`, `reviewer_report`, `polisher_report`, `operator_report`, `domain_specialist_report` | Store / artifact category |
| artifact | kind | `ArtifactKind` | `audit_report`, `notebook`, `dataset`, `model`, `backtest`, `strategy`, `binary`, `scene`, `playtest_session`, `cve_ref`, `research_brief`, `plan_spec`, `agent_report` | Durable artifact vocabulary |
