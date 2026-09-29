"""The records the decision overlays and the Enter-opened cards draw, validated once here.

A question, a pause, a claim with its rung ladder, a draft, a roadmap marker, a campaign
artifact and a campaign step are process and planning records rather than rows of a
route's document, so they arrive beside the projection the way the sealed acceptance bundle
does. Each model is closed and frozen: a field the producer starts emitting fails here
rather than passing silently, and a card holds no reference through which its record
could be edited. The combinations the planning contract forbids -- a person-wait pause
naming no record, a held pause naming no holder, a passed rung above an unpassed rung 1 or
2 -- are refused at this boundary, so a renderer never has to draw one.

What a record renders as (its situation, standing or ender) is never stored here; the
renderers derive it at render time from these fields.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from eawf.kernel.state.epoch2.pending_action import AgentPrincipal, PendingAction

#: An open question's ``QST-####`` key, or the ``ACT-####`` key of an operator decision,
#: which the question detail draws the same way: a question with offered answers.
QuestionKey = Annotated[str, StringConstraints(pattern=r"^(QST|ACT)-\d{4,}$")]
ClaimKey = Annotated[str, StringConstraints(pattern=r"^CLM-\d{4,}$")]
MilestoneKey = Annotated[str, StringConstraints(pattern=r"^MLS-\d{4,}$")]
CampaignKey = Annotated[str, StringConstraints(pattern=r"^CAM-\d{4,}$")]
RunKey = Annotated[str, StringConstraints(pattern=r"^RUN-[0-9a-f]{8}$")]
EvidenceKey = Annotated[str, StringConstraints(pattern=r"^EVD-\d{4,}$")]
Text = Annotated[str, StringConstraints(min_length=1)]

#: The most answers a question may offer as options; zero is a reply question.
MAX_OPTIONS: Final = 4
#: The rungs of a claim's ladder, in order, by name.
RUNG_NAMES: Final[tuple[str, ...]] = ("resolve", "anchor", "screen", "entail")


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------- the question ----------


class QuestionStatus(StrEnum):
    """The persisted states of an open question."""

    OPEN = "OPEN"
    BLOCKED = "BLOCKED"
    ANSWERED = "ANSWERED"
    AUTO_RESOLVED = "AUTO_RESOLVED"
    SEALED = "SEALED"
    DROPPED = "DROPPED"


class DropReason(StrEnum):
    """Why a question was dropped."""

    MOOT = "moot"
    OUT_OF_SCOPE = "out_of_scope"
    SUPERSEDED = "superseded"


class QuestionOption(_Record):
    """One answer a question offers.

    Attributes:
        key: The option's id, which an answer names.
        label: The option in words.
        recommended: Whether the asking Run recommends it; a recommendation is not consent.
    """

    key: Text
    label: Text
    recommended: bool = False


class LateAnswer(_Record):
    """A principal's own answer that arrived after another had already resolved the question.

    Attributes:
        principal: Who answered late.
        option: The option they chose.
    """

    principal: Text
    option: Text


class QuestionRecord(_Record):
    """One open question, as the question detail draws it.

    Attributes:
        id: The question's ``QST-####`` key, or an operator decision's ``ACT-####`` key.
        run: The asking Run; ``None`` when a person asked, since a person asks inside none.
        scope: The scope the question was asked under.
        asked_at: When it was asked.
        question: The question in full.
        rationale: Why the Run asks; ``None`` when it states no reason.
        options: The offered answers, zero to four; zero makes it a reply question.
        default_option: The option policy selects when nobody answers, if any.
        override_until: When an operator may still override a policy default.
        status: The persisted state.
        blocking: Whether an unanswered question halts work.
        escalated_by: The deadline that raised a blocking question's urgency, when one did.
        resolution_actor: Who resolved, answered or dropped it.
        chosen_option: The option it was resolved with.
        reply: The operator's own words, when it was answered by reply.
        drop_reason: Why it was dropped.
        superseded_by: The question that replaced it.
        disclosed: Whether this principal's authority class may see the winning answer.
        late_answers: Each principal's answer that arrived after the winning one.

    Raises:
        pydantic.ValidationError: a drop without a reason, a replacement drop naming no
            successor or a successor on another drop, or a default window with no default.
    """

    id: QuestionKey
    run: RunKey | None = None
    scope: Text
    asked_at: AwareDatetime
    question: Text
    rationale: str | None = None
    options: tuple[QuestionOption, ...] = Field(default=(), max_length=MAX_OPTIONS)
    default_option: str | None = None
    override_until: AwareDatetime | None = None
    status: QuestionStatus
    blocking: bool = False
    escalated_by: str | None = None
    resolution_actor: str | None = None
    chosen_option: str | None = None
    reply: str | None = Field(default=None, max_length=2000)
    drop_reason: DropReason | None = None
    superseded_by: QuestionKey | None = None
    disclosed: bool = True
    late_answers: tuple[LateAnswer, ...] = ()

    @model_validator(mode="after")
    def _dispositions_agree(self) -> Self:
        dropped = self.status is QuestionStatus.DROPPED
        if dropped != (self.drop_reason is not None):
            raise ValueError("a drop reason is carried exactly by a dropped question")
        replaced = self.drop_reason is DropReason.SUPERSEDED
        if replaced != (self.superseded_by is not None):
            raise ValueError("a successor is named exactly by a replacement drop")
        if self.status is QuestionStatus.AUTO_RESOLVED and (
            self.default_option is None or self.override_until is None
        ):
            raise ValueError("a defaulted question names its default and its override window")
        return self

    def option(self, key: str | None) -> QuestionOption | None:
        """Return the offered option ``key``, or ``None`` when the question offers none."""
        return next((o for o in self.options if o.key == key), None)

    @classmethod
    def of_decision(cls, action: PendingAction) -> Self:
        """Return the question detail's record of a waiting operator decision.

        A decision waits the way an open question does, with no halted work: it offers
        its filed options, marks its one recommendation, and names the default and the
        window in which an answer still overrides it.

        Args:
            action: A waiting ``operator_decision`` pending action.

        Returns:
            The record, open, asked by the Run the requesting agent acts inside or by
            nobody's Run when a person asked.
        """
        requester = action.requested_by
        return cls(
            id=action.id,
            run=requester.run_ref.entity_key if isinstance(requester, AgentPrincipal) else None,
            scope=action.subject_ref.entity_key,
            asked_at=action.created_at,
            question=action.question,
            options=tuple(
                QuestionOption(
                    key=option.option_id,
                    label=option.label,
                    recommended=option.option_id == action.recommended_option_id,
                )
                for option in action.options
            ),
            default_option=action.default_on_timeout,
            override_until=action.override_until,
            status=QuestionStatus.OPEN,
        )


# ---------- the pause ----------


class PauseReason(StrEnum):
    """Why the daemon paused the work."""

    PERMISSION = "permission"
    USER = "user"
    PROVIDER = "provider"
    LEASE = "lease"
    POLICY = "policy"
    INFRASTRUCTURE = "infrastructure"
    EXTERNAL_TRUTH_AMBIGUOUS = "external_truth_ambiguous"


#: The reasons whose pause waits on a person rather than a check.
PERSON_REASONS: Final = frozenset({PauseReason.PERMISSION, PauseReason.USER})
#: The reasons whose pause over a lost Run leaves a control's outcome unknown.
UNKNOWN_OUTCOME_REASONS: Final = frozenset(
    {PauseReason.PROVIDER, PauseReason.EXTERNAL_TRUTH_AMBIGUOUS}
)


class PauseStatus(StrEnum):
    """The persisted states of an open pause."""

    OPEN = "OPEN"
    HELD = "HELD"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    CANCELLED = "CANCELLED"


class EscalationCause(StrEnum):
    """What an escalation fired on."""

    BUDGET = "budget"
    DEADLINE = "deadline"
    AMBIGUITY = "ambiguity"


class Escalation(_Record):
    """Why an escalated pause escalated and the record it raised.

    Attributes:
        cause: What it fired on.
        raised_at: When it fired.
        raised_ref: The record it raised, which is the only ender it admits.
    """

    cause: EscalationCause
    raised_at: AwareDatetime
    raised_ref: Text


class ControlLedger(_Record):
    """The stamps of the control that raised the pause.

    Attributes:
        requested: When the control was requested.
        accepted: When the daemon accepted it.
        confirmed: When the Run confirmed it; ``None`` when the Run never answered.
    """

    requested: AwareDatetime
    accepted: AwareDatetime | None = None
    confirmed: AwareDatetime | None = None


class PauseRecord(_Record):
    """One open pause, as the pause detail draws it.

    Attributes:
        id: The pause's key.
        scope: The work it pauses, a Run, Batch, Campaign or Release key.
        reason: Why it paused.
        status: The persisted state.
        resume_predicate: What would resume it, in words.
        evaluator: What evaluates the predicate, when a check does.
        last_evaluated: When the predicate was last evaluated.
        waiting_on: The ``ACT-`` or ``QST-`` record a person-wait pause waits on.
        eligible: The principals who may answer that record.
        control: The control ledger stamps, where a control raised it.
        retry_used: The attempts spent.
        retry_allowed: The attempts the policy allows.
        held_by: The principal who placed the Hold.
        hold_id: The Hold's id.
        escalation: Why an escalated pause escalated.
        resolved_at: When the predicate was observed.
        enclosing: The work whose cancellation cancelled it.

    Raises:
        pydantic.ValidationError: a person-wait pause names no record or another pause
            names one, a held pause names no holder, or an escalated pause no escalation.
    """

    id: Text
    scope: Text
    reason: PauseReason
    status: PauseStatus
    resume_predicate: Text
    evaluator: str | None = None
    last_evaluated: AwareDatetime | None = None
    waiting_on: str | None = None
    eligible: tuple[str, ...] = ()
    control: ControlLedger | None = None
    retry_used: int | None = Field(default=None, ge=0)
    retry_allowed: int | None = Field(default=None, ge=0)
    held_by: str | None = None
    hold_id: str | None = None
    escalation: Escalation | None = None
    resolved_at: AwareDatetime | None = None
    enclosing: str | None = None

    @model_validator(mode="after")
    def _situation_fields_agree(self) -> Self:
        if (self.reason in PERSON_REASONS) != (self.waiting_on is not None):
            raise ValueError("a pause names the record it waits on exactly when a person answers")
        held = self.status is PauseStatus.HELD
        if held != (self.held_by is not None and self.hold_id is not None):
            raise ValueError("a held pause names its holder and Hold, and no other does")
        if (self.status is PauseStatus.ESCALATED) != (self.escalation is not None):
            raise ValueError("an escalation is carried exactly by an escalated pause")
        return self


# ---------- the claim and its rung ladder ----------


class RungOutcome(StrEnum):
    """What one rung of a claim's ladder returned."""

    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    NOT_RUN = "not_run"
    ATTESTED = "attested"


class RungInput(_Record):
    """One input a rung ran over.

    Attributes:
        ref: The input's reference.
        digest: Its digest, in full.
    """

    ref: Text
    digest: Text


class RungRecord(_Record):
    """One rung of a claim's ladder, written once by the evidence scorer.

    Attributes:
        rung: The rung, one to four.
        outcome: What it returned.
        check: The rung's question in full.
        inputs: Every input it ran over.
        finding: What it found; ``None`` before it ran.
        counts: The finding's named counts, one line each.
        as_of: When it was evaluated.
        evaluator: What evaluated it.
        sequence: The sequence the record was written at.
        evidence: The ``EVD-`` key filed for it, when one was.
        digest_unavailable: Whether rung 1 could not fetch an input digest.

    Raises:
        pydantic.ValidationError: an attestation below rung 4.
    """

    rung: int = Field(ge=1, le=len(RUNG_NAMES))
    outcome: RungOutcome
    check: Text
    inputs: tuple[RungInput, ...] = ()
    finding: str | None = None
    counts: tuple[str, ...] = ()
    as_of: AwareDatetime | None = None
    evaluator: Text = "evidence scorer"
    sequence: int | None = None
    evidence: EvidenceKey | None = None
    digest_unavailable: bool = False

    @model_validator(mode="after")
    def _only_rung_four_attests(self) -> Self:
        if self.outcome is RungOutcome.ATTESTED and self.rung != len(RUNG_NAMES):
            raise ValueError(f"rung {self.rung} cannot be attested; only rung 4 is")
        return self

    @property
    def name(self) -> str:
        """Return the rung's name."""
        return RUNG_NAMES[self.rung - 1]


class ClaimEdge(_Record):
    """One support or contradiction edge of a claim.

    Attributes:
        ref: The record at the other end.
        what: What it says, in words.
        denied: Whether this principal's authority class may not see it.
    """

    ref: Text
    what: str = ""
    denied: bool = False


class ClaimRecord(_Record):
    """One claim with its four-rung ladder, as the evidence viewer and rung card draw it.

    Attributes:
        id: The claim's ``CLM-####`` key.
        urn: The claim's canonical address, which copy yields.
        title: The claim in one line.
        in_words: The claim restated plainly.
        proves: What it proves.
        breaks_if: What would break it.
        source: The artifact it was read from.
        asked_by: The question it answers.
        supports: The typed support edges.
        contradictions: The typed contradiction edges.
        rungs: The ladder, rungs 1 to 4 in order.
        as_of: When the ladder was read.

    Raises:
        pydantic.ValidationError: the ladder is not rungs 1 to 4 in order, or a passed rung
            stands above an unpassed rung 1 or 2.
    """

    id: ClaimKey
    urn: Text
    title: Text
    in_words: str | None = None
    proves: str | None = None
    breaks_if: str | None = None
    source: str | None = None
    asked_by: str | None = None
    supports: tuple[ClaimEdge, ...] = ()
    contradictions: tuple[ClaimEdge, ...] = ()
    rungs: tuple[RungRecord, ...]
    as_of: AwareDatetime

    @model_validator(mode="after")
    def _ladder_is_whole_and_not_inferred(self) -> Self:
        if tuple(r.rung for r in self.rungs) != tuple(range(1, len(RUNG_NAMES) + 1)):
            raise ValueError("a claim's ladder is rungs 1 to 4, in order")
        base = all(r.outcome is RungOutcome.PASS for r in self.rungs[:2])
        above = any(r.outcome is RungOutcome.PASS for r in self.rungs[2:])
        if above and not base:
            raise ValueError("a rung above an unpassed rung 1 or 2 cannot have passed")
        return self

    def rung(self, n: int) -> RungRecord | None:
        """Return rung ``n``, or ``None`` past the ladder."""
        return self.rungs[n - 1] if 1 <= n <= len(self.rungs) else None


# ---------- the draft, the marker, the artifact and the step ----------


class DraftState(StrEnum):
    """The backlog states a draft card may be opened on."""

    DRAFT = "DRAFT"
    DEFERRED = "DEFERRED"


class DraftRecord(_Record):
    """One backlog row the draft card is opened on.

    Attributes:
        id: The Task's key.
        title: The Task in one line.
        state: Whether it is a draft or deferred.
        due_scope: The scope it is due in.
        due: The due value the route lists.
        criteria: The proof that will show it done, when answered.
        owner: Who answers for it, when answered.
        batch: Where it will be integrated, when answered.
    """

    id: Text
    title: Text
    state: DraftState
    due_scope: Text
    due: Text
    criteria: str | None = None
    owner: str | None = None
    batch: str | None = None


class MarkerFact(StrEnum):
    """What a roadmap marker's date is."""

    DATED = "dated"
    FORECAST = "forecast"
    UNDATED = "undated"


class MarkerRecord(_Record):
    """One roadmap marker's milestone record.

    Attributes:
        id: The Milestone's key.
        title: The Milestone's title.
        track: The Track its lane belongs to.
        fact: Whether its date is committed, forecast or absent.
        state: The Milestone's state in words.
        batches: The Batches under it, each as ``key title · state``.
        sealed_at: When its bundle was sealed, if it was.
        digest: The sealed bundle's digest.
        promised: What it promised.
        built_batches: The Batches built.
        built_tasks: The Tasks built.
        criteria: Each criterion in words with whether it is proven.
    """

    id: MilestoneKey
    title: Text
    track: Text
    fact: MarkerFact
    state: Text
    batches: tuple[str, ...] = ()
    sealed_at: AwareDatetime | None = None
    digest: str | None = None
    promised: Text
    built_batches: int = Field(ge=0)
    built_tasks: int = Field(ge=0)
    criteria: tuple[tuple[str, bool], ...] = ()


class ArtifactRecord(_Record):
    """One campaign artifact, its provenance and its text.

    Attributes:
        key: The artifact's key, the card's subject.
        campaign: The campaign that keeps it.
        index: Its place among the campaign's artifacts, from one.
        total: How many artifacts the campaign keeps.
        file: The file name.
        media: The media kind in words.
        size: The size in words.
        written_at: When it was written.
        step: The step that wrote it.
        digest: Its sha256, as the record keeps it.
        binary: Whether it is a binary file, which opens externally.
        lines: The file's text, one line each, never rewritten.
        as_of: When the campaign was read.
    """

    key: Text
    campaign: CampaignKey
    index: int = Field(ge=1)
    total: int = Field(ge=1)
    file: Text
    media: Text
    size: Text
    written_at: AwareDatetime
    step: int = Field(ge=1)
    digest: Text
    binary: bool = False
    lines: tuple[str, ...] = ()
    as_of: AwareDatetime


class StepState(StrEnum):
    """What a campaign step's record states; ``blocked`` is derived, never stored."""

    DONE = "done"
    RUNNING = "running"
    PENDING = "pending"


class StepRecord(_Record):
    """One campaign plan step.

    Attributes:
        campaign: The campaign.
        ordinal: Its place in the plan, from one.
        total: How many steps the plan holds.
        title: The step in words.
        state: What its record states.
        started_at: When it started.
        ended_at: When it ended.
        waits_on: The unmet steps and open contradictions it waits on, by name.
        runner: The runner Run.
        provider: The runner's provider.
        session_fact: What is known of the runner's session.
        spent: What it spent, as a number.
        limit: Its bound, as a number.
        unit: The unit both are in.
        done_units: The units done, while it runs.
        total_units: The units it will do.
        progress_unit: What those units are.
        outcome: What it showed, once done.
        events: The runner's semantic events, as ``(time, what)``.
        produced: The ``EVD-`` receipts and artifacts it produced.
        why_none: Why it has produced nothing yet.
        as_of: When the campaign was read.
    """

    campaign: CampaignKey
    ordinal: int = Field(ge=1)
    total: int = Field(ge=1)
    title: Text
    state: StepState
    started_at: AwareDatetime | None = None
    ended_at: AwareDatetime | None = None
    waits_on: tuple[str, ...] = ()
    runner: str | None = None
    provider: str | None = None
    session_fact: str | None = None
    spent: float | None = None
    limit: float | None = None
    unit: str = ""
    done_units: int | None = None
    total_units: int | None = None
    progress_unit: str = ""
    outcome: str | None = None
    events: tuple[tuple[str, str], ...] = ()
    produced: tuple[str, ...] = ()
    why_none: str = "it has not started"
    as_of: AwareDatetime

    @property
    def blocked(self) -> bool:
        """Return whether it is blocked: pending on something it names."""
        return self.state is StepState.PENDING and bool(self.waits_on)


# ---------- what the console holds ----------


class DecisionRecords(_Record):
    """Every record the decision overlays and cards may be opened on.

    Attributes:
        principal: The principal the console acts as, which tells a question answered by
            this principal from one answered elsewhere.
        run_states: The asking or affected Runs' states, which the unanswerable question
            and the unknown-outcome pause are derived from.
        questions: The open questions.
        pauses: The open pauses.
        claims: The claims, each with its ladder.
        drafts: The draft and deferred backlog rows.
        markers: The roadmap markers' records.
        artifacts: The campaign artifacts.
        steps: The campaign plan steps.
    """

    principal: str | None = None
    run_states: dict[str, str] = Field(default_factory=dict)
    questions: tuple[QuestionRecord, ...] = ()
    pauses: tuple[PauseRecord, ...] = ()
    claims: tuple[ClaimRecord, ...] = ()
    drafts: tuple[DraftRecord, ...] = ()
    markers: tuple[MarkerRecord, ...] = ()
    artifacts: tuple[ArtifactRecord, ...] = ()
    steps: tuple[StepRecord, ...] = ()

    def question(self, key: str | None) -> QuestionRecord | None:
        """Return the question ``key`` names, or ``None`` when none is held."""
        return next((q for q in self.questions if q.id == key), None)

    def pause(self, key: str | None) -> PauseRecord | None:
        """Return the pause ``key`` names by its id or its scope, or ``None``."""
        return next((p for p in self.pauses if key in (p.id, p.scope)), None)

    def claim(self, key: str | None) -> ClaimRecord | None:
        """Return the claim ``key`` names, or ``None`` when none is held."""
        return next((c for c in self.claims if c.id == key), None)

    def draft(self, key: str | None) -> DraftRecord | None:
        """Return the backlog row ``key`` names, or ``None`` when none is held."""
        return next((d for d in self.drafts if d.id == key), None)

    def marker(self, key: str | None) -> MarkerRecord | None:
        """Return the marker record ``key`` names, or ``None`` when none is held."""
        return next((m for m in self.markers if m.id == key), None)

    def artifact(self, key: str | None) -> ArtifactRecord | None:
        """Return the artifact ``key`` names, or ``None`` when none is held."""
        return next((a for a in self.artifacts if a.key == key), None)

    def step(self, campaign: str | None, ordinal: int) -> StepRecord | None:
        """Return step ``ordinal`` of ``campaign``, or ``None`` when none is held."""
        return next(
            (s for s in self.steps if s.campaign == campaign and s.ordinal == ordinal), None
        )


def short_time(at: datetime | None) -> str:
    """Return ``at`` as the clock time a frame prints, the no-value dash when absent."""
    return "–" if at is None else f"{at:%H:%M}"  # noqa: RUF001
