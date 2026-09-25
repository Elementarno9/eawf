"""``runtime.run.budget.meter``: cap a Run while its turn is still running.

The shipped token-cap interlock classified a dispatch's burn only once the
dispatch had returned, so the group it signalled had already exited and no
Run was ever terminated at its cap. This verb runs on a reading taken
mid-turn, and it writes before it signals: the notice and the opened
control reach the run ledger while the process group is alive, and the
confirmed effect is appended only after the kill ladder has observed the
group die. A confirmed effect names what was seen, so it cannot precede
the reap it reports.

The module stands on its own rather than reaching into the control verbs'
module: a cross-module name inside the method package is a deliberate
public API, and the primitives this needs are private there. What must not
be duplicated is not duplicated either -- the sequencing rule a control
fact obeys is enforced by :class:`~eawf.kernel.runtime.control.ControlFact`
itself, the lease is decided by
:func:`~eawf.runtime.control.reducer.decide_control_lease`, and terminality
is read out of :func:`~eawf.runtime.control.reducer.reduce_run_control`
rather than off the stored record. Only the append glue is local.

A budget termination takes the Run's one control lease like any other
principal. It reuses ``cancel`` -- no driver honours "budget", and a
stopped episode that produced no report is cancelled -- so a budget
termination racing an operator cancel is two answers to one question. The
lease grants one of them; the loser records its notice, which blocks
nothing, and sends no signal, because the holder's own control ends the
Run. The Run reaches the same terminal status either way, and only the
attribution differs.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.runtime.budget_notice import BUDGET_TERMINATION_CONTROL, BudgetNotice
from eawf.kernel.runtime.control import (
    ControlDisposition,
    ControlEffectId,
    ControlFact,
    ControlPhase,
    ControlRequestId,
)
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey, StrictNonNegativeInt, StrictPositiveInt
from eawf.kernel.state.epoch2.run import Run, RunStatus
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import (
    LedgerRecord,
    effective_records,
    read_ledger_records,
)
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.budget.policy import DEFAULT_ENFORCE, DEFAULT_MULTIPLIER, EnforceMode
from eawf.runtime.budget.service import TerminationResult
from eawf.runtime.control.reducer import decide_control_lease, reduce_run_control
from eawf.runtime.daemon.budget_interlock import InFlightBudgetOutcome, guard_in_flight_budget
from eawf.runtime.daemon.epoch2_recovery import PROJECTION_DEGRADED, publish_projection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.epoch2_transaction import (
    TransactionRefusedError,
    TransitionRequest,
    commit_ledger_append,
    run_transaction,
)
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_mutator
from eawf.runtime.runtimes.metering import InFlightMeter, MeterReading, UsageSample, meter_stream

logger = logging.getLogger(__name__)

#: The in-flight cap verb: meter a turn's usage and terminate at the cap.
RUN_BUDGET_METER_METHOD: Final = "runtime.run.budget.meter"

#: The ledger-line key a budget notice is filed under, prefixed for the
#: same reason the contract binding is: one key, one kind of line.
_NOTICE_KEY_PREFIX: Final = "BGT-"

#: The status a budget-notice line records.
_NOTICE_STATUS: Final = "noticed"

#: The stable reason a Run terminalized by a confirmed control effect.
_EFFECT_REASON_CODE: Final = "control-effect-confirmed"


class _MeterParams(BaseModel):
    """Params of :data:`RUN_BUDGET_METER_METHOD`."""

    model_config = ConfigDict(extra="forbid")

    urn: RunUrn
    control_request_ref: ControlRequestId
    actor: PrincipalKey
    samples: tuple[UsageSample, ...] = ()
    base_budget: StrictPositiveInt | None = None
    enforce: EnforceMode = DEFAULT_ENFORCE
    multiplier: Annotated[float, Field(gt=0.0)] = DEFAULT_MULTIPLIER
    pgid: StrictPositiveInt | None = None
    idempotency_key: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]


class RunBudgetAnswer(BaseModel):
    """What the in-flight cap verb answers with.

    Attributes:
        observed_tokens: The metered consumption the cap was tested against.
        output_tokens: The output slice of it, reasoning counted inside.
        cap_tokens: The effective cap, or ``None`` when none is configured.
        over_cap: Whether the reading met or crossed the cap.
        action: The enforcement verdict the reading classified to.
        terminated: Whether the kill ladder was driven.
        led: Whether the budget control took the Run's control lease.
        notice: The budget notice the ledger holds, or ``None`` when the
            reading stayed under the cap and nothing was written.
        run_status: The status the Run's confirmed effects support.
        control_cursor: The last applied control-ledger sequence.
        warnings: Non-fatal notes about an answer that still stands.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_tokens: StrictNonNegativeInt
    output_tokens: StrictNonNegativeInt
    cap_tokens: int | None
    over_cap: bool
    action: str
    terminated: bool
    led: bool
    notice: dict[str, Any] | None
    run_status: RunStatus
    control_cursor: StrictNonNegativeInt
    warnings: tuple[str, ...] = ()


def _validated(params: dict[str, Any]) -> _MeterParams:
    """Validate request params, dropping the key the fence already used.

    Raises:
        DaemonValidationError: The request does not parse. The pydantic
            detail is reduced to field paths so the refusal never repeats
            a submitted value into a log.
    """
    try:
        return _MeterParams.model_validate(
            {key: value for key, value in params.items() if key != REPO_ROOT_PARAM}
        )
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        raise DaemonValidationError(
            f"validation_failed: schema_validation_failed: check {', '.join(fields)}"
        ) from error


def _ledger(session: RootSession) -> Path:
    """Return the run collection's append-only ledger in this generation."""
    return session.ledger_path(Epoch2Collection.RUN)


def _control_facts(records: tuple[LedgerRecord, ...], urn: QualifiedUrn) -> tuple[ControlFact, ...]:
    """Return one Run's control facts from the run ledger, in sequence order."""
    facts = [
        ControlFact.model_validate(item.payload)
        for item in records
        if item.payload.get("payload_kind") == "control"
    ]
    selected = (fact for fact in facts if fact.run_ref == urn)
    return tuple(sorted(selected, key=lambda fact: fact.sequence))


def _stored_run(session: RootSession, records: tuple[LedgerRecord, ...], urn: QualifiedUrn) -> Run:
    """Return the Run record, from the document or from its ledger.

    A terminal Run is compacted out of the document into the run ledger,
    so a reading that looked only in the document would stop answering for
    exactly the Runs whose outcome matters most.

    Raises:
        DaemonValidationError: Neither tier holds the record.
    """
    row = document_rows(session.read_document(), Epoch2Collection.RUN).get(urn.entity_key)
    if row is not None:
        return Run.model_validate(row)
    for item in effective_records(records):
        if item.record_key == urn.entity_key and "payload_kind" not in item.payload:
            return Run.model_validate(item.payload)
    raise DaemonValidationError(
        f"validation_failed: identity_not_found: no run record keyed {urn.entity_key!r} "
        "is held by the document or the run ledger"
    )


def _append_fact(session: RootSession, fact: ControlFact, *, now: datetime) -> Envelope:
    """Commit one control fact as a line of the run ledger."""
    return commit_ledger_append(
        session,
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key=fact.control_request_ref,
            status=fact.phase.value,
            recorded_at=now,
            payload=fact.model_dump(mode="json"),
        ),
    )


def _append_notice(session: RootSession, notice: BudgetNotice, *, now: datetime) -> Envelope:
    """Commit one budget notice as a line of the run ledger."""
    return commit_ledger_append(
        session,
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key=f"{_NOTICE_KEY_PREFIX}{notice.control_request_ref}",
            status=_NOTICE_STATUS,
            recorded_at=now,
            payload=notice.model_dump(mode="json"),
        ),
    )


def _notice_of(records: tuple[LedgerRecord, ...], ref: ControlRequestId) -> BudgetNotice | None:
    """Return the budget notice already written for *ref*, or ``None``."""
    for item in records:
        if item.payload.get("payload_kind") != "budget_notice":
            continue
        notice = BudgetNotice.model_validate(item.payload)
        if notice.control_request_ref == ref:
            return notice
    return None


def _standing_fact(
    facts: tuple[ControlFact, ...], *, ref: ControlRequestId, phase: ControlPhase
) -> ControlFact | None:
    """Return the fact a retry of this phase is already answered by."""
    for fact in facts:
        if fact.control_request_ref == ref and fact.phase is phase:
            return fact
    return None


def _budget_effect_ref(ref: ControlRequestId) -> ControlEffectId:
    """Return the effect id a budget termination of *ref* names.

    Derived from the request rather than supplied, so a retry of the verb
    names the same effect and the ledger cannot grow two ids for one reap.
    """
    return f"EFF-{hashlib.sha256(ref.encode('utf-8')).hexdigest()[:16]}"


def _terminal_updates(run: Run, *, now: datetime) -> dict[str, Any]:
    """Return the fields the Run's terminal edge makes facts.

    A Run cancelled out of the queue never started, so the edge requires
    the start stamp as well: the duration of a stopped Run is a recorded
    fact rather than a difference against now.
    """
    updates: dict[str, Any] = {"ended_at": now.isoformat()}
    if run.status is RunStatus.QUEUED:
        updates["started_at"] = now.isoformat()
    if run.status is RunStatus.SUSPENDED:
        updates["suspension_reason"] = None
    return updates


class _BudgetLedger:
    """Write a budget termination to the run ledger in its two steps.

    Both steps re-read the ledger under the root's entity lock before they
    sequence anything. The kill ladder runs between them and holds no lock,
    so another principal's control may have appended in the meantime;
    sequencing the effect against the snapshot the open left behind would
    hand two facts the same position and leave the ledger non-contiguous.
    """

    def __init__(self, context: Epoch2RootContext, args: _MeterParams, *, now: datetime) -> None:
        self._context = context
        self._args = args
        self._now = now
        self._opened = False
        self.notice: BudgetNotice | None = None
        self.envelopes: list[Envelope] = []

    def open(self, notice: BudgetNotice) -> bool:
        """Record the reading and ask for the control it justifies.

        Returns:
            ``True`` when the budget control took the Run's one control
            lease. ``False`` when another principal's control holds it, in
            which case the notice still stands and this guard signals
            nothing.
        """
        with self._context.session([self._args.urn]) as session:
            records = read_ledger_records(_ledger(session))
            facts = _control_facts(records, self._args.urn)
            _stored_run(session, records, self._args.urn)
            # A retry reports the notice the ledger HOLDS, not the one it
            # just built: the two differ by their timestamps, and answering
            # with a reading no durable record carries would make the same
            # crossing look like a second one.
            standing = _notice_of(records, notice.control_request_ref)
            if standing is None:
                self.envelopes.append(_append_notice(session, notice, now=self._now))
            self.notice = standing if standing is not None else notice
            facts = self._request(session, facts)
            facts, disposition = self._acknowledge(session, facts)
        self._opened = True
        logger.info(
            f"open budget-termination run={self._args.urn.entity_key!r} "
            f"request={self._args.control_request_ref!r} disposition={disposition.value}"
        )
        return disposition is ControlDisposition.ACCEPTED

    def confirm(self, notice: BudgetNotice, termination: TerminationResult) -> None:
        """Record the reap the kill ladder observed and move the record.

        Raises:
            DaemonValidationError: The effect was confirmed before the
                notice was opened, so no accepted control earned it.
        """
        if not self._opened:
            raise DaemonValidationError(
                "validation_failed: illegal_transition: a budget effect was confirmed before "
                "its notice was opened"
            )
        with self._context.session([self._args.urn]) as session:
            records = read_ledger_records(_ledger(session))
            facts = _control_facts(records, self._args.urn)
            run = _stored_run(session, records, self._args.urn)
            standing = _standing_fact(
                facts, ref=self._args.control_request_ref, phase=ControlPhase.EFFECTED
            )
            if standing is None:
                fact = self._effect_fact(facts)
                self.envelopes.append(_append_fact(session, fact, now=self._now))
                facts = (*facts, fact)
        logger.info(
            f"confirm budget-termination run={self._args.urn.entity_key!r} "
            f"sigkill={termination.sigkill_sent} observed={notice.observed_tokens}"
        )
        transition = self._commit(run, facts)
        if transition is not None:
            self.envelopes.append(transition)

    def _commit(self, run: Run, facts: tuple[ControlFact, ...]) -> Envelope | None:
        """Commit the transition the confirmed effect makes a fact, if any.

        The reducer decides, not the caller: terminality is read out of the
        fold over the control facts rather than off the stored record, so an
        effect landing inside a torn window moves nothing twice.
        """
        state = reduce_run_control(status=run.status, facts=facts)
        if state.status is run.status:
            return None
        committed = run_transaction(
            context=self._context,
            request=TransitionRequest.model_validate(
                {
                    "urn": self._args.urn,
                    "to_status": str(state.status),
                    "expected_revision": run.revision,
                    "idempotency_key": self._args.idempotency_key,
                    "actor": self._args.actor,
                    "updates": _terminal_updates(run, now=self._now),
                    "reason_code": _EFFECT_REASON_CODE,
                    "binding_refs": (self._args.control_request_ref,),
                }
            ),
            now=self._now,
        )
        return committed.envelope

    def _request(
        self, session: RootSession, facts: tuple[ControlFact, ...]
    ) -> tuple[ControlFact, ...]:
        """Append the requested fact, or leave a replayed one standing."""
        if _standing_fact(facts, ref=self._args.control_request_ref, phase=ControlPhase.REQUESTED):
            return facts
        fact = self._fact(
            facts,
            phase=ControlPhase.REQUESTED,
            disposition=ControlDisposition.REQUESTING,
        )
        self.envelopes.append(_append_fact(session, fact, now=self._now))
        return (*facts, fact)

    def _acknowledge(
        self, session: RootSession, facts: tuple[ControlFact, ...]
    ) -> tuple[tuple[ControlFact, ...], ControlDisposition]:
        """Decide the Run's control lease and append what was decided."""
        standing = _standing_fact(
            facts, ref=self._args.control_request_ref, phase=ControlPhase.ACKNOWLEDGED
        )
        if standing is not None:
            return facts, standing.disposition
        decision = decide_control_lease(
            control_request_ref=self._args.control_request_ref, facts=facts
        )
        fact = self._fact(facts, phase=ControlPhase.ACKNOWLEDGED, disposition=decision.disposition)
        self.envelopes.append(_append_fact(session, fact, now=self._now))
        return (*facts, fact), decision.disposition

    def _effect_fact(self, facts: tuple[ControlFact, ...]) -> ControlFact:
        """Build the confirmed effect of the reap that was observed."""
        return self._fact(
            facts,
            phase=ControlPhase.EFFECTED,
            disposition=ControlDisposition.CONFIRMED,
            effect_ref=_budget_effect_ref(self._args.control_request_ref),
        )

    def _fact(
        self,
        facts: tuple[ControlFact, ...],
        *,
        phase: ControlPhase,
        disposition: ControlDisposition,
        effect_ref: ControlEffectId | None = None,
    ) -> ControlFact:
        """Build one control fact at the next position of *facts*."""
        return ControlFact(
            control_request_ref=self._args.control_request_ref,
            run_ref=self._args.urn,
            control=BUDGET_TERMINATION_CONTROL,
            phase=phase,
            disposition=disposition,
            effect_ref=effect_ref,
            actor=self._args.actor,
            recorded_at=self._now,
            sequence=len(facts) + 1,
        )


def _meter(
    context: Epoch2RootContext, args: _MeterParams, *, now: datetime
) -> tuple[RunBudgetAnswer, tuple[Envelope, ...]]:
    """Meter the turn so far and terminate the Run if it crossed its cap.

    Raises:
        DaemonValidationError: The Run has no record, or the reading is
            not classifiable against the supplied cap.
        TransactionRefusedError: The canonical transition was refused.
    """
    reading = meter_stream(args.samples)
    ledger = _BudgetLedger(context, args, now=now)
    try:
        outcome = guard_in_flight_budget(
            reading=reading,
            base_budget=args.base_budget,
            enforce=args.enforce,
            multiplier=args.multiplier,
            run_ref=args.urn,
            control_request_ref=args.control_request_ref,
            noticed_at=now,
            pgid=args.pgid,
            ledger=ledger,
        )
    except ValueError as error:
        raise DaemonValidationError(
            f"validation_failed: schema_validation_failed: {error}"
        ) from error
    with context.session([args.urn]) as session:
        records = read_ledger_records(_ledger(session))
        facts = _control_facts(records, args.urn)
        run = _stored_run(session, records, args.urn)
    state = reduce_run_control(status=run.status, facts=facts)
    recorded = ledger.notice if outcome.notice is not None else None
    answer = RunBudgetAnswer(
        observed_tokens=reading.observed_tokens,
        output_tokens=reading.output_tokens,
        cap_tokens=outcome.decision.cap,
        over_cap=outcome.decision.over_cap,
        action=outcome.decision.action.value,
        terminated=outcome.terminated,
        led=outcome.led,
        notice=recorded.model_dump(mode="json") if recorded is not None else None,
        run_status=state.status,
        control_cursor=state.control_cursor,
    )
    return answer, tuple(ledger.envelopes)


class InFlightRunMeter:
    """Meter one dispatched Run reading by reading, and reap it at its cap.

    The verb above folds a batch a caller already collected; this is the
    producer the native dispatch drives as the child discloses usage, so
    the cap is tested on every reading while the turn still runs. It trips
    once: after a reading crosses and the notice is written, later readings
    are answered from the recorded outcome, because a stream keeps flushing
    buffered readings after the reap and each of them would otherwise open
    a second control and signal a group that is already gone.

    Attributes:
        outcome: The outcome of the reading that crossed the cap, or
            ``None`` while every reading has stayed under it.
    """

    def __init__(
        self,
        context: Epoch2RootContext,
        *,
        urn: RunUrn,
        actor: PrincipalKey,
        control_request_ref: ControlRequestId,
        idempotency_key: str,
        cap_tokens: int,
    ) -> None:
        """Bind the meter to one Run and its hard token cap.

        Args:
            context: The native context of the root the Run lives in.
            urn: The Run the readings are of.
            actor: The principal the termination is attributed to.
            control_request_ref: The control a crossing opens. Derived by
                the caller from the dispatch attempt, so a resumed dispatch
                names the same control.
            idempotency_key: The key the terminal transition commits under.
            cap_tokens: The sealed token ceiling, enforced exactly: a
                capsule ceiling is a limit, not a baseline to scale.

        Raises:
            pydantic.ValidationError: An argument breaks the verb's own
                parameter grammar.
        """
        self._context = context
        self._args = _MeterParams(
            urn=urn,
            control_request_ref=control_request_ref,
            actor=actor,
            base_budget=cap_tokens,
            enforce="hard",
            multiplier=1.0,
            idempotency_key=idempotency_key,
        )
        self._meter = InFlightMeter()
        self.outcome: InFlightBudgetOutcome | None = None

    @property
    def terminated(self) -> bool:
        """Return whether a crossing drove the kill ladder."""
        return self.outcome is not None and self.outcome.terminated

    async def observe(self, sample: UsageSample, pgid: int | None) -> bool:
        """Adopt *sample* and terminate the Run if the fold crossed its cap.

        Args:
            sample: The child's cumulative usage at this point in the turn.
            pgid: The child's process group, or ``None`` when none is
                addressable; a crossing then records its notice and
                signals nothing.

        Returns:
            ``True`` once the Run was terminated at its cap.
        """
        if self.outcome is not None:
            return self.terminated
        reading = self._meter.observe(sample)
        outcome = await asyncio.to_thread(self._guard, reading, pgid, datetime.now(UTC))
        if outcome.notice is not None:
            self.outcome = outcome
        return self.terminated

    def _guard(
        self, reading: MeterReading, pgid: int | None, now: datetime
    ) -> InFlightBudgetOutcome:
        """Test *reading* against the cap, writing through the run ledger."""
        return guard_in_flight_budget(
            reading=reading,
            base_budget=self._args.base_budget,
            enforce=self._args.enforce,
            multiplier=self._args.multiplier,
            run_ref=self._args.urn,
            control_request_ref=self._args.control_request_ref,
            noticed_at=now,
            pgid=pgid,
            ledger=_BudgetLedger(self._context, self._args, now=now),
        )


@native_mutator(RUN_BUDGET_METER_METHOD)
async def _meter_run_budget(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Meter a turn in flight and terminate the Run at its cap.

    The notice and the control reach the ledger while the child is still
    running, and the confirmed effect is appended only once the kill
    ladder has seen the group die. Every committed line and transition is
    published once the locks are released; a replay that wrote nothing
    publishes nothing.
    """
    args = _validated(params)
    context = ctx.native_root_context(authority.root)
    try:
        answer, envelopes = await asyncio.to_thread(_meter, context, args, now=datetime.now(UTC))
    except TransactionRefusedError as refusal:
        logger.info(f"_meter_run_budget refused code={refusal.code.value}")
        raise DaemonValidationError(
            f"validation_failed: {refusal.code.value}: {refusal.detail}"
        ) from refusal
    published = [publish_projection(ctx.bus, envelope) for envelope in envelopes]
    if not all(published):
        answer = answer.model_copy(update={"warnings": (PROJECTION_DEGRADED,)})
    return answer.model_dump(mode="json")


__all__ = [
    "RUN_BUDGET_METER_METHOD",
    "InFlightRunMeter",
    "RunBudgetAnswer",
]
