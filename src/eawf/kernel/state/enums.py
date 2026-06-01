"""eawf state enumerations.

Every StrEnum used across the state subsystem is defined here.
Canonical reference: docs/enums.md.
"""

from __future__ import annotations

from enum import StrEnum


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    RETIRED = "retired"


class SubprojectStatus(StrEnum):
    ACTIVE = "active"
    PLANNED = "planned"
    DEFERRED = "deferred"
    RETIRED = "retired"


class GoalStatus(StrEnum):
    OPEN = "open"
    ACHIEVED = "achieved"
    ABANDONED = "abandoned"


class OutcomeStatus(StrEnum):
    PENDING = "pending"
    MET = "met"
    MISSED = "missed"
    WAIVED = "waived"


class OutcomeDirection(StrEnum):
    MIN = "min"
    MAX = "max"
    EQUAL = "equal"
    RANGE = "range"


class PhaseStatus(StrEnum):
    PLANNED = "planned"
    ACTIVE = "active"
    CLOSED = "closed"
    ARCHIVED = "archived"


class IterStatus(StrEnum):
    PLANNED = "planned"
    ACTIVE = "active"
    CLOSED = "closed"
    ABANDONED = "abandoned"


class IterTrigger(StrEnum):
    """Why an iter exists — feeds the planned-vs-reactive metric denominator.

    The planned-vs-reactive split classifies an iter's waves by *why the
    iter was opened*, not by the iter's id suffix. The id-suffix heuristic
    (``I01`` planned, ``I02+`` reactive) over-counted reactive work because
    a planned scope expansion opens an ``I02+`` iter yet is not repair /
    reactive — that conflation produced the inflated prior reactive-share
    figure. Tagging the *reason* lets the metric exclude bookkeeping iters
    entirely instead of binning them as reactive.

    Values:
        REACTIVE: Repair cycle or mid-flight scope add (counts toward the
            reactive numerator and the denominator).
        PROACTIVE: Planned-scope delivery, including a deliberate scope
            expansion (counts as planned; in the denominator, not the
            numerator).
        NONE: Pure bookkeeping / no-delivery iter that should not skew the
            ratio — excluded from the metric denominator entirely.
    """

    REACTIVE = "reactive"
    PROACTIVE = "proactive"
    NONE = "none"


class WaveStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    CLOSED = "closed"
    FAILED = "failed"
    ABANDONED = "abandoned"


class EffortBucket(StrEnum):
    XS = "XS"
    S = "S"
    M = "M"
    L = "L"
    XL = "XL"


class HypothesisStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"
    DEFERRED = "deferred"


class HypothesisVerdict(StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class AuditKind(StrEnum):
    EVALUATION = "evaluation"
    SHIP_GATE = "ship-gate"
    INCIDENT = "incident"
    REVIEW = "review"


class AuditStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class AuditVerdict(StrEnum):
    PASS = "pass"
    MINOR = "minor"
    MAJOR = "major"


class DecisionStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVERSED = "reversed"
    OBSOLETE = "obsolete"


class BacklogPriority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class BacklogStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    CLOSED = "closed"
    DEFERRED = "deferred"


class IncidentSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IncidentStatus(StrEnum):
    OPEN = "open"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"
    WONT_FIX = "wont-fix"


class IncidentCause(StrEnum):
    """Closed taxonomy for ``IncidentPayload.cause``.

    Replaces the free-string ``root_cause`` so the observability projection
    can ``GROUP BY cause`` without string normalisation. Grouped by the
    surface the incident originates from. :attr:`LEGACY_FREE_TEXT` is a
    documented sentinel for hypothetical downstream forks with pre-taxonomy
    rows that carried prose; the canonical repo never defaults to it.
    :attr:`UNKNOWN` is the not-yet-classified catchall for new emissions.
    """

    # Runtime / dispatch surface
    RUNTIME_RATE_LIMIT = "runtime_rate_limit"
    RUNTIME_SERVER_ERROR = "runtime_server_error"
    RUNTIME_TIMEOUT = "runtime_timeout"
    RUNTIME_API_ERROR = "runtime_api_error"
    RUNTIME_AUTH_ERROR = "runtime_auth_error"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    RUNTIME_OAUTH_CACHE_STRIPPED = "runtime_oauth_cache_stripped"

    # Daemon / IPC surface
    DAEMON_WAL_RECOVERY = "daemon_wal_recovery"
    DAEMON_SOCKET_BIND = "daemon_socket_bind"
    DAEMON_VERSION_SKEW = "daemon_version_skew"
    DAEMON_SUBPROCESS_OOM = "daemon_subprocess_oom"
    DAEMON_SUBSCRIPTION_DROPPED = "daemon_subscription_dropped"
    DAEMON_LOCK_TIMEOUT = "daemon_lock_timeout"

    # Cache / cost surface
    CACHE_MISLAYER = "cache_mislayer"
    COST_BUDGET_BREACHED = "cost_budget_breached"

    # Session / dispatch surface
    SESSION_HANDLE_PRUNED = "session_handle_pruned"
    SESSION_FAILOVER = "session_failover"

    # Worktree / git surface
    WORKTREE_CHERRY_PICK_CONFLICT = "worktree_cherry_pick_conflict"
    WORKTREE_BRANCH_STALE = "worktree_branch_stale"
    GIT_PUSH_REJECTED = "git_push_rejected"

    # Plugin / sync surface
    PLUGIN_DRIFT = "plugin_drift"

    # Validation / spec surface
    SPEC_VALIDATION_FAILED = "spec_validation_failed"
    STATE_VALIDATION_FAILED = "state_validation_failed"
    AUDIT_FAILED = "audit_failed"

    # External / human surface
    OPERATOR_INTERRUPT = "operator_interrupt"
    EXTERNAL_API_FAILURE = "external_api_failure"

    # Catchall + legacy
    LEGACY_FREE_TEXT = "legacy_free_text"
    UNKNOWN = "unknown"


class FlowStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    BLOCKED = "blocked"
    DONE = "done"
    ABANDONED = "abandoned"
    SUPERSEDED = "superseded"


class ActualStatus(StrEnum):
    PLANNED = "planned"
    ACTIVE = "active"
    DONE = "done"
    INTERRUPTED = "interrupted"
    BLOCKED = "blocked"
    ABANDONED = "abandoned"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class AgentSessionRole(StrEnum):
    RESEARCHER = "researcher"
    PLANNER = "planner"
    EXECUTOR = "executor"
    AUDITOR = "auditor"
    REVIEWER = "reviewer"
    POLISHER = "polisher"
    OPERATOR = "operator"
    DOMAIN_SPECIALIST = "domain-specialist"


class AgentReportVerdict(StrEnum):
    PASS = "pass"
    PASS_WITH_FOLLOWUPS = "pass-with-followups"
    FAIL = "fail"
    BLOCKED = "blocked"


class AgentSessionStatus(StrEnum):
    ACTIVE = "active"
    CHECKPOINTED = "checkpointed"
    CLOSED = "closed"
    STALE = "stale"
    FAILED = "failed"


class DispatchNote(StrEnum):
    """Reason annotating a dispatch transition on a wave's session table.

    Attached to :class:`~eawf.kernel.state.models.DispatchAnnotation` rows in
    ``Wave.dispatch_history``. The values are stable wire identifiers; the
    fresh-dispatch path emits :attr:`FRESH_DISPATCH`, the V8 ``--continue``
    happy path emits :attr:`CONTINUE_FROM_SESSION`, V8 fallback emits
    :attr:`CONTINUE_FAILED_FELL_BACK_TO_FRESH`, V5 runtime fallback emits
    :attr:`SWITCH_ON_ERROR`, and operator-driven manual swaps emit
    :attr:`SWITCH_MANUAL`.
    """

    FRESH_DISPATCH = "fresh_dispatch"
    CONTINUE_FROM_SESSION = "continue_from_session"
    CONTINUE_FAILED_FELL_BACK_TO_FRESH = "continue_failed_fell_back_to_fresh"
    SWITCH_ON_ERROR = "switch_on_error"
    SWITCH_MANUAL = "switch_manual"


class WorktreeStatus(StrEnum):
    ACTIVE = "active"
    CONFLICTED = "conflicted"
    MERGED = "merged"
    ABANDONED = "abandoned"


class McpRisk(StrEnum):
    READ = "read"
    READ_WRITE = "read-write"
    ADMIN = "admin"


class McpStatus(StrEnum):
    NOT_CONFIGURED = "not_configured"
    CONFIGURED = "configured"
    INSTALLED = "installed"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class PluginInstallStatus(StrEnum):
    INSTALLED = "installed"
    DRIFTED = "drifted"
    CONFLICTED = "conflicted"
    DISABLED = "disabled"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    SUPERSEDED = "superseded"
    PRUNED = "pruned"


class MemoryTier(StrEnum):
    """Tiered-memory placement for ``MemorySummary.tier``.

    Mirrors the Letta/Mem0 working/archival/retrieval split:

    - :attr:`WORKING` — hot tier; included in every render-context envelope.
    - :attr:`ARCHIVAL` — cold tier; excluded from the default context window
      but kept queryable for explicit recall (``memory list --tier``).
    - :attr:`RETRIEVAL` — promoted artifacts surfaced on demand (e.g. via
      ``memory render-context --tier``).
    """

    WORKING = "working"
    ARCHIVAL = "archival"
    RETRIEVAL = "retrieval"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SkillEnvelopeStatus(StrEnum):
    OK = "ok"
    NEEDS_USER = "needs_user"
    BLOCKED = "blocked"
    FAILED = "failed"
    PARTIAL = "partial"


class Health(StrEnum):
    OK = "ok"
    NEEDS_SETUP = "needs_setup"
    DEGRADED = "degraded"


class ScopeKind(StrEnum):
    REPO = "repo"
    WORKSPACE = "workspace"


class ScopeTier(StrEnum):
    """Lifecycle-hierarchy tier of a state scope.

    Names the level a ``scope_id`` resolves to in the project lifecycle
    hierarchy. Used by projections (decision graph, backlog filters,
    EU-projection roll-ups) that need to group rows by tier rather
    than by the workspace-vs-repo split that :class:`ScopeKind`
    captures.

    Values:
        WORKSPACE: A workspace-scoped row (one workspace registry).
        REPO: A repo-scoped row (project code).
        PHASE: A phase-scoped row (``P<NN>``).
        ITER: An iter-scoped row (``P<NN>-I<NN>``).
        WAVE: A wave-scoped row (``P<NN>-W<NN>`` / ``P<NN>-I<NN>-W<NN>``).
    """

    WORKSPACE = "workspace"
    REPO = "repo"
    PHASE = "phase"
    ITER = "iter"
    WAVE = "wave"


class StoreKind(StrEnum):
    RESEARCH = "research"
    AUDIT = "audit"
    INCIDENT = "incident"
    ESTIMATE = "estimate"
    ACTUAL = "actual"
    MEMORY = "memory"
    DECISION = "decision"
    EVENT = "event"
    EVIDENCE = "evidence"
    FLOW = "flow"
    RESEARCHER_REPORT = "researcher_report"
    PLANNER_REPORT = "planner_report"
    EXECUTOR_REPORT = "executor_report"
    AUDITOR_REPORT = "auditor_report"
    REVIEWER_REPORT = "reviewer_report"
    POLISHER_REPORT = "polisher_report"
    OPERATOR_REPORT = "operator_report"
    DOMAIN_SPECIALIST_REPORT = "domain_specialist_report"
    SUBSCRIPTION_LAG = "subscription_lag"
    CONFIG_UPDATED = "config_updated"
    REGISTRY_UPDATED = "registry_updated"
    SPEC_UPDATED = "spec_updated"


class ArtifactKind(StrEnum):
    """Closed enumeration of recognised artifact kinds (P14-W11 / B059).

    Existing string ``Artifact.kind`` values migrate onto this enum so
    downstream consumers can switch on a typed value rather than a free
    string. Adding a new kind requires bumping this enum *and* updating
    the URN router so the new kind has a documented routing rule.
    """

    AUDIT_REPORT = "audit_report"
    NOTEBOOK = "notebook"
    DATASET = "dataset"
    MODEL = "model"
    BACKTEST = "backtest"
    STRATEGY = "strategy"
    BINARY = "binary"
    SCENE = "scene"
    PLAYTEST_SESSION = "playtest_session"
    CVE_REF = "cve_ref"
    RESEARCH_BRIEF = "research_brief"
    PLAN_SPEC = "plan_spec"
    AGENT_REPORT = "agent_report"


class SpecStatus(StrEnum):
    """Lifecycle states for Spec entity (C01-IMPL W03 placeholder; full DAG in C03).

    Per c01-foundations §5.4.15: specs live on the filesystem at
    ``.ea/specs/<phase>/[<iter>/]<wave|spec>.md``; no State.specs dict in
    state.json. C03-IMPL wires the full lifecycle — this enum reserves the
    canonical state vocabulary so C02 daemon transitions + C03 spec CLI verbs
    share one source of truth.
    """

    DRAFT = "draft"
    READY = "ready"
    IMPLEMENTED = "implemented"
    ARCHIVED = "archived"
