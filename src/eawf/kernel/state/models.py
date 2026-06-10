"""Pydantic v2 state models for eawf.

Mirrors ``docs/architecture/state-model.md``. Every model uses
``ConfigDict(extra="forbid")``. ID-shaped fields use regex patterns from
:mod:`eawf.kernel.state.ids`. URN-shaped fields use the ``urn:eawf:v1:`` prefix
constraint. Datetimes are tz-aware UTC (Pydantic accepts ISO-8601 with ``Z``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    ScopeKind,
    SubprojectStatus,
    Urgency,
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
    subproject_id: str | None = None
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
        subprocess_pid: PID at spawn time; ``None`` before W09 wires
            the actual spawn.
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
    subprocess_pid: int | None = None
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
    """

    api_duration_ms: Annotated[int, Field(ge=0)] | None = None
    total_duration_ms: Annotated[int, Field(ge=0)] | None = None
    cost_usd: Annotated[float, Field(ge=0.0)] | None = None
    input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)] | None = None
    cache_creation_input_tokens: Annotated[int, Field(ge=0)] | None = None
    cache_read_input_tokens: Annotated[int, Field(ge=0)] | None = None
    captured_at: UtcDatetime


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
    """

    id: IdStr
    scope_id: str
    status: ActualStatus
    elapsed_eu: float
    attention_eu: float | None = None
    agent_runtime_eu: float | None = None
    actual_tokens: Annotated[int, Field(ge=0)] = 0
    actual_cost_usd: Annotated[float, Field(ge=0.0)] = 0.0
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


# ---- State root -------------------------------------------------------------


class State(_StrictModel):
    """Top-level eawf state document.

    ``schema_version`` accepts the full ``"1.0"`` through ``"1.9"`` range so
    an on-disk state written before any bump still re-validates after the
    model advances — the migrate chain rewrites the version string in place,
    but a read of an un-migrated state must never reject. The accepted set
    drives the migrate guard's model-supported max, so the literals move in
    lockstep with the migration steps (``v1_0_to_v1_1`` through
    ``v1_8_to_v1_9``). The ``1.5`` edge is purely additive — it registers
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
    """

    schema_version: Literal["1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "1.8", "1.9"]
    scope_kind: ScopeKind
    urn: UrnStr
    updated_at: UtcDatetime
    project: Project | None
    current: CurrentPointers
    workspace: WorkspaceIndex | None
    health: Health | None = None
    dispatch_paused: bool = False
    subprojects: dict[str, Subproject] | None = None
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
