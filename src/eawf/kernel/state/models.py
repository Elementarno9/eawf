"""Pydantic v2 state models for eawf.

Mirrors ``docs/architecture/state-model.md``. Every model uses
``ConfigDict(extra="forbid")``. ID-shaped fields use regex patterns from
:mod:`eawf.kernel.state.ids`. URN-shaped fields use the ``urn:eawf:v1:`` prefix
constraint. Datetimes are tz-aware UTC (Pydantic accepts ISO-8601 with ``Z``).
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from eawf.kernel.spec.common import CriterionSpec, GateSpec

from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.state.enums import (
    ActualStatus,
    AgentSessionRole,
    AgentSessionStatus,
    AuditKind,
    AuditStatus,
    AuditVerdict,
    BacklogPriority,
    BacklogStatus,
    ClaimStatus,
    Confidence,
    DecisionStatus,
    DispatchNote,
    EffortBucket,
    FlowStatus,
    GoalStatus,
    Health,
    HypothesisStatus,
    HypothesisVerdict,
    IncidentCause,
    IncidentSeverity,
    IncidentStatus,
    IterStatus,
    IterTrigger,
    McpRisk,
    McpStatus,
    MemoryStatus,
    MemoryTier,
    OpenQuestionStatus,
    OutcomeDirection,
    OutcomeStatus,
    PhaseStatus,
    PluginInstallStatus,
    ProjectStatus,
    RiskTier,
    ScopeKind,
    TrackKind,
    TrackStatus,
    Urgency,
    UserDecisionKind,
    WaveStatus,
    WorktreeStatus,
)
from eawf.kernel.state.ids import (
    RE_HYPOTHESIS,
    RE_HYPOTHESIS_SCOPED,
    RE_ITER,
    RE_PHASE,
    RE_PROJECT_CODE,
    RE_WAVE,
    natural_key,
)
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.state.urn import URN_KINDS
from eawf.runtime.sandbox.policy import SandboxPolicy

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
ShaStr = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
#: A ``vMAJOR.MINOR.PATCH`` release label (e.g. ``v0.5.0``). The semver core
#: is required; an optional PEP-440 pre-release segment (``a`` / ``b`` /
#: ``rc`` + a number) is accepted so a pre-release phase band (``v0.5.0rc1``)
#: still validates. This mirrors the grammar :mod:`eawf._version` carries for
#: ``__version__`` and the ``v``-prefixed tag :func:`eawf release tag` writes.
ReleaseStr = Annotated[
    str,
    Field(pattern=r"^v\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?$"),
]

McpGrantScopeKind = Literal["wave", "profile", "global"]
GRANT_SCOPE_KINDS: tuple[McpGrantScopeKind, ...] = get_args(McpGrantScopeKind)


class _StrictModel(BaseModel):
    """Base model: forbids unknown keys."""

    model_config = ConfigDict(extra="forbid")


#: Minimum useful length for a present entity ``description``. A description
#: shorter than this carries no signal beyond the title it accompanies; the
#: floor (Layer 1 of the doc-clarity stack) refuses it at the model boundary.
#: A title-only entity passes ``description=None`` and is grandfathered.
_DESCRIPTION_FLOOR: int = 12


def _is_title_restatement(description: str, title: str) -> bool:
    """Return whether *description* is a near-pure restatement of *title*.

    Both arguments are already case-folded and stripped. The check targets the
    "no signal" description the doc-clarity brief condemns — a description that
    is the title typed again with at most trivial trailing punctuation — while
    deliberately *not* flagging two legitimate shapes:

    - a description that opens with the title and then adds the *why*
      (``"<title> because <reason>"``), which carries new content; and
    - the migration-preserved original of a truncated over-length title (the
      ``v1.0 -> v1.1`` step truncates ``title`` to the 72-char cap and keeps
      the full original in ``description``), which is information-preserving.

    The rule therefore fires only when *description* equals *title*, or equals
    *title* followed solely by whitespace / punctuation (no further word
    characters). A one-character title (``"i"``) cannot match a longer first
    word (``"iter ..."``) because the boundary check requires the title to be
    the whole leading token.
    """
    if description == title:
        return True
    if not description.startswith(title):
        return False
    remainder = description[len(title) :]
    # A genuine restatement adds nothing of substance: the tail after the title
    # is only separators / punctuation. Any alphanumeric char in the tail means
    # the description carries new content and is not a pure restatement.
    return not remainder[:1].isalnum() and not any(ch.isalnum() for ch in remainder)


class _DescribedEntity(_StrictModel):
    """Strict base whose optional ``description`` carries a clarity floor.

    The doc-clarity standard (see
    ``.ea/local/research/2026-05-29-doc-clarity.md``) found that most lifecycle
    and decision entities either carry no description or merely restate the
    title. A field-level ``min_length`` would be a flag-day validation storm on
    the hundreds of already-persisted empty descriptions, so the floor is a
    ``model_validator`` that fires **only when a description is present**:

    - ``description is None`` is grandfathered (a title-only entity is valid).
    - A present description must be at least :data:`_DESCRIPTION_FLOOR`
      non-whitespace characters — shorter is "no signal".
    - A present description must not be a near-pure restatement of the title
      (the title typed again with at most trivial trailing punctuation). A
      description that adds the *why* after the title, or the migration's
      preserved full original of a truncated over-length title, both carry new
      content and pass — see :func:`_is_title_restatement`.

    The base declares **no fields** so a subclass keeps full control of its
    own field declaration order (the validator reads ``self.title`` and
    ``self.description`` by name at validation time). ``Phase`` / ``Iter`` /
    ``Wave`` / ``Decision`` inherit this base; each still declares its own
    ``title`` and ``description`` fields in place. (``BacklogItem`` keeps its
    own field-level ``min_length=1`` floor — it predates this rule and its
    empty case was already closed.)
    """

    @model_validator(mode="after")
    def _description_quality(self) -> _DescribedEntity:
        """Reject a too-short or title-duplicating description; grandfather ``None``.

        Raises:
            ValueError: when ``description`` is present and is either shorter
                than :data:`_DESCRIPTION_FLOOR` non-whitespace characters or a
                near-pure restatement of ``title``.
        """
        description: str | None = getattr(self, "description", None)
        if description is None:
            return self
        stripped = description.strip()
        if len(stripped) < _DESCRIPTION_FLOOR:
            raise ValueError(f"description too short to be useful: {description!r}")
        title_stem = str(getattr(self, "title", "")).rstrip(". ").casefold()
        if title_stem and _is_title_restatement(stripped.casefold(), title_stem):
            raise ValueError("description merely repeats the title; describe the why")
        return self


# ---- Core records -----------------------------------------------------------


class Project(_StrictModel):
    """Repo-level project record.

    The ``weekly_eu_target`` field is the operator-set weekly EU budget; when
    set, the TUI footer renders a ``weekly burn: <consumed_eu> / <target_eu>``
    rollup over the rolling 7-day window of :class:`ActualSummary.updated_at`.
    The field is strictly optional (default ``None``) so adding it does not
    bump ``schema_version`` — projects without a target render no burn line.

    ``track_ids`` is the Project end of the ``Project -> Track`` containment
    edge: each id names a :class:`Track` the project owns, completing the
    ``Project -> Track -> Goal -> Outcome`` containment chain (the remaining
    edges live on :attr:`Track.goal_ids` and :attr:`Goal.outcome_ids`). The
    list defaults empty so a project without any track loads unchanged and
    adding the field stays additive under ``extra="forbid"``.
    """

    code: ProjectCodeStr
    slug: str
    title: str
    description: str | None = None
    domains: list[str]
    default_branch: str
    status: ProjectStatus
    repo_urn: UrnStr
    weekly_eu_target: float | None = None
    track_ids: list[str] = Field(default_factory=list)


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
    track_id: str | None = None
    phase_id: PhaseIdStr | None = None
    iter_id: IterIdStr | None = None
    active_wave_ids: list[WaveIdStr] = Field(default_factory=list)
    active_session_ids: list[str] = Field(default_factory=list)


class Track(_StrictModel):
    """Durable per-workstream vehicle under a Project.

    A Track is the lifecycle vehicle a Project hangs scoped Goals and
    Outcomes off (e.g. a quant-research strategy, an ML model, a
    reverse-engineering target). Its :attr:`kind` is a closed
    :class:`~eawf.kernel.state.enums.TrackKind` so an unknown kind fails as a
    :class:`pydantic.ValidationError` at the ingestion boundary rather than
    flowing downstream as a free string; the kind selects which
    :class:`~eawf.platform.profiles.models.TrackKindSpec` parametrizes the
    track's noun, status lifecycle, outcome template, and overview view.

    :attr:`status` carries the Track lifecycle as a closed
    :class:`~eawf.kernel.state.enums.TrackStatus`, mirroring how
    :attr:`Phase.status` carries the phase lifecycle: a Track opens
    :attr:`~eawf.kernel.state.enums.TrackStatus.PLANNED`, advances to
    :attr:`~eawf.kernel.state.enums.TrackStatus.ACTIVE` when focused, and
    settles on a terminal value. The lifecycle is dormant: no lifecycle
    step (phase / iter / wave open or close) requires a Track, so the
    ``track.add`` / ``track.switch`` mutator pair sets :attr:`status` and the
    :attr:`CurrentPointers.track_id` cursor without gating any other
    transition. :attr:`goal_ids` is the Track end of the ``Track -> Goal``
    containment edge (the project end is :attr:`Project.track_ids`).

    :attr:`scope_globs` is the Track's *declared* file scope -- the glob
    patterns (e.g. ``src/strategies/collar/**``) that bound the files a Track's
    waves are expected to touch. The list lets a wave's :attr:`Wave.file_scopes`
    be checked for containment so an out-of-scope edit is flagged rather than
    silently assumed in-scope. It defaults empty (no declared scope, so
    containment cannot be enforced) and adding it stays additive under
    ``extra="forbid"``.
    """

    id: ProjectCodeStr
    code: ProjectCodeStr
    slug: str
    title: str
    kind: TrackKind
    domains: list[str]
    status: TrackStatus
    owner: str | None = None
    goal_ids: list[str] = Field(default_factory=list)
    scope_globs: list[str] = Field(default_factory=list)


class Goal(_StrictModel):
    """Goal under a project or track."""

    id: IdStr
    scope_id: str
    title: str
    summary: str
    status: GoalStatus
    outcome_ids: list[str] = Field(default_factory=list)
    created_at: UtcDatetime
    closed_at: UtcDatetime | None = None


class Outcome(_StrictModel):
    """Quantitative outcome attached to a goal.

    An outcome is *measured* once :attr:`sample` is recorded: ``sample`` is the
    latest observed value of :attr:`metric`, and :attr:`best_value` carries the
    best value seen so far so the comparator can tell a fresh miss apart from a
    regression off a previously-achieved best (see
    :func:`eawf.workflow.evidence.outcome.compute_outcome_status`). The derived
    :attr:`status` is never hand-set on a measured outcome -- the comparator
    derives it from the threshold, the sample, and the favorable
    :attr:`direction`.

    :attr:`evidence_refs` carries the repo-relative paths / Eawf URNs / external
    URLs that ratify a measured status claim. The model-level invariant forbids
    a measured outcome (terminal :attr:`status` with a recorded :attr:`sample`)
    from carrying an empty ``evidence_refs`` list, so a status claim cannot
    fabricate its own evidence at the ingestion boundary.
    """

    id: IdStr
    scope_id: str
    metric: str
    threshold: float
    direction: OutcomeDirection
    value: float | None = None
    sample: float | None = None
    best_value: float | None = None
    status: OutcomeStatus
    audit_id: str | None = None
    evidence_refs: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _measured_outcome_requires_evidence(self) -> Outcome:
        """Reject a measured outcome whose status claim cites no evidence.

        A measured outcome carries a recorded :attr:`sample` and a terminal
        :attr:`status` (anything past :attr:`OutcomeStatus.PENDING`); such a
        claim MUST resolve to at least one evidence ref so the status cannot be
        asserted without backing.

        Raises:
            ValueError: When ``status`` is terminal and ``sample`` is recorded
                but ``evidence_refs`` is empty.
        """
        measured = self.sample is not None and self.status is not OutcomeStatus.PENDING
        if measured and not self.evidence_refs:
            raise ValueError(f"measured outcome {self.id!r} has no resolving evidence ref")
        return self


class Phase(_DescribedEntity):
    """Lifecycle phase.

    The ``release`` field bands the phase under a ``vMAJOR.MINOR.PATCH``
    release version in the rendered roadmap. It is strictly optional
    (default ``None``) so adding it is additive -- a phase without a
    release loads unchanged and renders under the "Unreleased" band,
    and an old ``state.json`` that predates the field stays valid under
    ``extra="forbid"`` because the missing key takes the default.

    The optional ``description`` carries a clarity floor when present (see
    :class:`_DescribedEntity`): grandfathered when ``None``, else floored at
    a useful length and rejected when it merely restates the title.
    """

    id: PhaseIdStr
    scope_id: str
    track_id: str | None = None
    title: Annotated[str, Field(min_length=1, max_length=72)]
    description: Annotated[str, Field(max_length=500)] | None = None
    status: PhaseStatus
    iter_ids: list[IterIdStr] = Field(default_factory=list)
    outcome_ids: list[str] = Field(default_factory=list)
    depends_on: list[PhaseIdStr] = Field(default_factory=list)
    source_brief_ids: list[str] = Field(default_factory=list)
    release: ReleaseStr | None = None
    opened_at: UtcDatetime
    closed_at: UtcDatetime | None = None
    audit_id: str | None = None
    intent: IntentBrief | None = None


class Iter(_DescribedEntity):
    """Lifecycle iteration.

    ``trigger`` records *why* the iter was opened so the planned-vs-reactive
    metric can classify its waves by intent rather than by the ``I##`` id
    suffix. It defaults to :attr:`IterTrigger.NONE` so the field is additive
    (pre-trigger states and in-code constructors stay valid); the lifecycle
    surface that opens iters sets the real value, and :class:`IterTrigger`
    documents how each value lands in the metric denominator.

    ``candidate_tag`` carries a proposed ``vMAJOR.MINOR.PATCH`` release tag
    for the iter -- the version an operator pencils in for the iter's
    deliverable before the phase-close release pre-flight pins it. It is
    strictly optional (default ``None``) so adding it is additive: an iter
    without a candidate tag loads unchanged and a pre-1.4 ``state.json``
    stays valid under ``extra="forbid"`` because the missing key takes the
    default.

    The optional ``description`` carries a clarity floor when present (see
    :class:`_DescribedEntity`): grandfathered when ``None``, else floored at
    a useful length and rejected when it merely restates the title.
    """

    id: IterIdStr
    phase_id: PhaseIdStr
    title: Annotated[str, Field(min_length=1, max_length=72)]
    description: Annotated[str, Field(max_length=500)] | None = None
    status: IterStatus
    trigger: IterTrigger = IterTrigger.NONE
    candidate_tag: ReleaseStr | None = None
    wave_ids: list[WaveIdStr] = Field(default_factory=list)
    estimate_id: str | None = None
    audit_id: str | None = None
    opened_at: UtcDatetime
    closed_at: UtcDatetime | None = None
    intent: IntentBrief | None = None

    @field_validator("wave_ids")
    @classmethod
    def _normalize_wave_ids(cls, wave_ids: list[WaveIdStr]) -> list[WaveIdStr]:
        """Sort ``wave_ids`` into ascending natural-id order on every validation.

        Stored iters from before this rule kept ``wave_ids`` in append order,
        which diverges from ascending ``W##`` order whenever a wave was claimed
        out-of-order or a reactive wave landed after a higher-numbered sibling.
        Normalizing here is self-healing: loading then persisting state rewrites
        a divergent iter to ascending order with no separate migration step, and
        every render site that iterates ``wave_ids`` gets ascending order for
        free. The sort is idempotent (an already-sorted list is unchanged) and
        total (membership is preserved; only the order changes).
        """
        return sorted(wave_ids, key=natural_key)


class SessionAttempt(_StrictModel):
    """One runtime subprocess attempt against a wave.

    Captures the daemon-issued bookkeeping for a single
    ``agent.dispatch`` invocation. ``session_log_handle`` is an
    **opaque** handle (blob-URN or daemon-side index key) — never a
    filesystem path. The daemon's in-process map (see
    :func:`eawf.runtime.daemon.session.register_session_log` /
    :func:`eawf.runtime.daemon.session.resolve_session_log`) is the only place
    real paths live, satisfying AGENTS rule 16 (secrets / PII hygiene).

    Attributes:
        attempt: 1-based attempt counter under the parent wave.
        runtime: Runtime adapter id (e.g. ``"claude-code"`` /
            ``"codex"`` / ``"opencode"``).
        session_id: Runtime-specific session identifier (typically a
            UUID emitted by the runtime).
        session_log_handle: Opaque handle resolvable by the daemon to
            a real session-log path. Format is daemon-internal; the
            fresh-path skeleton uses
            ``urn:eawf:v1:session-log:<runtime>:<uuid>``.
        started_at: When the subprocess started.
        ended_at: When the subprocess exited; ``None`` while running.
        exit_status: Subprocess exit code; ``None`` while running.
        subprocess_pid: PID at spawn time; ``None`` when no subprocess
            is addressable for this attempt.
        cache_creation_input_tokens: Anthropic prompt-cache write
            tokens charged on this attempt (optional).
        cache_read_input_tokens: Anthropic prompt-cache read tokens
            charged on this attempt (optional).
        input_tokens: Non-cached input tokens (optional).
        output_tokens: Output tokens (optional).
    """

    attempt: Annotated[int, Field(ge=1)]
    runtime: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    session_log_handle: str = Field(min_length=1)
    started_at: UtcDatetime
    ended_at: UtcDatetime | None = None
    exit_status: int | None = None
    subprocess_pid: Annotated[int, Field(ge=1)] | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class DispatchAnnotation(_StrictModel):
    """One dispatch-history row attached to a wave.

    The annotation captures the *transition* — fresh dispatch, continue
    from session, continue-failed fallback, V5 runtime switch, or
    operator-driven manual switch — that produced the matching
    :class:`SessionAttempt` row in ``Wave.sessions``.

    Attributes:
        attempt: Attempt number this annotation belongs to.
        note: Why the dispatch happened (per :class:`DispatchNote`).
        runtime_from: Runtime id of the previous attempt, when this
            transition swapped runtimes. ``None`` on first dispatch.
        runtime_to: Runtime id of the new attempt.
        occurred_at: Wall-clock timestamp of the transition.
        reason: Free-form scrubbed context the operator or runtime
            attached at dispatch time.
    """

    attempt: Annotated[int, Field(ge=1)]
    note: DispatchNote
    runtime_from: str | None = None
    runtime_to: str | None = None
    occurred_at: UtcDatetime
    reason: str | None = None


class RuntimeBaseline(_StrictModel):
    """Cumulative runtime counters captured when a wave is first claimed.

    The baseline is the "before" snapshot used by later close-time telemetry
    readers to subtract already-spent runtime/cost/token counters from the
    same runtime sidecar. Numeric fields are optional because not every runtime
    reports every counter; ``captured_at`` is required so consumers can order
    and audit the snapshot.

    ``harness`` and ``model`` carry the attribution that makes the captured
    counters calibratable: the agent harness id (e.g. ``"claude-code"``) and the
    model id (e.g. ``"claude-opus-4-1"``) the runtime billed against. Both are
    optional because not every runtime reports them, and a state written before
    the v1.11 bump re-validates with both defaulted to ``None``.
    """

    api_duration_ms: Annotated[int, Field(ge=0)] | None = None
    total_duration_ms: Annotated[int, Field(ge=0)] | None = None
    cost_usd: Annotated[float, Field(ge=0.0)] | None = None
    input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)] | None = None
    cache_creation_input_tokens: Annotated[int, Field(ge=0)] | None = None
    cache_read_input_tokens: Annotated[int, Field(ge=0)] | None = None
    harness: str | None = None
    model: str | None = None
    captured_at: UtcDatetime


class RuntimeLatest(RuntimeBaseline):
    """Latest cumulative runtime counters captured while a wave is active."""


class Wave(_DescribedEntity):
    """Atomic execution unit under an iter.

    ``opened_at`` records *plan/creation* time (stamped when the wave row
    is inserted), while ``claimed_at`` records *work-start* time (stamped
    on the first claim). The two diverge under plan-all-then-execute,
    where creation can precede the claim by hours, so elapsed-clock
    consumers anchor on ``claimed_at`` (not ``opened_at``) and render no
    clock at all while ``claimed_at`` is ``None`` -- a wave that has not
    been claimed has no work-start fact to elapse from. The field is
    additive + optional so on-disk state written before the v1.3 schema
    bump re-validates unchanged.

    The optional ``description`` carries a clarity floor when present (see
    :class:`_DescribedEntity`): grandfathered when ``None``, else floored at
    a useful length and rejected when it merely restates the title.
    """

    id: WaveIdStr
    iter_id: IterIdStr
    title: Annotated[str, Field(min_length=1, max_length=72)]
    description: Annotated[str, Field(max_length=500)] | None = None
    status: WaveStatus
    deps: list[WaveIdStr] = Field(default_factory=list)
    blocks: list[WaveIdStr] = Field(default_factory=list)
    file_scopes: list[str] = Field(default_factory=list)
    success_criteria: list[CriterionSpec] = Field(default_factory=list)
    gates: list[GateSpec] = Field(default_factory=list)
    agent_role: AgentSessionRole | None = None
    effort_bucket: EffortBucket | None = None
    claim_session_id: str | None = None
    worktree_id: str | None = None
    token_budget: Annotated[int | None, Field(ge=0)] = None
    tokens_consumed: Annotated[int, Field(ge=0)] = 0
    outcome: str | None = None
    commit: ShaStr | None = None
    opened_at: UtcDatetime
    claimed_at: UtcDatetime | None = None
    runtime_baseline: RuntimeBaseline | None = None
    runtime_latest: RuntimeLatest | None = None
    closed_at: UtcDatetime | None = None
    sessions: dict[int, SessionAttempt] = Field(default_factory=dict)
    runtime_preference: list[str] | None = None
    dispatch_history: list[DispatchAnnotation] = Field(default_factory=list)
    intent: IntentBrief | None = None


class Hypothesis(_StrictModel):
    """Research hypothesis with confirm/reject thresholds."""

    id: HypothesisIdStr
    scope_id: str
    title: Annotated[str, Field(min_length=1, max_length=72)]
    description: Annotated[str, Field(max_length=500)] | None = None
    metric: str
    confirm: str
    reject: str
    status: HypothesisStatus
    verdict: HypothesisVerdict | None = None
    audit_id: str | None = None
    source_artifact_id: str | None = None


class Claim(_StrictModel):
    """A single typed claim in a research-campaign ledger.

    The Claim ledger is the prerequisite of the pruning pass and the
    SaturationReport: a campaign accumulates claims as it surveys sources,
    and the ledger lets a downstream pass detect saturation (no new claims
    arriving) and prune subsumed rows. Each claim names a single assertion
    (``title``) with optional long-form context (``description``) and carries
    the ``evidence_refs`` that ratify it — repo-relative paths, Eawf URNs, or
    external URLs — so the EviBound scorer can score "every claim resolves"
    over the ledger.

    The field shape mirrors the sibling research entity
    :class:`Hypothesis`: a bounded imperative ``title`` (no trailing period),
    a 500-char optional ``description``, a ``scope_id`` binding the claim to
    its campaign scope, and a closed :class:`ClaimStatus` lifecycle.
    ``answers_question_id`` back-links a claim to the separate
    :class:`OpenQuestion` entity it resolves; it defaults to ``None`` for a
    free-standing claim.

    Attributes:
        id: Stable claim id (``IdStr`` — non-empty, no whitespace).
        scope_id: Campaign / research scope the claim belongs to.
        title: Imperative noun-phrase stating the claim, bounded at 72
            characters with no trailing period (entity-title convention).
        description: Optional long-form statement bounded at 500 characters.
        status: Closed :class:`ClaimStatus` lifecycle position.
        evidence_refs: Repo-relative / URN / external-URL strings that
            ratify the claim. Default empty so a freshly logged claim whose
            evidence is not yet attached still validates (the EviBound gate
            scores resolution downstream, not at ingestion).
        source_artifact_id: Optional id of the artifact (notebook, brief,
            dataset) the claim was distilled from.
        answers_question_id: Optional :class:`OpenQuestion` id this claim
            answers; ``None`` for a claim not tied to a tracked question.
        created_at: When the claim was logged into the ledger.
        superseded_by: Id of the later claim that subsumes this one when
            :attr:`status` is :attr:`ClaimStatus.SUPERSEDED`; ``None``
            otherwise.
    """

    id: IdStr
    scope_id: str
    title: Annotated[str, Field(min_length=1, max_length=72)]
    description: Annotated[str, Field(max_length=500)] | None = None
    status: ClaimStatus
    evidence_refs: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    source_artifact_id: str | None = None
    answers_question_id: str | None = None
    created_at: UtcDatetime
    superseded_by: str | None = None


class OpenQuestion(_StrictModel):
    """An unresolved research question tracked as its own first-class entity.

    An open question is NOT folded into :class:`Claim`: a question is a gap to
    close, a claim is an assertion that closes it, and conflating the two
    loses the distinction the research-campaign control plane needs (a
    question can outlive many candidate claims, and an operator can add a
    question mid-run through the input channel). The ``BLOCKED`` status feeds
    the balanced-autonomy interrupt: a blocking question is the only kind that
    raises to the operator.

    The field shape mirrors the sibling research entities (:class:`Claim` /
    :class:`Hypothesis`): a bounded imperative ``title`` with no trailing
    period, a 500-char optional ``description``, a ``scope_id`` binding the
    question to its campaign, and a closed :class:`OpenQuestionStatus`.

    Attributes:
        id: Stable question id (``IdStr`` — non-empty, no whitespace).
        scope_id: Campaign / research scope the question belongs to.
        title: Imperative noun-phrase stating the question, bounded at 72
            characters with no trailing period (entity-title convention).
        description: Optional long-form framing bounded at 500 characters.
        status: Closed :class:`OpenQuestionStatus` lifecycle position.
        blocking: Whether an unanswered question gates further campaign work;
            a ``True`` value is what the balanced-autonomy interrupt raises to
            the operator. Defaults to ``False`` (advisory question).
        urgency: Shared :class:`~eawf.kernel.state.enums.Urgency` ladder ranking
            how soon the operator should look at the question. The same enum
            ranks needs_user pauses and the attention feed, so an open question
            sorts against pauses on one comparable scale. Defaults to
            :attr:`~eawf.kernel.state.enums.Urgency.NORMAL` so the field is
            additive (pre-urgency states and in-code constructors stay valid).
        answered_by_claim_id: Optional :class:`Claim` id that resolved the
            question when :attr:`status` is
            :attr:`OpenQuestionStatus.ANSWERED`; ``None`` otherwise.
        created_at: When the question was opened.
        resolved_at: When the question reached a terminal state
            (``ANSWERED`` / ``DROPPED``); ``None`` while still open.
    """

    id: IdStr
    scope_id: str
    title: Annotated[str, Field(min_length=1, max_length=72)]
    description: Annotated[str, Field(max_length=500)] | None = None
    status: OpenQuestionStatus
    blocking: bool = False
    urgency: Urgency = Urgency.NORMAL
    answered_by_claim_id: str | None = None
    created_at: UtcDatetime
    resolved_at: UtcDatetime | None = None


class Round(_StrictModel):
    """One executed research-campaign round, projected from its store record.

    A :class:`Round` is the typed view the Research board's tree + RUN band and
    ``eawf status`` render: one row per round the campaign run actually executed
    (the daemon persists each round to the append-only ``research_round`` store).
    It is the real entity that retires the board's earlier *synthetic* round
    node -- the board derived a single placeholder round from the question
    ledger because the multi-round runner was idle, but a run now persists one
    record per round, so the board renders the real rounds instead.

    This is a projection model, not a persisted ``State`` field: a round lives
    in the append-only round store (like a Claim's evidence lives in the store),
    so adding it needs no ``State`` schema bump. The board / status load the
    store records and re-validate them into this typed view.

    Attributes:
        campaign_id: The campaign the round belongs to.
        round_number: The 1-based round index (the order it executed in).
        finding_count: How many findings lines the round's researchers
            returned (the round's productivity figure).
        claim_count: How many Claim rows the round-end reconcile wrote.
        saturated: Whether this round was the one the loop halted on because
            the campaign converged.
        checkpoint: Whether the round coincided with an operator-review
            checkpoint (per the run's checkpoint policy).
        steer_notes: The operator steer / override notes folded into the round
            -- the between-rounds feedback that shaped its dispatch set.
    """

    campaign_id: str = Field(min_length=1)
    round_number: Annotated[int, Field(ge=1)]
    finding_count: Annotated[int, Field(ge=0)] = 0
    claim_count: Annotated[int, Field(ge=0)] = 0
    saturated: bool = False
    checkpoint: bool = False
    steer_notes: list[str] = Field(default_factory=list)


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
    """Tracked artifact (file/blob/external).

    ``kind`` is a free-form string in v0.3 but the canonical vocabulary is
    pinned by :class:`~eawf.kernel.state.enums.ArtifactKind` (P14-W11 / B059).
    Callers that already speak the canonical vocabulary can pass an
    :class:`ArtifactKind` member directly; Pydantic coerces it to the
    underlying string value on serialisation. The strict-enum tightening
    lands in v0.4 once every internal caller is migrated.
    """

    id: IdStr
    kind: str
    uri: str
    urn: UrnStr
    sha256: str | None = None
    size_bytes: int | None = None
    created_at: UtcDatetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class Decision(_DescribedEntity):
    """Architectural / process decision.

    The optional ``description`` carries a clarity floor when present (see
    :class:`_DescribedEntity`): grandfathered when ``None``, else floored at
    a useful length and rejected when it merely restates the title.
    """

    id: IdStr
    scope_id: str
    title: Annotated[str, Field(min_length=1, max_length=72)]
    description: Annotated[str, Field(max_length=500)] | None = None
    rationale: str
    alternatives: list[str] = Field(default_factory=list)
    # Nygard-ADR "Consequences" — what becomes easier/harder after the
    # decision. Default-empty so pre-existing decision rows (written before
    # the field existed) still validate without a schema bump.
    consequences: list[str] = Field(default_factory=list)
    status: DecisionStatus
    created_at: UtcDatetime
    superseded_by: str | None = None
    # Stamp set when :func:`apply_decision_obsolete` flips ``status`` to
    # :attr:`DecisionStatus.OBSOLETE`. Default-``None`` so pre-existing
    # decision rows (written before the field existed) still validate
    # without a schema bump.
    obsoleted_at: UtcDatetime | None = None


class BacklogItem(_StrictModel):
    """Triaged backlog entry.

    The optional :attr:`description` carries the long-form purpose. When
    present, it is bounded at 500 characters and rejected when empty:
    a zero-length description is the W56 audit's "no signal" trap (a
    surviving item without a problem statement is indistinguishable
    from one that was never triaged), so the model refuses
    ``description=""`` at ingestion. ``None`` remains valid for a
    title-only item; pass ``--description`` only when the field has
    substantive prose.
    """

    id: IdStr
    scope_id: str
    title: Annotated[str, Field(min_length=1, max_length=72)]
    description: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    priority: BacklogPriority
    status: BacklogStatus
    created_at: UtcDatetime
    closed_at: UtcDatetime | None = None
    resolution: str | None = None
    commit: str | None = None
    intent: IntentBrief | None = None


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
    """Latest actual (segments in store).

    ``attention_eu`` may be auto-derived at wave close from telemetry
    session ``duration_ms`` when no operator-authored actual already
    exists. ``actual_tokens`` mirrors :attr:`Wave.tokens_consumed` so the
    M26 variance / cost view has the close-time token tally without
    re-reading the wave record. ``actual_cost_usd`` is the per-token cost
    rollup; v0.4 leaves it at ``0.0`` until a per-model rate table wires
    in (see :func:`eawf.workflow.lifecycle.wave.close_wave`).
    Both fields default to ``0`` / ``0.0`` so existing on-disk rows
    stay valid without a schema bump.

    ``harness`` and ``model`` carry the attribution that makes the recorded
    actual calibratable by harness+model: the agent harness id (e.g.
    ``"claude-code"``) and the model id the runtime billed against. Both are
    optional because not every recorded actual knows them, and a state written
    before the v1.11 bump re-validates with both defaulted to ``None``.
    """

    id: IdStr
    scope_id: str
    status: ActualStatus
    elapsed_eu: float
    attention_eu: float | None = None
    agent_runtime_eu: float | None = None
    actual_tokens: Annotated[int, Field(ge=0)] = 0
    actual_cost_usd: Annotated[float, Field(ge=0.0)] = 0.0
    harness: str | None = None
    model: str | None = None
    current_store_record_id: str
    updated_at: UtcDatetime


class AgentSession(_StrictModel):
    """Agent work session for provenance.

    The ``agent_principal_id`` field is a v0.3-v0.5 placeholder mirroring
    :attr:`eawf.kernel.store.kinds.event.EventPayload.actor_principal_id`:
    sessions MAY carry the :class:`Principal` id of the agent that
    drove them when known, but the load-bearing identity for backward
    compatibility remains :attr:`runtime`. The v0.5+ governance phase
    populates this for every dispatched session once the per-repo
    Principal database lands; until then existing on-disk rows stay
    valid (default ``None`` is replay-safe / additive — no schema
    bump).
    """

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
    agent_principal_id: PrincipalIdStr | None = None


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
    :func:`eawf.kernel.validate.invariants.check_mcp_grant_server_ref` and emits
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
    scope_id: str
    target_path: str
    status: PluginInstallStatus
    managed_files: list[str] = Field(default_factory=list)
    installed_at: UtcDatetime
    updated_at: UtcDatetime


class MemorySummary(_StrictModel):
    """Memory entry summary (full text in memory.jsonl).

    ``promoted_to_artifact_id`` mirrors the corresponding field on
    :class:`~eawf.kernel.store.kinds.memory.MemoryPayload`: when an entry is
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
    title: Annotated[str, Field(min_length=1, max_length=72)]
    description: Annotated[str, Field(max_length=500)] | None = None
    status: IncidentStatus
    opened_at: UtcDatetime
    closed_at: UtcDatetime | None = None
    root_cause: str | None = None
    #: Typed cause taxonomy (V7). ``UNKNOWN`` until classified at close via
    #: ``eawf incident close --cause``; the free-text ``root_cause`` carries
    #: the operator prose, ``cause`` carries the ``GROUP BY``-able category.
    cause: IncidentCause = IncidentCause.UNKNOWN
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


PrincipalIdStr = Annotated[str, Field(pattern=r"^u-[0-9a-f]{8}$")]


class Principal(_StrictModel):
    """Minimum Principal identity record (v0.3-v0.5 placeholder; full enforcement v0.5+).

    Per c01-foundations §5.3.19: the field shape stabilises now so query-side
    code + telemetry projection can be typed today, even though the v0.5+
    governance phase owns full enforcement (capabilities, ed25519 signatures,
    per-repo principal database).

    The ``cli`` kind is the legacy CLI-dispatch sentinel — every
    ``EventPayload.actor == "cli"`` row maps onto a synthetic
    ``Principal(kind="cli")`` until v0.5+ migration assigns operator/agent
    principal ids.

    Attributes:
        id: Principal identifier matching ``^u-[0-9a-f]{8}$``.
        kind: Identity flavour — ``operator`` (human driving the CLI),
            ``agent`` (a runtime-backed subagent), or ``cli`` (legacy
            CLI-dispatch sentinel).
        display_name: Short scrubbed label for the principal (no PII).
        runtime: Runtime adapter id when ``kind == "agent"`` — e.g.
            ``"claude"`` / ``"codex"`` / ``"opencode"``. Optional and
            defaulted to ``None`` so existing rows still validate without
            backfill; the v0.5+ governance migration populates it for
            every agent-kind principal. Operator / cli kinds leave it
            ``None``.
    """

    id: PrincipalIdStr
    kind: Literal["operator", "agent", "cli"]
    display_name: str
    runtime: str | None = None


# ---- Fleet auto-drain loop --------------------------------------------------


class FleetRunState(StrEnum):
    """Run-state of the daemon-owned fleet auto-drain loop.

    The closed state machine the loop advances through:

    - ``IDLE`` -- armed but holding (e.g. ``state.dispatch_paused`` is set on
      arm, so no wave is claimed); the loop has not begun draining.
    - ``DRAINING`` -- actively claiming + dispatching the ready frontier into
      lanes up to the concurrency cap, advancing as lanes free.
    - ``PAUSED`` -- an operator pause-all stopped further claims; in-flight
      lanes are left intact and a resume returns the run to ``DRAINING``.
    - ``HALTED`` -- a halt-all blocked new claims but lets in-flight lanes
      finish; distinct from a kill-all (which reaps in-flight work).
    - ``DONE`` -- a terminal stop (frontier drained empty, or a convergence
      criterion such as K consecutive clean rounds was met).
    """

    IDLE = "idle"
    DRAINING = "draining"
    PAUSED = "paused"
    HALTED = "halted"
    DONE = "done"


class FleetTerminalReason(StrEnum):
    """Why a :class:`FleetRun` reached :data:`FleetRunState.DONE`.

    - ``drained`` -- the ready frontier emptied, so no further wave can be
      claimed (the drain-to-empty stop).
    - ``converged`` -- a convergence criterion (e.g. ``kclean`` -- K
      consecutive rounds with zero progress) was met before the frontier
      emptied, so the loop stopped early.
    - ``budget`` -- a spend cap (EU / USD / waves) fired, so the loop stopped
      claiming further waves. Under the graceful-drain default the in-flight
      lanes finish before the run ends; under the armed hard-halt toggle the
      in-flight lanes are killed at the cap (DL-4 budget HALT teeth).
    """

    DRAINED = "drained"
    CONVERGED = "converged"
    BUDGET = "budget"


class FleetLane(_StrictModel):
    """One in-flight dispatch slot in the fleet auto-drain loop.

    A lane binds a claimed + dispatched wave to its dispatch session so the
    loop can watch it to completion and free the slot for the next frontier
    wave. The loop holds at most ``concurrency`` lanes at once.

    The ``(wave_id, attempt)`` pair plus ``pgid`` form the live per-lane
    process registry that the kill (DL-3) and reattach (DL-8) paths read: a
    lane resolves to a real OS process group rather than a bare label. A lane
    whose spawn returned no pid carries ``pgid=None`` and is reported
    ``killable=False`` -- the registry never holds a pid the OS does not own.

    Attributes:
        wave_id: ``W<NN>`` wave the lane is driving.
        attempt: 1-based dispatch attempt the lane is driving -- the second
            half of the ``(wave_id, attempt)`` registry key so a re-dispatch
            of the same wave registers a distinct lane.
        session_id: Executor session id the dispatch registered for the
            wave, or ``None`` on a plan-only / stateless dispatch.
        pgid: Process-group id of the spawned child (its own group leader,
            so the pgid equals the child pid), or ``None`` when the spawn
            produced no subprocess -- a ``None`` pgid marks the lane
            unkillable rather than recording a fabricated pid.
        dispatched_at: When the lane's dispatch was issued.
    """

    wave_id: WaveIdStr
    attempt: Annotated[int, Field(ge=1)] = 1
    session_id: str | None = None
    pgid: Annotated[int, Field(ge=1)] | None = None
    dispatched_at: UtcDatetime

    @property
    def killable(self) -> bool:
        """Return whether this lane resolves to a real OS process group.

        A lane is killable iff its spawn recorded a real ``pgid`` -- a
        ``None`` pgid (no subprocess) leaves the lane unkillable so the kill
        path skips it rather than signalling a pid the OS does not own.
        """
        return self.pgid is not None


class FleetCounters(_StrictModel):
    """Running tallies the fleet auto-drain loop accumulates.

    Attributes:
        claimed: Total waves the loop has claimed across the run.
        dispatched: Total waves the loop has dispatched across the run.
        closed: Total lanes that closed clean across the run -- the FA7
            run-summary ``N closed`` tally.
        forked: Total lanes that forked (failed / re-planned) across the run.
        failed: Total lanes whose watcher reported a genuine fork (the wave
            failed / abandoned) across the run -- the FA7 run-summary
            ``M failed`` tally, distinct from a safety-gate ``blocked``
            downgrade. Additive (defaults ``0``) so a counters row written
            before the field existed re-validates unchanged.
        blocked: Total lanes whose clean close was DOWNGRADED to a fork by the
            DL-5 RiskTier safety gate (a high / ui close under an unearned
            advisory jury) across the run -- the FA7 ``K blocked`` tally.
            Additive (defaults ``0``).
        forks_resolved: Total forked lanes a reattach sweep re-dispatched
            successfully across the run -- the FA7 ``N fork resolved`` tally.
            Additive (defaults ``0``).
        rounds: Number of frontier-advance rounds the loop has run.
        clean_rounds: Consecutive rounds with zero forks -- the convergence
            counter the ``kclean`` criterion reads. Reset to zero on any fork.
        spent_eu: Cumulative effort units spent across every finished lane,
            read off each lane's runtime delta -- the figure the EU budget cap
            (DL-4) tests against, and the FA7 ``EU`` total. Additive (defaults
            ``0.0``) so a counters row written before the field existed
            re-validates unchanged.
        spent_usd: Cumulative USD spent across every finished lane, read off
            each lane's runtime delta -- the figure the USD budget cap (DL-4)
            tests against, and the FA7 ``$`` total. Additive (defaults ``0.0``)
            so an older row re-validates unchanged.
    """

    claimed: int = Field(default=0, ge=0)
    dispatched: int = Field(default=0, ge=0)
    closed: int = Field(default=0, ge=0)
    forked: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    blocked: int = Field(default=0, ge=0)
    forks_resolved: int = Field(default=0, ge=0)
    rounds: int = Field(default=0, ge=0)
    clean_rounds: int = Field(default=0, ge=0)
    spent_eu: float = Field(default=0.0, ge=0.0)
    spent_usd: float = Field(default=0.0, ge=0.0)


class FleetForkReason(StrEnum):
    """Why a fleet lane paused to a blocking fork rather than draining clean -- DL-6.

    The closed set of reasons the loop pauses ONLY the offending lane (the
    sibling lanes keep draining) and enqueues a typed :class:`FleetFork`:

    - ``high_risk_close`` -- a :attr:`~eawf.kernel.state.enums.RiskTier.UI`
      (visual-band) lane reported a clean close, but the visual oracle is the
      least deterministic, so the close is held for the operator rather than
      auto-closed.
    - ``uncalibrated_jury`` -- a :attr:`~eawf.kernel.state.enums.RiskTier.HIGH`
      (jury-gated) lane reported a clean close, but the jury that would gate it
      holds only :attr:`~eawf.observability.eval.jury_validation.BlockAuthority.ADVISORY`
      authority, so its advisory verdict cannot auto-close the wave (the
      LOAD-BEARING DL-5 safety invariant surfaces here as a fork).
    - ``needs_user_split`` -- the lane's wave hit a needs-user split mid-run
      (a clarification the executor could not resolve), so the lane pauses for
      operator input.
    - ``repair_exhausted`` -- the lane's grounded repair loop spent its whole
      attempt budget without the refused criterion passing, so the loop
      ESCALATES the lane to an operator-resolved fork ("repair exhausted -- your
      call") carrying the last failing check rather than silently dropping the
      lane or re-dispatching forever (DL-7).
    - ``retry_exhausted`` -- the lane's bounded agent-cli spawn-retry loop spent
      its whole ``max_total_attempts`` budget (RETRY_SAME then SWITCH) without a
      clean spawn, so the loop HALTS the lane to an operator-resolved fork
      rather than respawning it forever (DL-11). The fork carries the terminal
      runtime error class as its failure-class so the operator reads what the
      last attempt failed on.
    - ``runtime_spawn_error`` -- the lane's spawn raised a hard
      :class:`~eawf.runtime.runtimes.adapter.RuntimeSpawnError` the retry ladder
      cannot recover (an ENOENT / permission failure to launch the agent CLI at
      all): no runtime switch and no retry can fix a missing or unexecutable
      binary, so the lane terminates cleanly to an operator-resolved fork on the
      first such failure rather than looping (DL-11).
    - ``subprocess_oom`` -- the lane's spawned subprocess was OOM-killed (the
      kernel reaped it for exceeding memory), so the lane terminates cleanly to
      an operator-resolved fork carrying the OOM failure-class rather than
      respawning a process that will be reaped again (DL-11).
    """

    HIGH_RISK_CLOSE = "high_risk_close"
    UNCALIBRATED_JURY = "uncalibrated_jury"
    NEEDS_USER_SPLIT = "needs_user_split"
    REPAIR_EXHAUSTED = "repair_exhausted"
    RETRY_EXHAUSTED = "retry_exhausted"
    RUNTIME_SPAWN_ERROR = "runtime_spawn_error"
    SUBPROCESS_OOM = "subprocess_oom"


class FleetForkResolution(StrEnum):
    """How an operator resolves a paused :class:`FleetFork` -- DL-6, the closed set.

    - ``approve_close`` -- accept the held close: the wave resolves to
      ``CLOSED`` and the fork is dequeued. The operator overrides the safety
      hold.
    - ``re_dispatch`` -- re-queue the wave onto the run frontier so the loop
      claims + dispatches it again on a later round (a fresh lane / attempt).
    - ``skip`` -- leave the wave PENDING and free the lane slot without
      re-queuing -- the fork is dropped from the queue and the wave is left for
      a later operator decision.
    - ``abort_run`` -- halt the WHOLE run: every still-queued fork is abandoned
      and the run transitions to ``HALTED``.
    """

    APPROVE_CLOSE = "approve_close"
    RE_DISPATCH = "re_dispatch"
    SKIP = "skip"
    ABORT_RUN = "abort_run"


class FleetFork(_StrictModel):
    """One lane paused to a blocking fork, awaiting operator resolution -- DL-6.

    When a lane forks for a blocking reason (a high-risk close, an
    uncalibrated-jury advisory, or a needs-user split) the loop pauses ONLY
    that lane -- it is removed from the in-flight :attr:`FleetRun.lanes` slot
    and appended to the :attr:`FleetRun.forks` queue -- while the sibling lanes
    keep draining. The operator later resolves each queued fork via one of the
    closed :class:`FleetForkResolution` paths.

    Attributes:
        wave_id: ``W<NN>`` wave the paused lane was driving.
        attempt: Dispatch attempt of the paused lane -- the second half of the
            ``(wave_id, attempt)`` lane-registry key it was forked from.
        risk_tier: The lane's resolved
            :class:`~eawf.kernel.state.enums.RiskTier` at fork time, so the
            cockpit renders the band badge on the queued fork.
        reason: The :class:`FleetForkReason` that paused the lane.
        evidence_ref: Repo-relative path / Eawf URN / external URL backing the
            fork -- the close verdict, jury ballot, or needs-user question the
            operator reads before resolving; ``None`` when the loop captured no
            ref.
        forked_at: When the lane was paused to this fork.
    """

    wave_id: WaveIdStr
    attempt: Annotated[int, Field(ge=1)] = 1
    risk_tier: RiskTier
    reason: FleetForkReason
    evidence_ref: Annotated[str, Field(min_length=1)] | None = None
    forked_at: UtcDatetime


class PauseResolution(_StrictModel):
    """One resolved operator decision, kind-tagged across both decision surfaces.

    eawf has two operator-decision surfaces today, each with its own ad-hoc
    resolution shape: a ``needs_user`` pause resolves with a free-string
    ``choice`` against a
    :class:`~eawf.workflow.skills.bodies.user_question.UserQuestion`'s option
    labels, and a fleet auto-drain lane resolves with one of the closed
    :class:`FleetForkResolution` paths. This is the SHARED typed resolution
    record both surfaces project onto so a downstream consumer (the attention
    feed, a calibration sweep over operator decisions) can read every resolved
    decision through one schema -- :attr:`decision_kind` names which surface it
    came from, :attr:`choice` carries the chosen label verbatim, and
    :attr:`urgency` ranks it on the shared :class:`~eawf.kernel.state.enums.Urgency`
    ladder.

    A :class:`UserDecisionKind.FLEET_FORK` resolution always carries a
    :attr:`fork_resolution` (the chosen :class:`FleetForkResolution`) and its
    :attr:`choice` mirrors that resolution's value; a
    :class:`UserDecisionKind.PAUSE` resolution leaves
    :attr:`fork_resolution` ``None`` because a pause is answered by a free-form
    option label rather than the fixed fleet path set. The validator enforces
    that coupling so a malformed cross-surface row fails at construction rather
    than silently skewing a calibration sweep.

    Attributes:
        decision_kind: The :class:`UserDecisionKind` family this resolution
            belongs to -- ``PAUSE`` or ``FLEET_FORK``.
        scope_id: The scope the resolved decision belonged to.
        ref: The decision the resolution answers -- the pause-urn for a
            ``PAUSE`` decision, the ``(wave_id, attempt)``-keyed fork
            identifier for a ``FLEET_FORK`` decision.
        choice: The operator's chosen option label, verbatim. For a
            ``FLEET_FORK`` decision this equals the chosen
            :class:`FleetForkResolution` value.
        fork_resolution: The chosen :class:`FleetForkResolution` on a
            ``FLEET_FORK`` decision; ``None`` on a ``PAUSE`` decision.
        urgency: Shared :class:`~eawf.kernel.state.enums.Urgency` ranking of the
            decision when it was raised, defaulting to
            :attr:`~eawf.kernel.state.enums.Urgency.NORMAL`.
        resolved_at: When the operator resolved the decision.
    """

    decision_kind: UserDecisionKind
    scope_id: Annotated[str, Field(min_length=1)]
    ref: Annotated[str, Field(min_length=1)]
    choice: Annotated[str, Field(min_length=1)]
    fork_resolution: FleetForkResolution | None = None
    urgency: Urgency = Urgency.NORMAL
    resolved_at: UtcDatetime

    @model_validator(mode="after")
    def _check_kind_coupling(self) -> PauseResolution:
        """Enforce the decision-kind / fork-resolution coupling.

        Raises:
            ValueError: When a ``PAUSE`` decision carries a ``fork_resolution``,
                a ``FLEET_FORK`` decision omits it, or a ``FLEET_FORK`` decision's
                ``choice`` disagrees with its ``fork_resolution`` value.
        """
        if self.decision_kind is UserDecisionKind.PAUSE:
            if self.fork_resolution is not None:
                raise ValueError("pause resolution must not carry a fork_resolution")
        elif self.fork_resolution is None:
            raise ValueError("fleet-fork resolution requires a fork_resolution")
        elif self.choice != self.fork_resolution.value:
            raise ValueError(
                f"fleet-fork choice {self.choice!r} must equal "
                f"fork_resolution {self.fork_resolution.value!r}"
            )
        return self


class FleetRun(_StrictModel):
    """Daemon-owned state of the fleet auto-drain loop.

    Persisted as the optional top-level :attr:`State.fleet_run` field
    (default ``None`` -- no active run) so a state written before this field
    existed re-validates unchanged. The loop runner
    (:mod:`eawf.runtime.daemon.methods.fleet`) is the only mutator; the daemon
    canonical state writer persists every run-state transition, so the loop
    never writes ``state.json`` directly.

    Attributes:
        run_state: The closed :class:`FleetRunState` the loop is in.
        concurrency: Maximum lanes the loop holds at once (the drain width).
        frontier: Ready ``W<NN>`` wave ids still queued to claim, in claim
            order. The loop pops from the head as lanes free.
        lanes: In-flight dispatch slots, keyed by wave id.
        forks: Lanes paused to a blocking fork (high-risk close /
            uncalibrated-jury advisory / needs-user split), awaiting operator
            resolution (DL-6). Each paused lane is removed from ``lanes`` and
            appended here while the sibling lanes keep draining. Additive
            (defaults ``[]``) so a state written before the field existed
            re-validates unchanged.
        counters: Running tallies for the run.
        convergence: Convergence mode -- ``drain`` (stop only when the
            frontier empties) or ``kclean`` (stop after K clean rounds).
        kclean_k: K threshold for the ``kclean`` convergence mode -- the
            number of consecutive clean rounds that ends the run. Ignored
            under ``drain``.
        eu_cap: Optional cumulative effort-unit spend cap. When the run's
            ``spent_eu`` reaches it the loop claims no further wave and ends
            ``terminal_reason=budget`` (DL-4). ``None`` (the default) leaves
            the run uncapped. Additive + back-compat.
        usd_cap: Optional cumulative USD spend cap, applied identically to
            ``eu_cap``. ``None`` leaves the run uncapped. Additive.
        waves_cap: Optional claimed-wave count cap. When ``counters.claimed``
            reaches it the loop claims no further wave and ends
            ``terminal_reason=budget``. ``None`` leaves the run uncapped.
            Additive.
        hard_halt: The arm-modal budget-halt toggle. ``False`` (the default)
            is the graceful-drain budget stop -- at the cap the loop stops
            claiming but lets the in-flight lanes finish before ending
            ``budget``. ``True`` arms the hard halt -- reaching the cap KILLS
            the in-flight lanes (the DL-3 kill) instead of draining. Additive
            + back-compat (an older run re-validates as graceful-drain).
        terminal_reason: Why the run reached ``DONE``; ``None`` until then.
            The FA7 run-summary surface relies on its presence as the
            run-complete signal -- it is set in lockstep with the ``DONE``
            transition and stays ``None`` while the run is not DONE.
        ended_at: When the run reached ``DONE``; ``None`` until then. The
            DAEMON stamps it at the terminal transition so the elapsed window
            is computed once, daemon-side, rather than re-derived in the UI.
            Additive (defaults ``None``) so an older run re-validates unchanged.
        elapsed_hours: Wall-clock hours from ``armed_at`` to ``ended_at``,
            computed DAEMON-side at run end -- the FA7 ``elapsed`` figure.
            ``None`` until the run is DONE. Additive.
        throughput: Run throughput in closed waves per hour, computed
            DAEMON-side at run end as ``counters.closed / elapsed_hours`` (the
            FA7 waves/hour figure); the loop computes it once so the cockpit
            never recomputes it. ``None`` until the run is DONE, and ``0.0``
            when the elapsed window is degenerate (zero hours) so the division
            never divides by zero. Additive.
        armed_at: When the run was armed.
    """

    run_state: FleetRunState = FleetRunState.IDLE
    concurrency: int = Field(default=1, ge=1)
    frontier: list[WaveIdStr] = Field(default_factory=list)
    lanes: dict[str, FleetLane] = Field(default_factory=dict)
    forks: list[FleetFork] = Field(default_factory=list)
    counters: FleetCounters = Field(default_factory=FleetCounters)
    convergence: Literal["drain", "kclean"] = "drain"
    kclean_k: int = Field(default=2, ge=1)
    eu_cap: Annotated[float, Field(gt=0.0)] | None = None
    usd_cap: Annotated[float, Field(gt=0.0)] | None = None
    waves_cap: Annotated[int, Field(ge=1)] | None = None
    hard_halt: bool = False
    terminal_reason: FleetTerminalReason | None = None
    ended_at: UtcDatetime | None = None
    elapsed_hours: Annotated[float, Field(ge=0.0)] | None = None
    throughput: Annotated[float, Field(ge=0.0)] | None = None
    armed_at: UtcDatetime


# ---- State root -------------------------------------------------------------


class State(_StrictModel):
    """Top-level eawf state document.

    ``schema_version`` accepts the full ``"1.0"`` through ``"1.12"`` range so
    an on-disk state written before any bump still re-validates after the
    model advances — the migrate chain rewrites the version string in place,
    but a read of an un-migrated state must never reject. The accepted set
    drives the migrate guard's model-supported max, so the literals move in
    lockstep with the migration steps (``v1_0_to_v1_1`` through
    ``v1_11_to_v1_12``). The ``1.5`` edge is purely additive — it registers
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
    to ``None`` until the runtime.capture daemon RPC records fresh counters.

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
    """

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


# Importing :mod:`eawf.kernel.spec.common` triggers its module-bottom
# ``_rebuild_state_models`` call, which resolves the ``list[CriterionSpec]``
# forward reference on :attr:`Wave.success_criteria`. This is a bare MODULE
# import (no attribute access) so it is cycle-safe regardless of entry point:
# when ``common`` is the import entry it is already mid-init in ``sys.modules``
# and this just binds the partial module without touching an undefined name;
# when ``state.models`` is the entry ``common`` loads fully here and rebuilds.
import eawf.kernel.spec.common as _spec_common  # noqa: E402, F401
