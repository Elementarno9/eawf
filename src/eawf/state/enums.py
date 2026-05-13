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


class AgentSessionStatus(StrEnum):
    ACTIVE = "active"
    CHECKPOINTED = "checkpointed"
    CLOSED = "closed"
    STALE = "stale"
    FAILED = "failed"


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


class StoreKind(StrEnum):
    RESEARCH = "research"
    AUDIT = "audit"
    INCIDENT = "incident"
    ESTIMATE = "estimate"
    ACTUAL = "actual"
    MEMORY = "memory"
    DECISION = "decision"
    EVENT = "event"
    FLOW = "flow"


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
