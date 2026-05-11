"""Pydantic v2 state models for eawf.

Mirrors ``docs/architecture/state-model.md``. Every model uses
``ConfigDict(extra="forbid")``. ID-shaped fields use regex patterns from
:mod:`eawf.state.ids`. URN-shaped fields use the ``urn:eawf:v1:`` prefix
constraint. Datetimes are tz-aware UTC (Pydantic accepts ISO-8601 with ``Z``).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from eawf.state.enums import (
    ActualStatus,
    AgentSessionRole,
    AgentSessionStatus,
    AuditKind,
    AuditStatus,
    AuditVerdict,
    BacklogPriority,
    BacklogStatus,
    Confidence,
    DecisionStatus,
    FlowStatus,
    GoalStatus,
    Health,
    HypothesisStatus,
    HypothesisVerdict,
    IncidentSeverity,
    IncidentStatus,
    IterStatus,
    McpRisk,
    McpStatus,
    MemoryStatus,
    MemoryTier,
    OutcomeDirection,
    OutcomeStatus,
    PhaseStatus,
    PluginInstallStatus,
    ProjectStatus,
    ScopeKind,
    SubprojectStatus,
    WaveStatus,
    WorktreeStatus,
)
from eawf.state.ids import (
    RE_HYPOTHESIS,
    RE_HYPOTHESIS_SCOPED,
    RE_ITER,
    RE_PHASE,
    RE_PROJECT_CODE,
    RE_WAVE,
)
from eawf.state.types import UtcDatetime
from eawf.state.urn import URN_KINDS

# ---- Reusable annotated types -----------------------------------------------

_URN_KINDS_PATTERN = "|".join(sorted(URN_KINDS))
UrnStr = Annotated[
    str,
    Field(
        pattern=(
            rf"^urn:eawf:v1:({_URN_KINDS_PATTERN}):"
            r"[^:?#/]+(?:/[^?#]*)?(?:\?=[^#]*)?(?:#.*)?$"
        )
    ),
]
ProjectCodeStr = Annotated[str, Field(pattern=RE_PROJECT_CODE.pattern)]
PhaseIdStr = Annotated[str, Field(pattern=RE_PHASE.pattern)]
IterIdStr = Annotated[str, Field(pattern=RE_ITER.pattern)]
WaveIdStr = Annotated[str, Field(pattern=RE_WAVE.pattern)]
HypothesisIdStr = Annotated[
    str,
    Field(pattern=f"^(?:{RE_HYPOTHESIS.pattern[1:-1]}|{RE_HYPOTHESIS_SCOPED.pattern[1:-1]})$"),
]
IdStr = Annotated[str, Field(min_length=1, pattern=r"^\S+$")]

McpGrantScopeKind = Literal["wave", "profile", "global"]
GRANT_SCOPE_KINDS: tuple[McpGrantScopeKind, ...] = get_args(McpGrantScopeKind)


class _StrictModel(BaseModel):
    """Base model: forbids unknown keys."""

    model_config = ConfigDict(extra="forbid")


# ---- Core records -----------------------------------------------------------


class Project(_StrictModel):
    """Repo-level project record."""

    code: ProjectCodeStr
    slug: str
    title: str
    description: str | None = None
    domains: list[str]
    default_branch: str
    status: ProjectStatus
    repo_urn: UrnStr


class WorkspaceRepoRef(_StrictModel):
    """Reference to a repo from a workspace index."""

    code: ProjectCodeStr
    path: str
    state_urn: UrnStr
    project_code: ProjectCodeStr
    title: str
    status: ProjectStatus


class WorkspaceIndex(_StrictModel):
    """Workspace-level catalogue of repos."""

    code: ProjectCodeStr
    title: str
    repos: dict[str, WorkspaceRepoRef]
    current_repo_code: str | None = None


class CurrentPointers(_StrictModel):
    """Active lifecycle pointers for the current state."""

    project_code: ProjectCodeStr | None = None
    subproject_id: str | None = None
    phase_id: PhaseIdStr | None = None
    iter_id: IterIdStr | None = None
    active_wave_ids: list[WaveIdStr] = Field(default_factory=list)
    active_session_ids: list[str] = Field(default_factory=list)


class Subproject(_StrictModel):
    """Sub-workstream under a Project."""

    id: ProjectCodeStr
    code: ProjectCodeStr
    slug: str
    title: str
    kind: str
    domains: list[str]
    status: SubprojectStatus
    owner: str | None = None
    goal_ids: list[str] = Field(default_factory=list)


class Goal(_StrictModel):
    """Goal under a project or subproject."""

    id: IdStr
    scope_id: str
    title: str
    summary: str
    status: GoalStatus
    outcome_ids: list[str] = Field(default_factory=list)
    created_at: UtcDatetime
    closed_at: UtcDatetime | None = None


class Outcome(_StrictModel):
    """Quantitative outcome attached to a goal."""

    id: IdStr
    scope_id: str
    metric: str
    threshold: float
    direction: OutcomeDirection
    value: float | None = None
    status: OutcomeStatus
    audit_id: str | None = None
    updated_at: UtcDatetime


class Phase(_StrictModel):
    """Lifecycle phase."""

    id: PhaseIdStr
    scope_id: str
    subproject_id: str | None = None
    title: str
    status: PhaseStatus
    iter_ids: list[IterIdStr] = Field(default_factory=list)
    outcome_ids: list[str] = Field(default_factory=list)
    opened_at: UtcDatetime
    closed_at: UtcDatetime | None = None
    audit_id: str | None = None


class Iter(_StrictModel):
    """Lifecycle iteration."""

    id: IterIdStr
    phase_id: PhaseIdStr
    title: str
    status: IterStatus
    wave_ids: list[WaveIdStr] = Field(default_factory=list)
    estimate_id: str | None = None
    audit_id: str | None = None
    opened_at: UtcDatetime
    closed_at: UtcDatetime | None = None


class Wave(_StrictModel):
    """Atomic execution unit under an iter."""

    id: WaveIdStr
    iter_id: IterIdStr
    title: str
    status: WaveStatus
    deps: list[WaveIdStr] = Field(default_factory=list)
    blocks: list[WaveIdStr] = Field(default_factory=list)
    file_scopes: list[str] = Field(default_factory=list)
    claim_session_id: str | None = None
    worktree_id: str | None = None
    token_budget: Annotated[int | None, Field(ge=0)] = None
    tokens_consumed: Annotated[int, Field(ge=0)] = 0
    commit: str | None = None
    outcome: str | None = None
    opened_at: UtcDatetime
    closed_at: UtcDatetime | None = None


class Hypothesis(_StrictModel):
    """Research hypothesis with confirm/reject thresholds."""

    id: HypothesisIdStr
    scope_id: str
    text: str
    metric: str
    confirm: str
    reject: str
    status: HypothesisStatus
    verdict: HypothesisVerdict | None = None
    audit_id: str | None = None
    source_artifact_id: str | None = None


class Audit(_StrictModel):
    """Audit record (evaluation, ship-gate, incident, review)."""

    id: IdStr
    scope_id: str
    kind: AuditKind
    status: AuditStatus
    report_artifact_id: str | None = None
    check_results: list[Any] = Field(default_factory=list)
    integrity_results: list[Any] = Field(default_factory=list)
    created_at: UtcDatetime
    verdict: AuditVerdict | None = None


class Artifact(_StrictModel):
    """Tracked artifact (file/blob/external)."""

    id: IdStr
    kind: str
    uri: str
    urn: UrnStr
    sha256: str | None = None
    size_bytes: int | None = None
    created_at: UtcDatetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class Decision(_StrictModel):
    """Architectural / process decision."""

    id: IdStr
    scope_id: str
    summary: str
    rationale: str
    alternatives: list[str] = Field(default_factory=list)
    status: DecisionStatus
    created_at: UtcDatetime
    superseded_by: str | None = None


class BacklogItem(_StrictModel):
    """Triaged backlog entry."""

    id: IdStr
    scope_id: str
    title: str
    priority: BacklogPriority
    status: BacklogStatus
    created_at: UtcDatetime
    closed_at: UtcDatetime | None = None
    resolution: str | None = None
    commit: str | None = None


class EstimateSummary(_StrictModel):
    """Latest estimate (full history in store)."""

    id: IdStr
    scope_id: str
    expected_eu: float
    pessimistic_eu: float
    expected_minutes: float
    pessimistic_minutes: float
    display: str
    reference_class: str | None = None
    confidence: Confidence
    current_store_record_id: str
    updated_at: UtcDatetime


class ActualSummary(_StrictModel):
    """Latest actual (segments in store)."""

    id: IdStr
    scope_id: str
    status: ActualStatus
    elapsed_eu: float
    attention_eu: float | None = None
    agent_runtime_eu: float | None = None
    current_store_record_id: str
    updated_at: UtcDatetime


class AgentSession(_StrictModel):
    """Agent work session for provenance."""

    id: IdStr
    role: AgentSessionRole
    runtime: str
    scope_id: str
    status: AgentSessionStatus
    claimed_wave_ids: list[WaveIdStr] = Field(default_factory=list)
    worktree_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    started_at: UtcDatetime
    ended_at: UtcDatetime | None = None
    summary: str | None = None


class WorktreeRecord(_StrictModel):
    """git-worktree provenance record."""

    id: IdStr
    wave_id: WaveIdStr
    branch: str
    path: str
    base_branch: str
    status: WorktreeStatus
    owner_session_id: str | None = None
    created_at: UtcDatetime
    merged_commit: str | None = None


class McpServer(_StrictModel):
    """MCP server config (Eä-managed when owner == 'eawf')."""

    id: IdStr
    owner: str
    command: str
    args: list[str] = Field(default_factory=list)
    env_refs: list[Annotated[str, Field(pattern=r"^\$\{ENV:[A-Z_][A-Z0-9_]*\}$")]] = Field(
        default_factory=list
    )
    risk: McpRisk
    write_capable: bool
    status: McpStatus
    installed_targets: list[str] = Field(default_factory=list)


class McpGrant(_StrictModel):
    """Scope-binding between an MCP server and a wave/profile/global scope.

    A grant is the projection key the dispatcher uses to compute
    ``allowed_tools`` for a runtime: when the dispatched wave matches
    ``scope_kind="wave"`` / ``scope_id=<wave_id>`` (or a broader scope),
    the grant's ``server_id`` contributes ``mcp__<server_id>__*`` to the
    SDK envelope's allowed-tools list. ``server_id`` MUST reference an
    entry in :attr:`State.mcp_servers`; the referential check lives in
    :func:`eawf.validate.invariants.check_mcp_grant_server_ref` and emits
    ``INV.REF.MCP_GRANT_SERVER_MISSING`` when the reference dangles.

    The ``id`` field follows the ``GRANT-<n>`` convention (verified by
    :mod:`tests.unit.test_state_mcp_grant_model`); it is otherwise an
    :data:`IdStr` so the schema-level pattern stays additive and the
    convention can evolve without a schema break.
    """

    id: IdStr
    scope_kind: McpGrantScopeKind
    scope_id: str
    server_id: IdStr
    granted_at: UtcDatetime


class PluginInstall(_StrictModel):
    """Runtime plugin install record (Claude only in v0.1)."""

    id: IdStr
    owner: str
    runtime: str
    scope: str
    target_path: str
    status: PluginInstallStatus
    managed_files: list[str] = Field(default_factory=list)
    installed_at: UtcDatetime
    updated_at: UtcDatetime


class MemorySummary(_StrictModel):
    """Memory entry summary (full text in memory.jsonl).

    ``promoted_to_artifact_id`` mirrors the corresponding field on
    :class:`~eawf.store.kinds.memory.MemoryPayload`: when an entry is
    canonised into a durable artifact (a :class:`Decision` in v0.1) the cache
    surface carries the artifact ID so ``memory list`` / ``memory view`` can
    surface it without replaying the JSONL.
    """

    id: IdStr
    scope_id: str
    summary: str
    confidence: Confidence
    status: MemoryStatus
    store_record_id: str
    review_due: UtcDatetime | None = None
    promoted_to_artifact_id: str | None = None
    tier: MemoryTier = MemoryTier.WORKING


class Incident(_StrictModel):
    """Incident record (full timeline in incidents.jsonl)."""

    id: IdStr
    scope_id: str
    severity: IncidentSeverity
    title: str
    status: IncidentStatus
    opened_at: UtcDatetime
    closed_at: UtcDatetime | None = None
    root_cause: str | None = None
    corrective_action_ids: list[str] = Field(default_factory=list)
    report_artifact_id: str | None = None


class Flow(_StrictModel):
    """Long-running flow with budgets and safe checkpoints."""

    id: IdStr
    goal: str
    budgets: dict[str, Any] = Field(default_factory=dict)
    status: FlowStatus
    current_pointers: CurrentPointers
    policy: dict[str, Any] = Field(default_factory=dict)
    last_safe_checkpoint: str | None = None
    next_action: str | None = None
    started_at: UtcDatetime
    updated_at: UtcDatetime


# ---- State root -------------------------------------------------------------


class State(_StrictModel):
    """Top-level eawf state document."""

    schema_version: Literal["1.0"]
    scope_kind: ScopeKind
    urn: UrnStr
    updated_at: UtcDatetime
    project: Project | None
    current: CurrentPointers
    workspace: WorkspaceIndex | None
    health: Health | None = None
    subprojects: dict[str, Subproject] | None = None
    goals: dict[str, Goal] | None = None
    outcomes: dict[str, Outcome] | None = None
    phases: dict[str, Phase]
    iters: dict[str, Iter]
    waves: dict[str, Wave]
    estimates: dict[str, EstimateSummary] | None = None
    actuals: dict[str, ActualSummary] | None = None
    hypotheses: dict[str, Hypothesis] | None = None
    audits: dict[str, Audit] | None = None
    incidents: dict[str, Incident] | None = None
    artifacts: dict[str, Artifact]
    decisions: dict[str, Decision] = Field(default_factory=dict)
    backlog: dict[str, BacklogItem] | None = None
    agent_sessions: dict[str, AgentSession]
    worktrees: dict[str, WorktreeRecord] | None = None
    mcp_servers: dict[str, McpServer] | None = None
    mcp_grants: dict[str, McpGrant] | None = None
    plugins: dict[str, PluginInstall]
    memory_index: dict[str, MemorySummary] | None = None
    indexes: dict[str, Any]
