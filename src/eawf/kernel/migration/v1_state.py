"""The epoch-1 ``state.json`` document model, kept for the migration path.

A native tree holds a generation instead; this model reads and validates
the v1 document an un-migrated tree still carries, so the v1 chain to
schema 1.20 and the epoch-2 cutover can load it. Its row models stay in
:mod:`eawf.kernel.state.models`, which the generation shares.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The bare module import resolves the ``Wave`` forward references before
# ``State`` is built; see the note at the bottom of ``state.models``.
import eawf.kernel.spec.common as _spec_common  # noqa: F401
from eawf.kernel.state.enums import Health, ScopeKind
from eawf.kernel.state.models import (
    ActualSummary,
    AgentSession,
    Artifact,
    Audit,
    BacklogItem,
    Claim,
    CloseAttempt,
    CurrentPointers,
    Decision,
    EstimateSummary,
    FleetRun,
    Goal,
    Hypothesis,
    Incident,
    Iter,
    McpGrant,
    McpServer,
    MemorySummary,
    OpenQuestion,
    Outcome,
    Phase,
    PluginInstall,
    Project,
    Track,
    UrnStr,
    Wave,
    WaveDependencyBarrier,
    WaveDependencyBinding,
    WaveIntegration,
    WorkspaceIndex,
    WorktreeRecord,
    wave_dependency_key,
)
from eawf.kernel.state.types import UtcDatetime
from eawf.runtime.sandbox.policy import SandboxPolicy

__all__ = ["State", "latest_close_attempt"]


class State(BaseModel):
    """Top-level eawf state document.

    ``schema_version`` accepts the full ``"1.0"`` through ``"1.20"`` range so
    an on-disk state written before any bump still re-validates after the
    model advances — the migrate chain rewrites the version string in place,
    but a read of an un-migrated state must never reject. The accepted set
    drives the migrate guard's model-supported max, so the literals move in
    lockstep with the migration steps (``v1_0_to_v1_1`` through
    ``v1_19_to_v1_20``). The ``1.5`` edge is purely additive — it registers
    :attr:`~eawf.kernel.state.enums.ArtifactKind.MATH_EXPLAINER`, an enum
    value no existing state row references, so no historical fact changes.
    The ``1.6`` edge is likewise purely additive — it adds the top-level
    :attr:`dispatch_paused` flag (default ``False``), so a state written
    before the bump re-validates with the flag defaulted and no historical
    fact changes. The ``1.7`` edge retypes
    :attr:`Wave.success_criteria` from ``list[str]`` to
    ``list[CriterionSpec]`` and backfills every legacy string into a
    grandfathered :class:`~eawf.kernel.spec.common.CriterionSpec` row, so an
    un-migrated state with bare strings would reject the typed field — the
    migrate chain rewrites the rows before load. The ``1.8`` edge is purely
    additive — it adds the typed :attr:`Wave.gates` list (default ``[]``), so a
    state written before the bump re-validates with the list defaulted and no
    historical fact changes; the migrate chain backfills ``gates: []`` on every
    wave for an explicit on-disk row. The ``1.9`` edge adds the optional
    :attr:`Wave.runtime_baseline` claim-time telemetry baseline; it backfills
    ``runtime_baseline: None`` on every wave for an explicit on-disk row.
    ``Wave.runtime_latest`` is likewise additive on top of ``1.9`` and defaults
    to ``None``; the epoch-1 capture that wrote it is gone, so it carries only
    counters an epoch-1 tree recorded.

    :attr:`fleet_run` is additive on top of ``1.9`` and defaults to ``None``
    (no active auto-drain loop), so a state written before the field existed
    re-validates with it defaulted and no historical fact changes.

    The ``1.10`` edge is a rename, not an additive field: it renames the
    top-level ``subprojects`` key to :attr:`tracks` and the cursor field
    ``current.subproject_id`` to :attr:`CurrentPointers.track_id`. An
    un-migrated state carrying the old key names rejects under
    ``extra="forbid"``, so the ``v1_9_to_v1_10`` migrate step rewrites both
    names before load. The ``1.11`` edge is purely additive — it adds the
    optional ``harness`` + ``model`` attribution fields to
    :class:`ActualSummary` and :class:`RuntimeBaseline` (inherited by
    :class:`RuntimeLatest`), so EU actuals become calibratable by harness+model.
    Both default to ``None``; the ``v1_10_to_v1_11`` migrate step backfills NULL
    attribution on every actual + runtime-baseline / runtime-latest row for an
    explicit on-disk row, and a state written before the bump re-validates with
    both defaulted and no historical fact changes.

    The ``1.12`` edge is purely additive in the same sense as the ``1.5``
    enum-registration edge: it registers the
    :class:`~eawf.kernel.state.enums.UserDecisionKind` enum and the
    :class:`PauseResolution` typed record (the shared operator-decision shape
    spanning both the ``needs_user`` pause and the fleet-fork surfaces), neither
    of which any existing persisted ``State`` field references -- pause / fork
    resolutions live in the append-only event + evidence stores, not on a
    top-level ``State`` field. The ``v1_11_to_v1_12`` step therefore rewrites
    only the version marker, leaving every row untouched, so a state written
    before the bump re-validates unchanged and the step is a lossless,
    replay-safe round-trip.

    The ``1.15`` edge is purely additive: it adds ``session_id`` to
    :class:`RuntimeBaseline` (inherited by :class:`RuntimeLatest`) and the
    optional :attr:`Wave.runtime_carry` accumulator, which together let a wave
    that spans several sessions sum its per-session runtimes instead of
    differencing counters taken against two different session origins. Both
    default to ``None``; the ``v1_14_to_v1_15`` step backfills them explicitly on
    every wave, and a state written before the bump re-validates with both
    defaulted and no historical fact changes.

    The ``1.16`` edge is purely additive: it adds
    :attr:`RuntimeCarry.counter_resets`, which records how many times the counter
    source reset under a wave. A reset drops the runtime measured before it, so
    the count is the recorded REASON a wave closed with missing runtime -- without
    it, an honest reset is indistinguishable from a capture path that silently did
    nothing. It defaults to ``0``; the ``v1_15_to_v1_16`` step backfills it on
    every existing carry.

    The ``1.17`` edge is purely additive: it adds
    :attr:`RuntimeBaseline.measure_version` (inherited by :class:`RuntimeLatest`),
    which records which definition of the counters produced a snapshot. Counters
    are comparable only against a baseline taken under the same definition, and a
    redefinition that RAISES the figure is otherwise indistinguishable from work.
    It defaults to ``None``; the ``v1_16_to_v1_17`` step backfills it on every
    snapshot, and a null version falls back to the old direction check.

    The ``1.18`` edge is purely additive: it adds
    :attr:`RuntimeBaseline.shared_wave_count` (inherited by
    :class:`RuntimeLatest`), which records how many waves were active when a
    snapshot was captured. One capture is written to every active wave, so without
    the count N concurrent waves each difference -- and each record -- the whole
    session's runtime. It defaults to ``None``; the ``v1_17_to_v1_18`` step
    backfills it on every snapshot, and a null count reads as a divisor of one.

    The ``1.19`` edge is purely additive: it adds
    :attr:`ActualSummary.calibration_excluded`, which marks an actual that was
    measured but does not calibrate anything -- its runtime was re-originated by a
    counter reset, or shared across concurrent waves. Without the flag a consumer
    has no way to tell such a row from a clean one, so the exclusion would live in
    a document rather than in state. It defaults to ``False``; the
    ``v1_18_to_v1_19`` step backfills it on every actual, and a state written
    before the bump re-validates with no historical fact changed.

    The ``1.20`` edge adds sparse operational truth without changing
    :class:`Wave` or :class:`WaveStatus`: immutable integration generations,
    durable close attempts, and explicit dependency barriers/bindings live in
    top-level mappings. The migration creates only empty mappings, so no
    historical Wave gains fabricated integration, close, dependency, or usage
    facts. Missing barrier metadata continues to mean ``closed/closed``.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[
        "1.0",
        "1.1",
        "1.2",
        "1.3",
        "1.4",
        "1.5",
        "1.6",
        "1.7",
        "1.8",
        "1.9",
        "1.10",
        "1.11",
        "1.12",
        "1.13",
        "1.14",
        "1.15",
        "1.16",
        "1.17",
        "1.18",
        "1.19",
        "1.20",
    ]
    scope_kind: ScopeKind
    urn: UrnStr
    updated_at: UtcDatetime
    project: Project | None
    current: CurrentPointers
    workspace: WorkspaceIndex | None
    health: Health | None = None
    dispatch_paused: bool = False
    fleet_run: FleetRun | None = None
    tracks: dict[str, Track] | None = None
    goals: dict[str, Goal] | None = None
    outcomes: dict[str, Outcome] | None = None
    phases: dict[str, Phase]
    iters: dict[str, Iter]
    waves: dict[str, Wave]
    wave_integrations: dict[str, WaveIntegration] = Field(default_factory=dict)
    close_attempts: dict[str, CloseAttempt] = Field(default_factory=dict)
    wave_dependency_barriers: dict[str, WaveDependencyBarrier] = Field(default_factory=dict)
    wave_dependency_bindings: dict[str, WaveDependencyBinding] = Field(default_factory=dict)
    estimates: dict[str, EstimateSummary] | None = None
    actuals: dict[str, ActualSummary] | None = None
    hypotheses: dict[str, Hypothesis] | None = None
    claims: dict[str, Claim] | None = None
    open_questions: dict[str, OpenQuestion] | None = None
    audits: dict[str, Audit] | None = None
    incidents: dict[str, Incident] | None = None
    artifacts: dict[str, Artifact]
    decisions: dict[str, Decision] = Field(default_factory=dict)
    backlog: dict[str, BacklogItem] | None = None
    agent_sessions: dict[str, AgentSession]
    worktrees: dict[str, WorktreeRecord] | None = None
    mcp_servers: dict[str, McpServer] | None = None
    mcp_grants: dict[str, McpGrant] | None = None
    sandbox_policies: dict[str, SandboxPolicy] | None = None
    plugins: dict[str, PluginInstall]
    memory_index: dict[str, MemorySummary] | None = None
    indexes: dict[str, Any]

    @model_validator(mode="after")
    def _validate_sparse_operational_keys(self) -> State:
        """Keep sparse map keys identical to their immutable record identities."""
        for key, integration in self.wave_integrations.items():
            if key != integration.id:
                raise ValueError(
                    f"wave integration key {key!r} does not match id {integration.id!r}"
                )
        for key, attempt in self.close_attempts.items():
            if key != attempt.id:
                raise ValueError(f"close attempt key {key!r} does not match id {attempt.id!r}")
        return self

    @model_validator(mode="after")
    def _validate_dependency_barriers(self) -> State:
        """Require sparse barriers to resolve to declared graph edges."""
        for key, barrier in self.wave_dependency_barriers.items():
            expected = wave_dependency_key(barrier.wave_id, barrier.dep_wave_id)
            if key != expected:
                raise ValueError(
                    f"wave dependency barrier key {key!r} does not match edge {expected!r}"
                )
            wave = self.waves.get(barrier.wave_id)
            if wave is None:
                raise ValueError(
                    f"wave dependency barrier references missing wave {barrier.wave_id!r}"
                )
            if barrier.dep_wave_id not in self.waves:
                raise ValueError(
                    f"wave dependency barrier references missing dep wave {barrier.dep_wave_id!r}"
                )
            if barrier.dep_wave_id not in wave.deps:
                raise ValueError(
                    f"wave dependency barrier edge {expected!r} is not declared in wave deps"
                )
        return self

    @model_validator(mode="after")
    def _validate_dependency_bindings(self) -> State:
        """Require sparse bindings to resolve to declared graph edges."""
        for key, binding in self.wave_dependency_bindings.items():
            expected = wave_dependency_key(binding.wave_id, binding.dep_wave_id)
            if key != expected:
                raise ValueError(
                    f"wave dependency binding key {key!r} does not match edge {expected!r}"
                )
            wave = self.waves.get(binding.wave_id)
            if wave is None:
                raise ValueError(
                    f"wave dependency binding references missing wave {binding.wave_id!r}"
                )
            if binding.dep_wave_id not in self.waves:
                raise ValueError(
                    f"wave dependency binding references missing dep wave {binding.dep_wave_id!r}"
                )
            if binding.dep_wave_id not in wave.deps:
                raise ValueError(
                    f"wave dependency binding edge {expected!r} is not declared in wave deps"
                )
        return self

    @model_validator(mode="after")
    def _validate_open_question_supersession(self) -> State:
        """Refuse an OpenQuestion supersession chain that cycles back on itself.

        A single question's own field validator catches a self-reference;
        it cannot see a longer cycle spread across several questions
        (Q1 superseded by Q2, Q2 superseded by Q1), which only the full
        ledger held here can detect.
        """
        questions = self.open_questions or {}
        for question in questions.values():
            seen = {question.id}
            cursor = question.superseded_by_question_ref
            while cursor is not None:
                if cursor in seen:
                    raise ValueError(
                        f"open question {question.id!r} supersession chain cycles "
                        f"through {cursor!r}"
                    )
                seen.add(cursor)
                successor = questions.get(cursor)
                cursor = successor.superseded_by_question_ref if successor is not None else None
        return self


def latest_close_attempt(state: State, wave_id: str) -> CloseAttempt | None:
    """Return the newest close attempt generation recorded for *wave_id*.

    Generation order is the authority (``requested_at`` then id break ties), so
    the returned row is the one whose remaining budget the daemon is currently
    enforcing. Shared by the daemon close RPCs and the display surfaces so a
    pane never reads a superseded generation's counters.

    Args:
        state: The loaded state.
        wave_id: The wave whose close attempts to scan.

    Returns:
        The newest :class:`CloseAttempt` for *wave_id*, or ``None`` when the wave
        has never been submitted for close.
    """
    rows = [row for row in state.close_attempts.values() if row.wave_id == wave_id]
    if not rows:
        return None
    rows.sort(key=lambda row: (row.generation, row.requested_at, row.id))
    return rows[-1]
