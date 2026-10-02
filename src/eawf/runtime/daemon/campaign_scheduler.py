"""The native research scheduler: a Campaign's plan run as rounds of bounded Runs.

A research Campaign is planned from its brief and then driven step by step.
Each round the scheduler takes the ready steps -- pending, with every
dependency done and no open contradiction against them -- up to the fan-out
width, and for each one creates a Run scoped to the Campaign, starts it and
starts the step on it. The research agent works each step's question; the
rounds run side by side, and each one ends in its checkpoint:

1. the accountant charges what the round spent against the step's bound,
   which charges the Campaign's axes by the same delta, and a charge that
   brings a hard axis to its limit records the budget stop on the Campaign;
2. the round's report is recorded as the step's checkpoint artifact,
   from the text the agent returned;
3. the step finishes with the outcome the agent stated, the findings it
   names are promoted, and the Run finishes.

A round whose agent fails returns its step to pending and cancels its Run, and
the drive pauses there rather than retrying blind; a step an earlier drive
left running is returned to pending when the next drive starts. The drive
ends when the
Campaign leaves ``active``: it converges once every step is done, or once the
budget stop leaves nothing running, keeping the budget stop as its reason so
a starved Campaign is never read as one that converged on its own.

Every write goes through the native Campaign and Run writers, so each lands
with its own WAL intent and firehose row and publishes like any other.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import secrets
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, StringConstraints

from eawf.kernel.config.schema import DEFAULT_STALL_INTERVAL_SECONDS
from eawf.kernel.identity import EntityKind, QualifiedUrn, parse_qualified_urn
from eawf.kernel.runtime.dispatch_queue import admission_hold
from eawf.kernel.runtime.events import MessageSummaryPayload, RunEventKind
from eawf.kernel.spec.research import ResearchDepth
from eawf.kernel.state.enums import CampaignStatus
from eawf.kernel.state.epoch2.artifact_revision import MediaKind
from eawf.kernel.state.epoch2.base import PrincipalKey, StrictNonNegativeInt
from eawf.kernel.state.epoch2.campaign import (
    Campaign,
    CampaignMethod,
    CampaignPlanStep,
    ResearchBudget,
    StepOutcome,
    StepState,
    step_blockers,
)
from eawf.kernel.state.epoch2.finding import FindingStatement
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.kernel.state.epoch2.urns import CampaignUrn, RunUrn, TrackUrn
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.control.reducer import reduce_run_control
from eawf.runtime.daemon.admission import (
    EconomicsPolicyError,
    in_flight_reservations,
    load_economics,
)
from eawf.runtime.daemon.epoch2_create import CreateRequest, run_create
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import TransitionRequest, run_transaction
from eawf.runtime.daemon.methods.campaign import (
    MAX_TEXT_BYTES,
    ArtifactRecordParams,
    CampaignCommit,
    CloseParams,
    FindingPromoteParams,
    PlanApproveParams,
    StepUpdateParams,
    close_campaign,
    promote_finding,
    record_artifact,
    update_step,
)
from eawf.runtime.daemon.methods.projection import document_path
from eawf.runtime.daemon.methods.run import append_run_event
from eawf.runtime.daemon.native_dispatch import control_facts_of, stored_run
from eawf.runtime.daemon.run_events import RunEventAppend

logger = logging.getLogger(__name__)

#: How many seconds one unit of a wall-time axis holds, by the unit's spelling.
_SECONDS_PER_UNIT: Final[Mapping[str, int]] = {"s": 1, "min": 60, "h": 3600}

#: The methods a question is worked by at each depth, before the synthesis step.
_DEPTH_METHODS: Final[Mapping[ResearchDepth, tuple[CampaignMethod, ...]]] = {
    ResearchDepth.SHALLOW: (CampaignMethod.SURVEY,),
    ResearchDepth.MEDIUM: (CampaignMethod.SURVEY,),
    ResearchDepth.DEEP: (CampaignMethod.SURVEY, CampaignMethod.ADVERSARIAL),
    ResearchDepth.EXHAUSTIVE: (
        CampaignMethod.SURVEY,
        CampaignMethod.DEPTH,
        CampaignMethod.ADVERSARIAL,
    ),
}

#: The one-line progress a finished round states: one round of one.
_ROUND_UNIT: Final = "rounds"

#: How often a round still being worked says so on its Run's stream. A round's
#: Run sends no worker hello, so the stall sweep holds it to the default interval,
#: and a beat at a quarter of that keeps a slow round from reading as lost.
_HEARTBEAT_SECONDS: float = DEFAULT_STALL_INTERVAL_SECONDS / 4

Report = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=MAX_TEXT_BYTES)]


class StepAssignment(BaseModel):
    """What the research agent is asked to work in one round.

    Attributes:
        campaign_ref: The Campaign the step belongs to.
        campaign_title: What the Campaign researches.
        ordinal: The step.
        title: The step in words.
        method: How the step works its question.
        question: The question in full.
        prior_outcomes: What each step it depends on showed, in plan order.
        run_ref: The Run the round is worked under.
        round_number: Which round of the step this is, from one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_ref: CampaignUrn
    campaign_title: str
    ordinal: int
    title: str
    method: CampaignMethod
    question: str
    prior_outcomes: tuple[str, ...] = ()
    run_ref: RunUrn
    round_number: int


class StepReport(BaseModel):
    """What a research agent hands back for one round, validated where it enters.

    Attributes:
        report: The round's checkpoint report, as markdown.
        outcome: What the step showed, in one line.
        tokens: The tokens the round consumed.
        sources: The sources the round consulted.
        findings: What the round learned that should outlive it, one line each.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    report: Report
    outcome: StepOutcome
    tokens: StrictNonNegativeInt = 0
    sources: StrictNonNegativeInt = 0
    findings: tuple[FindingStatement, ...] = ()


class ResearchAgent(Protocol):
    """Works one step's question for one round and reports what it found."""

    async def work(self, assignment: StepAssignment) -> StepReport:
        """Return the round's report.

        Raises:
            Exception: The round could not be worked; the scheduler returns
                the step to pending and cancels its Run.
        """
        ...


class CampaignDrive(BaseModel):
    """How one drive of a Campaign ended.

    Attributes:
        campaign_key: The Campaign driven.
        disposition: ``converged`` when every step is done, ``budget_exhausted``
            when a hard axis stopped it, ``paused`` when a round failed or every
            remaining step waits on something no round can clear, and
            ``cancelled`` when it was cancelled.
        rounds: The rounds this drive worked.
        run_refs: The Runs it created, in order.
        detail: Why it ended, in one line.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_key: str
    disposition: Literal["converged", "budget_exhausted", "paused", "cancelled"]
    rounds: int
    run_refs: tuple[RunUrn, ...]
    detail: str


# ---------- planning ----------


def _slot(anchor: QualifiedUrn, kind: EntityKind, key: str, *, repository: bool) -> str:
    """Return the address of *key* of *kind* beside *anchor*."""
    return str(
        QualifiedUrn(
            workspace_key=anchor.workspace_key,
            project_key=anchor.project_key,
            repository_key=anchor.repository_key if repository else None,
            kind=kind,
            entity_key=key,
        )
    )


def _next_ordinal(prefix: str, keys: Iterable[str]) -> int:
    taken = [int(key.split("-", 1)[1]) for key in keys if key.startswith(f"{prefix}-")]
    return max(taken, default=0) + 1


def plan_campaign(
    taken_keys: Iterable[str],
    *,
    actor: str,
    track_ref: TrackUrn,
    title: str,
    questions: Sequence[str],
    depth: ResearchDepth,
    budget: ResearchBudget | None = None,
) -> PlanApproveParams:
    """Return the plan approval a research brief asks for.

    Each question is filed as a seed question and worked by the methods the
    depth names, each after the last; one synthesis step then depends on
    every other step and works the first question, the Campaign's own.

    Args:
        taken_keys: Every question key the tree has given out, in whichever store.
        actor: Who approves the plan.
        track_ref: The Track that owns the Campaign.
        title: What the Campaign researches.
        questions: The brief's questions, the Campaign's own first.
        depth: How deep each question is worked.
        budget: The Campaign's axes; ``None`` bounds it by one round per step.

    Returns:
        The plan approval request, every step bounded by one round and by
        the Campaign's own limit on every other axis.

    Raises:
        ValueError: The brief states no question.
    """
    if not questions:
        raise ValueError("a research brief states at least one question")
    first = _next_ordinal("QST", taken_keys)
    seeds = [{"key": f"QST-{first + i:04d}", "question": text} for i, text in enumerate(questions)]
    for seed in seeds:
        seed["urn"] = _slot(track_ref, EntityKind.QUESTION, seed["key"], repository=False)
    methods = _DEPTH_METHODS[depth]
    steps: list[dict[str, Any]] = []
    for seed in seeds:
        for method in methods:
            ordinal = len(steps) + 1
            chained = method is not methods[0]
            steps.append(
                {
                    "ordinal": ordinal,
                    "title": f"{method.value.capitalize()} question {seed['key']}",
                    "method": method.value,
                    "question_ref": seed["urn"],
                    "depends_on": [ordinal - 1] if chained else [],
                }
            )
    steps.append(
        {
            "ordinal": len(steps) + 1,
            "title": "Synthesize the findings",
            "method": CampaignMethod.SYNTHESIS.value,
            "question_ref": seeds[0]["urn"],
            "depends_on": [step["ordinal"] for step in steps],
        }
    )
    total = budget or ResearchBudget.model_validate(
        {"axes": [{"axis_kind": "rounds", "limit": len(steps), "unit": _ROUND_UNIT}]}
    )
    bound = {
        "axes": [
            {
                "axis_kind": axis.axis_kind,
                "limit": 1 if axis.axis_kind == "rounds" else axis.limit,
                "unit": axis.unit,
                "hard": axis.hard,
            }
            for axis in total.axes
        ]
    }
    return PlanApproveParams.model_validate(
        {
            "actor": actor,
            "track_ref": str(track_ref),
            "title": title,
            "evidence_budget": total.model_dump(mode="json"),
            "plan_steps": [{**step, "bound": bound} for step in steps],
            "seed_questions": [
                {"urn": seed["urn"], "question": seed["question"]} for seed in seeds
            ],
        }
    )


# ---------- the drive ----------


class _Driver:
    """One drive of one Campaign: its writes, in the order the rounds need them."""

    def __init__(
        self,
        context: Epoch2RootContext,
        *,
        campaign_key: str,
        agent: ResearchAgent,
        actor: str,
        publish: Callable[[tuple[Envelope, ...]], None],
    ) -> None:
        self._context = context
        self._key = campaign_key
        self._agent = agent
        self._actor = actor
        self._publish = publish
        self._runs: list[str] = []
        self._rounds = 0

    # ----- reads -----

    def _document(self) -> dict[str, Any]:
        return read_document(document_path(self._context.require_selected_generation()))

    def campaign(self) -> Campaign:
        """Return the Campaign as it stands now.

        Raises:
            KeyError: The tree holds no such Campaign.
        """
        row = document_rows(self._document(), Epoch2Collection.CAMPAIGN)[self._key]
        return Campaign.model_validate(row)

    def _question(self, document: dict[str, Any], ref: object) -> str:
        rows = document_rows(document, Epoch2Collection.OPEN_QUESTION)
        match = next((row for row in rows.values() if row.get("urn") == str(ref)), None)
        return str(match["question"]) if match is not None else str(ref)

    # ----- writes -----

    def _commit(self, commit: tuple[CampaignCommit, tuple[Envelope, ...]]) -> CampaignCommit:
        answer, envelopes = commit
        self._publish(envelopes)
        return answer

    def _step(self, ordinal: int, **changes: Any) -> Campaign:
        campaign = self.campaign()
        args = StepUpdateParams.model_validate(
            {
                "actor": self._actor,
                "urn": str(campaign.urn),
                "expected_revision": campaign.revision,
                "ordinal": ordinal,
                **changes,
            }
        )
        answer = self._commit(update_step(self._context, args, now=datetime.now(UTC)))
        return Campaign.model_validate(answer.record)

    def _new_run(self, campaign: Campaign) -> str:
        """Create a Run scoped to *campaign* and start it."""
        document = self._document()
        path = document_path(self._context.require_selected_generation())
        keys = set(document_rows(document, Epoch2Collection.RUN))
        keys |= {
            line.record_key
            for line in effective_records(
                read_ledger_records(ledger_path(path, Epoch2Collection.RUN))
            )
        }
        key = f"RUN-{_next_ordinal('RUN', keys):08d}"
        urn = _slot(campaign.urn, EntityKind.RUN, key, repository=True)
        now = datetime.now(UTC)
        created = run_create(
            context=self._context,
            request=CreateRequest.model_validate(
                {
                    "urn": urn,
                    "expected_revision": int(document.get("canonical_sequence", 0)),
                    "idempotency_key": f"campaign-run-{campaign.key}-{key}",
                    "actor": self._actor,
                    "spec": {
                        "key": key,
                        "scope": {
                            "scope_kind": "campaign",
                            "purpose": "research",
                            "campaign_ref": str(campaign.urn),
                        },
                    },
                }
            ),
            now=now,
        )
        started = self._transition(urn, "RUNNING", 1, updates={"started_at": now.isoformat()})
        self._publish(tuple(e for e in (created.envelope, started) if e is not None))
        self._runs.append(urn)
        return urn

    def _transition(
        self, urn: str, to_status: str, revision: int, **fields: Any
    ) -> Envelope | None:
        committed = run_transaction(
            context=self._context,
            request=TransitionRequest.model_validate(
                {
                    "urn": urn,
                    "to_status": to_status,
                    "expected_revision": revision,
                    "idempotency_key": f"campaign-run-{urn.rsplit('/', 1)[1]}-{to_status.lower()}",
                    "actor": self._actor,
                    **fields,
                }
            ),
            now=datetime.now(UTC),
        )
        return committed.envelope

    def _say(self, urn: str, text: str) -> None:
        """Append one daemon-observed line to the round's Run stream."""
        append_run_event(
            self._context,
            RunEventAppend(
                urn=parse_qualified_urn(urn),
                event_ref=f"EVT-{secrets.token_hex(8)}",
                run_sequence=1,
                event_kind=RunEventKind.MESSAGE_SUMMARIZED,
                provenance="daemon_observed",
                payload=MessageSummaryPayload(message_role="system", summary=text),
                actor=self._actor,
            ),
            now=datetime.now(UTC),
            at_tail=True,
        )

    def _end_run(self, urn: str, *, succeeded: bool) -> None:
        now = datetime.now(UTC).isoformat()
        # anything that moved the Run while the round was worked moved its revision
        with self._context.session([urn]) as session:
            records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
            revision = stored_run(session, records, parse_qualified_urn(urn)).revision
        if succeeded:
            envelope = self._transition(
                urn,
                "COMPLETED",
                revision,
                observations=("run_report_bound",),
                updates={"ended_at": now},
            )
        else:
            envelope = self._transition(
                urn, "CANCELLED", revision, reason_code="round-failed", updates={"ended_at": now}
            )
        self._publish(() if envelope is None else (envelope,))

    def admission(self) -> tuple[str | None, int]:
        """Return what holds new rounds back, and how many more Runs the governor admits.

        A round is a Run like any other: the operator's pause or drain holds it,
        and the governor's Run ceiling counts it beside the admitted Runs still
        live. A round's Run carries no sealed spend cap, so only the ceiling on
        Runs binds it; the Campaign's own axes bound what the rounds spend.
        """
        try:
            governor = load_economics(self._context.identity.tree_root.parent).governor
        except EconomicsPolicyError as error:
            return str(error)[:400], 0
        campaign = self.campaign()
        with self._context.session([campaign.urn]) as session:
            records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
            held = admission_hold(records)
            if held is not None:
                return f"dispatch is held by a {held.value} request", 0

            def status_of(urn: QualifiedUrn) -> RunStatus:
                run = stored_run(session, records, urn)
                return reduce_run_control(
                    status=run.status, facts=control_facts_of(records, urn)
                ).status

            admitted = in_flight_reservations(records, status_of=status_of, excluding=None)
            rounds = [
                row
                for row in document_rows(session.read_document(), Epoch2Collection.RUN).values()
                if row.get("scope", {}).get("scope_kind") == "campaign"
                and status_of(parse_qualified_urn(row["urn"])) is RunStatus.RUNNING
            ]
        free = governor.max_concurrent_runs - len(admitted) - len(rounds)
        if free < 1:
            return (
                f"the governor's ceiling of {governor.max_concurrent_runs} live Runs is reached",
                0,
            )
        return None, free

    # ----- one round -----

    def _assignment(self, campaign: Campaign, step: CampaignPlanStep, run: str) -> StepAssignment:
        prior = tuple(
            dep.outcome
            for dep in campaign.plan_steps
            if dep.ordinal in step.depends_on and dep.outcome is not None
        )
        return StepAssignment.model_validate(
            {
                "campaign_ref": str(campaign.urn),
                "campaign_title": campaign.title,
                "ordinal": step.ordinal,
                "title": step.title,
                "method": step.method,
                "question": self._question(self._document(), step.question_ref),
                "prior_outcomes": prior,
                "run_ref": run,
                "round_number": len(step.run_refs) + 1,
            }
        )

    def start(self, step: CampaignPlanStep) -> StepAssignment | None:
        """Start *step* on a new Run; ``None`` when the Campaign refuses to dispatch it."""
        campaign = self.campaign()
        if campaign.stop is not None:
            return None
        run = self._new_run(campaign)
        assignment = self._assignment(campaign, step, run)
        self._step(step.ordinal, to_state=StepState.RUNNING.value, run_ref=run)
        self._say(run, f"round {assignment.round_number} of step {step.ordinal} started")
        return assignment

    def _spent(self, step: CampaignPlanStep, report: StepReport, seconds: float) -> dict[str, int]:
        """Return the step's observed totals per bounded axis after this round."""
        observed: dict[str, int] = {}
        for axis in step.bound.axes:
            delta = {
                "rounds": 1,
                "tokens": report.tokens,
                "sources": report.sources,
            }.get(axis.axis_kind)
            if axis.axis_kind == "wall_time" and axis.unit in _SECONDS_PER_UNIT:
                delta = math.ceil(seconds / _SECONDS_PER_UNIT[axis.unit])
            if delta is not None:
                observed[axis.axis_kind] = axis.spent + delta
        return observed

    def checkpoint(self, assignment: StepAssignment, report: StepReport, seconds: float) -> None:
        """Charge, record, finish and promote what one round produced."""
        step = self.campaign().step(assignment.ordinal)
        assert step is not None
        self._step(
            step.ordinal,
            spent=self._spent(step, report, seconds),
            progress={"done": 1, "total": 1, "unit": _ROUND_UNIT, "quality": "measured"},
        )
        campaign = self.campaign()
        self._commit(
            record_artifact(
                self._context,
                ArtifactRecordParams(
                    actor=self._actor,
                    urn=campaign.urn,
                    file_name=(
                        f"{campaign.key}/step-{step.ordinal}/"
                        f"round-{assignment.round_number}/summary.md"
                    ),
                    media_kind=MediaKind.MARKDOWN,
                    run_ref=assignment.run_ref,
                    step_ordinal=step.ordinal,
                    text=report.report,
                ),
                now=datetime.now(UTC),
            )
        )
        self._step(step.ordinal, to_state=StepState.DONE.value, outcome=report.outcome)
        for statement in report.findings:
            self._commit(
                promote_finding(
                    self._context,
                    FindingPromoteParams(actor=self._actor, urn=campaign.urn, statement=statement),
                    now=datetime.now(UTC),
                )
            )
        self._end_run(str(assignment.run_ref), succeeded=True)
        self._rounds += 1

    def abandon(self, assignment: StepAssignment, error: BaseException) -> None:
        """Return a failed round's step to pending and cancel its Run."""
        logger.warning(
            f"campaign round failed campaign={self._key} step={assignment.ordinal} "
            f"error={type(error).__name__}"
        )
        self._step(assignment.ordinal, to_state=StepState.PENDING.value)
        self._end_run(str(assignment.run_ref), succeeded=False)

    async def work(self, assignment: StepAssignment) -> tuple[StepReport | None, float, str]:
        """Have the agent work *assignment*; the report, its seconds and any failure."""
        began = time.monotonic()
        done = asyncio.Event()

        async def heartbeat() -> None:
            while True:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(done.wait(), timeout=_HEARTBEAT_SECONDS)
                if done.is_set():
                    return
                elapsed = round(time.monotonic() - began)
                await asyncio.to_thread(
                    self._say, str(assignment.run_ref), f"round still working after {elapsed}s"
                )

        # the beat is stopped and awaited, never cancelled, so no line lands after the Run ends
        beating = asyncio.create_task(heartbeat())
        try:
            report = await self._agent.work(assignment)
        except Exception as error:  # an agent failure of any kind ends only its round
            done.set()
            await beating
            self.abandon(assignment, error)
            return None, time.monotonic() - began, f"{type(error).__name__}: {error}"
        done.set()
        await beating
        return report, time.monotonic() - began, ""

    def recover(self) -> None:
        """Return every step an earlier drive left running to pending, cancelling its Run.

        A drive that ended mid-round -- a daemon stopped under it -- leaves the
        step running on a Run nobody works any more. The Run stays in the
        step's runners, and the step starts again on a new Run.
        """
        campaign = self.campaign()
        runs = document_rows(self._document(), Epoch2Collection.RUN)
        for step in campaign.plan_steps:
            if step.state is not StepState.RUNNING:
                continue
            self._step(step.ordinal, to_state=StepState.PENDING.value)
            run = runs.get(step.run_refs[-1].entity_key)
            if run is not None and run.get("status") == "RUNNING":
                envelope = self._transition(
                    str(step.run_refs[-1]),
                    "CANCELLED",
                    int(run["revision"]),
                    reason_code="drive-stopped",
                    updates={"ended_at": datetime.now(UTC).isoformat()},
                )
                self._publish(() if envelope is None else (envelope,))
            logger.info(f"campaign recovered step campaign={self._key} step={step.ordinal}")

    # ----- ending -----

    def close(self, reason: str) -> Campaign:
        campaign = self.campaign()
        answer = self._commit(
            close_campaign(
                self._context,
                CloseParams(
                    actor=self._actor,
                    urn=campaign.urn,
                    expected_revision=campaign.revision,
                    to_status=CampaignStatus.CONVERGED,
                    reason=reason,
                ),
                now=datetime.now(UTC),
            )
        )
        return Campaign.model_validate(answer.record)

    def ended(self, disposition: str, detail: str) -> CampaignDrive:
        return CampaignDrive.model_validate(
            {
                "campaign_key": self._key,
                "disposition": disposition,
                "rounds": self._rounds,
                "run_refs": self._runs,
                "detail": detail,
            }
        )


def _ready(campaign: Campaign, width: int) -> tuple[CampaignPlanStep, ...]:
    """Return up to *width* steps nothing keeps from starting, in plan order."""
    ready = [
        step
        for step in campaign.plan_steps
        if step.state is StepState.PENDING and not step_blockers(step, campaign.plan_steps)
    ]
    return tuple(ready[:width])


def _settled(driver: _Driver, campaign: Campaign) -> CampaignDrive | None:
    """Return how the drive ends when *campaign* has nothing left to dispatch."""
    if campaign.status is CampaignStatus.CANCELLED:
        return driver.ended("cancelled", campaign.stop.detail if campaign.stop else "cancelled")
    if campaign.status is not CampaignStatus.ACTIVE:
        stop = campaign.stop
        budget = stop is not None and stop.reason == "budget_exhausted"
        return driver.ended(
            "budget_exhausted" if budget else "converged", stop.detail if stop else ""
        )
    running = any(step.state is StepState.RUNNING for step in campaign.plan_steps)
    if campaign.stop is not None and not running:
        closed = driver.close(campaign.stop.detail)
        return _settled(driver, closed)
    if all(step.state is StepState.DONE for step in campaign.plan_steps):
        return _settled(driver, driver.close("every step of the plan is done"))
    if not _ready(campaign, 1) and not running:
        waits = sorted(
            {b for s in campaign.plan_steps for b in step_blockers(s, campaign.plan_steps)}
        )
        return driver.ended("paused", f"every remaining step waits on {', '.join(waits)}")
    return None


async def drive_campaign(
    context: Epoch2RootContext,
    *,
    campaign_key: str,
    agent: ResearchAgent,
    actor: PrincipalKey,
    width: int,
    publish: Callable[[tuple[Envelope, ...]], None],
) -> CampaignDrive:
    """Drive a Campaign's plan round by round until it leaves ``active`` or pauses.

    Args:
        context: The native context of the tree.
        campaign_key: The Campaign to drive.
        agent: Who works each step's question.
        actor: Who the scheduler writes as.
        width: How many ready steps one round dispatches at most, from one.
        publish: Publishes each committed row once its session is released.

    Returns:
        How the drive ended.

    Raises:
        KeyError: The tree holds no such Campaign.
        TransactionRefusedError: A write the scheduler decided was refused.
    """
    driver = _Driver(context, campaign_key=campaign_key, agent=agent, actor=actor, publish=publish)
    await asyncio.to_thread(driver.recover)
    while True:
        campaign = await asyncio.to_thread(driver.campaign)
        settled = await asyncio.to_thread(_settled, driver, campaign)
        if settled is not None:
            logger.info(
                f"drive_campaign campaign={campaign_key} disposition={settled.disposition} "
                f"rounds={settled.rounds}"
            )
            return settled
        held, room = await asyncio.to_thread(driver.admission)
        if held is not None:
            return driver.ended("paused", held)
        started = [
            assignment
            for step in _ready(campaign, min(width, room))
            if (assignment := await asyncio.to_thread(driver.start, step)) is not None
        ]
        if not started:
            return driver.ended("paused", "the Campaign stopped dispatching its ready steps")
        results = await asyncio.gather(*(driver.work(a) for a in started))
        failures = [failure for _report, _seconds, failure in results if failure]
        for assignment, (report, seconds, _failure) in zip(started, results, strict=True):
            if report is not None:
                await asyncio.to_thread(driver.checkpoint, assignment, report, seconds)
        if failures:
            return driver.ended("paused", f"a round failed: {failures[0]}"[:500])


__all__ = [
    "CampaignDrive",
    "ResearchAgent",
    "StepAssignment",
    "StepReport",
    "drive_campaign",
    "plan_campaign",
]
