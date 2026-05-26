# eawf state enums

Auto-generated from `eawf.kernel.state.enums`. Every `StrEnum` defined in
that module is listed with its members.

| Class | Values |
|---|---|
| `ActualStatus` | `planned`, `active`, `done`, `interrupted`, `blocked`, `abandoned`, `failed`, `superseded` |
| `AgentReportVerdict` | `pass`, `pass-with-followups`, `fail`, `blocked` |
| `AgentSessionRole` | `researcher`, `planner`, `executor`, `auditor`, `reviewer`, `polisher`, `operator`, `domain-specialist` |
| `AgentSessionStatus` | `active`, `checkpointed`, `closed`, `stale`, `failed` |
| `ArtifactKind` | `audit_report`, `notebook`, `dataset`, `model`, `backtest`, `strategy`, `binary`, `scene`, `playtest_session`, `cve_ref`, `research_brief`, `plan_spec`, `agent_report` |
| `AuditKind` | `evaluation`, `ship-gate`, `incident`, `review` |
| `AuditStatus` | `pending`, `running`, `complete`, `failed` |
| `AuditVerdict` | `pass`, `minor`, `major` |
| `BacklogPriority` | `P0`, `P1`, `P2`, `P3` |
| `BacklogStatus` | `open`, `in_progress`, `closed`, `deferred` |
| `Confidence` | `high`, `medium`, `low` |
| `DecisionStatus` | `active`, `superseded`, `reversed` |
| `DispatchNote` | `fresh_dispatch`, `continue_from_session`, `continue_failed_fell_back_to_fresh`, `switch_on_error`, `switch_manual` |
| `EffortBucket` | `XS`, `S`, `M`, `L`, `XL` |
| `FlowStatus` | `pending`, `in_progress`, `paused`, `blocked`, `done`, `abandoned`, `superseded` |
| `GoalStatus` | `open`, `achieved`, `abandoned` |
| `Health` | `ok`, `needs_setup`, `degraded` |
| `HypothesisStatus` | `pending`, `confirmed`, `rejected`, `inconclusive`, `deferred` |
| `HypothesisVerdict` | `confirmed`, `rejected`, `inconclusive` |
| `IncidentCause` | `runtime_rate_limit`, `runtime_server_error`, `runtime_timeout`, `runtime_api_error`, `runtime_auth_error`, `runtime_unavailable`, `runtime_oauth_cache_stripped`, `daemon_wal_recovery`, `daemon_socket_bind`, `daemon_version_skew`, `daemon_subprocess_oom`, `daemon_subscription_dropped`, `daemon_lock_timeout`, `cache_mislayer`, `cost_budget_breached`, `session_handle_pruned`, `session_failover`, `worktree_cherry_pick_conflict`, `worktree_branch_stale`, `git_push_rejected`, `plugin_drift`, `spec_validation_failed`, `state_validation_failed`, `audit_failed`, `operator_interrupt`, `external_api_failure`, `legacy_free_text`, `unknown` |
| `IncidentSeverity` | `low`, `medium`, `high`, `critical` |
| `IncidentStatus` | `open`, `mitigated`, `resolved`, `wont-fix` |
| `IterStatus` | `planned`, `active`, `closed`, `abandoned` |
| `McpRisk` | `read`, `read-write`, `admin` |
| `McpStatus` | `not_configured`, `configured`, `installed`, `degraded`, `disabled` |
| `MemoryStatus` | `active`, `stale`, `superseded`, `pruned` |
| `MemoryTier` | `working`, `archival`, `retrieval` |
| `OutcomeDirection` | `min`, `max`, `equal`, `range` |
| `OutcomeStatus` | `pending`, `met`, `missed`, `waived` |
| `PhaseStatus` | `planned`, `active`, `closed`, `archived` |
| `PluginInstallStatus` | `installed`, `drifted`, `conflicted`, `disabled` |
| `ProjectStatus` | `active`, `archived`, `retired` |
| `ScopeKind` | `repo`, `workspace` |
| `SkillEnvelopeStatus` | `ok`, `needs_user`, `blocked`, `failed`, `partial` |
| `SpecStatus` | `draft`, `ready`, `implemented`, `archived` |
| `StoreKind` | `research`, `audit`, `incident`, `estimate`, `actual`, `memory`, `decision`, `event`, `evidence`, `flow`, `researcher_report`, `planner_report`, `executor_report`, `auditor_report`, `reviewer_report`, `polisher_report`, `operator_report`, `domain_specialist_report`, `subscription_lag`, `config_updated`, `registry_updated`, `spec_updated` |
| `SubprojectStatus` | `active`, `planned`, `deferred`, `retired` |
| `WaveStatus` | `pending`, `claimed`, `in_progress`, `closed`, `failed`, `abandoned` |
| `WorktreeStatus` | `active`, `conflicted`, `merged`, `abandoned` |
