"""Order a Run's events, explain the holes, and say when it went quiet.

Three answers live here and the verbs above do the IO for them.

*What is the stream?* :func:`reduce_run_events` walks a Run's event
lines in sequence order and reports the last sequence it can derive
from. A recorded gap stops that derivation where the hole starts: the
later events are still readable, but they are no longer a history with
nothing missing in front of them, and saying so is the whole reason the
gap is a typed line rather than a skipped number.

*What happens to this event?* :func:`plan_event_append` decides before
anything is written. A repeat of an event already on the ledger appends
nothing and answers with the line that already stands. A sequence that
jumps ahead gets a typed gap line covering the missing range, appended
in front of the event that revealed it. An event arriving after the Run
already ended is written quarantined: retained as a diagnostic, derived
from by nothing, and unable to move a status that has stopped.

Terminality is read from the control ledger's *effect truth*, not from
the stored Run record. The two legitimately disagree for a while, because
a control effect is appended before the transition it causes; a check
against the record would admit exactly the events that arrive inside that
window. A check against the reduced status refuses them.

*Is it still alive?* :func:`assess_stall` compares the daemon's own
recording clock against the stall interval. It anchors on
``recorded_at`` rather than on any provider timestamp, because a worker
that has stopped responding is in no position to be believed about what
time it is, and it returns an observation rather than a transition: a
stall is the fact that makes a lost Run a deliberate call instead of a
timeout guess.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.runtime.compiled import canonical_digest
from eawf.kernel.runtime.events import (
    SUPPORTED_EVENT_KINDS,
    EventGapId,
    EventGapPayload,
    EventProvenance,
    QuarantineReason,
    RunEventId,
    RunEventKind,
    RunEventPayload,
    RunEventRecord,
)
from eawf.kernel.runtime.handshake import WorkerHelloFact
from eawf.kernel.runtime.provider import ControlKind
from eawf.kernel.state.epoch2.base import PrincipalKey, StrictPositiveInt
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.ledger import LedgerRecord

logger = logging.getLogger(__name__)

#: How long a Run may produce nothing before it is flagged stalled. Six
#: hundred seconds is the measured boundary of the unrecovered stalls
#: that motivated the check, not a chosen round number.
DEFAULT_STALL_INTERVAL_SECONDS: Final = 600

#: The control a stalled Run is resumed through. A stall never moves the
#: Run itself, so the resume path is a control a principal asks for.
STALL_RESUME_CONTROL: Final = ControlKind.RESUME


class AppendDisposition(StrEnum):
    """What one append request did to the stream."""

    APPENDED = "appended"
    DUPLICATE = "duplicate"
    GAP_RECORDED = "gap_recorded"
    QUARANTINED = "quarantined"


class RunLiveness(StrEnum):
    """Whether a Run is still producing.

    ``UNKNOWN`` is a real answer rather than an optimistic ``LIVE``: a
    Run that has recorded no activity at all has nothing to measure
    silence against, and reporting it as alive would be a guess.
    """

    LIVE = "live"
    STALLED = "stalled"
    UNKNOWN = "unknown"


class RunScopedParams(BaseModel):
    """The Run a run-event request addresses."""

    model_config = ConfigDict(extra="forbid")

    urn: RunUrn


class RunEventAppend(RunScopedParams):
    """One proposed event line, as the observer submits it.

    Attributes:
        event_ref: The event's identity. A retry repeats it, which is
            how a duplicate is recognised without comparing bytes.
        run_sequence: Where the observer says this event sits.
        event_kind: What happened.
        provenance: Who observed it.
        payload: The typed detail the kind admits.
        actor: The principal the observation is attributable to.
        observed_at: The observer's own timestamp, which may be absent.
        replay_capability: Whether the provider can replay a missing
            range, which decides whether a recorded gap asks for one.
    """

    event_ref: RunEventId
    run_sequence: StrictPositiveInt
    event_kind: RunEventKind
    provenance: EventProvenance = "provider_native"
    payload: RunEventPayload
    actor: PrincipalKey
    observed_at: UtcDatetime | None = None
    replay_capability: Literal["verified", "unavailable"] = "unavailable"


class RunEventsRead(RunScopedParams):
    """A read of one Run's stream and its liveness.

    Attributes:
        stall_interval_seconds: The silence a Run is allowed before it
            is flagged. It is overridable because a pause that is
            routine on one runtime is pathological on another.
    """

    stall_interval_seconds: Annotated[StrictInt, Field(ge=0, le=86_400)] = (
        DEFAULT_STALL_INTERVAL_SECONDS
    )


@dataclass(frozen=True, slots=True)
class RunEventState:
    """What a Run's event lines reduce to.

    Attributes:
        last_contiguous_sequence: The highest sequence reachable with
            nothing missing in front of it, or zero for an empty stream.
        next_sequence: The sequence the next event is expected to carry.
        gaps: Every recorded hole, in stream order.
        quarantined: The lines retained as diagnostics, in ledger order.
        derivation_stopped: Whether a gap sits between the start of the
            stream and its end.
        last_activity_at: When the Run last produced something, as the
            daemon recorded it.
        last_activity_kind: What it last produced.
    """

    last_contiguous_sequence: int
    next_sequence: int
    gaps: tuple[EventGapPayload, ...]
    quarantined: tuple[RunEventRecord, ...]
    derivation_stopped: bool
    last_activity_at: datetime | None
    last_activity_kind: RunEventKind | None


@dataclass(frozen=True, slots=True)
class AppendPlan:
    """What the daemon will write for one append request.

    Attributes:
        disposition: What the request did.
        lines: The records to append, in order. A gap line precedes the
            event that revealed it, and a duplicate writes nothing.
        event: The line that now stands for the request, whether it was
            just built or was already on the ledger.
        gap: The hole this append recorded, or ``None``.
        reason: One sentence an operator reads.
    """

    disposition: AppendDisposition
    lines: tuple[RunEventRecord, ...]
    event: RunEventRecord
    gap: EventGapPayload | None
    reason: str


@dataclass(frozen=True, slots=True)
class StallAssessment:
    """Whether a Run has been silent past its interval.

    Attributes:
        verdict: Live, stalled, or unknown for a Run with no activity to
            measure against.
        last_activity_at: The activity the silence is measured from.
        last_activity_kind: What that activity was.
        elapsed_seconds: How long the Run has produced nothing.
        interval_seconds: The interval it was measured against.
        resume_control: The control a principal asks for to resume it. A
            stall never terminates the Run, so this is an offer rather
            than something the daemon does.
    """

    verdict: RunLiveness
    last_activity_at: datetime | None
    last_activity_kind: RunEventKind | None
    elapsed_seconds: float
    interval_seconds: int
    resume_control: ControlKind


def run_events_of(records: Sequence[LedgerRecord], urn: QualifiedUrn) -> tuple[RunEventRecord, ...]:
    """Return one Run's event lines from the run ledger, in ledger order.

    Args:
        records: Every line the run ledger holds.
        urn: The Run to select.

    Returns:
        The Run's event lines, in the order they were appended. Sequence
        order is imposed by the reducer, because a quarantined line may
        carry a sequence another line already holds.

    Raises:
        pydantic.ValidationError: A line claims to be a run event and
            does not validate as one, which means the ledger is corrupt
            rather than merely unfamiliar.
    """
    events = [
        RunEventRecord.model_validate(item.payload)
        for item in records
        if item.payload.get("payload_kind") == "run_event"
    ]
    return tuple(event for event in events if event.run_ref == urn)


def hello_facts_of(
    records: Sequence[LedgerRecord], urn: QualifiedUrn
) -> tuple[WorkerHelloFact, ...]:
    """Return one Run's handshake facts from the run ledger, in ledger order.

    Args:
        records: Every line the run ledger holds.
        urn: The Run to select.

    Returns:
        The Run's handshake facts, in the order they were appended.

    Raises:
        pydantic.ValidationError: A line claims to be a handshake fact
            and does not validate as one.
    """
    facts = [
        WorkerHelloFact.model_validate(item.payload)
        for item in records
        if item.payload.get("payload_kind") == "worker_hello"
    ]
    return tuple(fact for fact in facts if fact.run_ref == urn)


def reduce_run_events(events: Sequence[RunEventRecord]) -> RunEventState:
    """Fold a Run's event lines into its cursor, its holes and its clock.

    Args:
        events: Every event line of the Run, in ledger order.

    Returns:
        The reduced state. Quarantined lines are carried through and
        derived from by nothing: they set no cursor, explain no hole and
        never refresh the liveness clock.

    Raises:
        ValueError: The live lines are not the run ``1..n`` once every
            recorded gap is accounted for. An unexplained hole means a
            ledger line was lost, and folding over the survivors would
            report a cursor that skips it.
    """
    live = sorted(
        (event for event in events if event.quarantine is None), key=lambda item: item.run_sequence
    )
    quarantined = tuple(event for event in events if event.quarantine is not None)
    expected = 1
    last_contiguous = 0
    stopped = False
    gaps: list[EventGapPayload] = []
    last_at: datetime | None = None
    last_kind: RunEventKind | None = None
    for event in live:
        if event.run_sequence != expected:
            raise ValueError(
                f"run event stream is not contiguous: expected sequence {expected}, "
                f"found {event.run_sequence}"
            )
        if event.event_kind is RunEventKind.EVENT_GAP:
            gap = _gap_payload(event)
            gaps.append(gap)
            stopped = True
            expected = gap.observed_sequence
            continue
        if not stopped:
            last_contiguous = event.run_sequence
        last_at = event.recorded_at
        last_kind = event.event_kind
        expected = event.run_sequence + 1
    return RunEventState(
        last_contiguous_sequence=last_contiguous,
        next_sequence=expected,
        gaps=tuple(gaps),
        quarantined=quarantined,
        derivation_stopped=stopped,
        last_activity_at=last_at,
        last_activity_kind=last_kind,
    )


def plan_event_append(
    *,
    request: RunEventAppend,
    events: Sequence[RunEventRecord],
    terminal: bool,
    gap_ref: EventGapId,
    now: datetime,
) -> AppendPlan:
    """Decide what one append request writes, before anything is written.

    Args:
        request: The proposed event.
        events: Every event line the Run already holds, in ledger order.
        terminal: Whether the Run's confirmed control effects have ended
            it. This is the reduced status, never the stored record's,
            so an event arriving between a confirmed effect and the
            transition it causes is refused rather than admitted.
        gap_ref: The identity a recorded gap would carry. It is minted by
            the caller so this decision stays reproducible.
        now: The daemon's recording clock.

    Returns:
        The plan. Its ``lines`` are appended in order by the caller.

    Raises:
        ValueError: The event kind has no payload model yet; an event id
            already on the ledger is repeated with different content; or
            the sequence falls inside a recorded gap, which is a replay
            fill rather than an append.
    """
    if request.event_kind not in SUPPORTED_EVENT_KINDS:
        raise ValueError(
            f"event kind {request.event_kind.value!r} is declared but carries no payload model "
            "yet, so no line is appended for it"
        )
    standing = _standing_event(events, request.event_ref)
    if standing is not None:
        return _duplicate_plan(request=request, standing=standing)
    if terminal:
        quarantined = _record(request, now=now, quarantine=QuarantineReason.LATE_AFTER_TERMINAL)
        logger.info(
            f"plan_event_append quarantined run_sequence={request.run_sequence} "
            f"event_kind={request.event_kind.value}"
        )
        return AppendPlan(
            disposition=AppendDisposition.QUARANTINED,
            lines=(quarantined,),
            event=quarantined,
            gap=None,
            reason=(
                "this Run's confirmed effects already ended it, so the event is retained as a "
                "quarantined diagnostic and reopens nothing"
            ),
        )
    state = reduce_run_events(events)
    if request.run_sequence < state.next_sequence:
        return _conflict_plan(request=request, events=events, state=state, now=now)
    if request.run_sequence == state.next_sequence:
        appended = _record(request, now=now, quarantine=None)
        return AppendPlan(
            disposition=AppendDisposition.APPENDED,
            lines=(appended,),
            event=appended,
            gap=None,
            reason=f"the event took sequence {request.run_sequence}",
        )
    return _gap_plan(request=request, state=state, gap_ref=gap_ref, now=now)


def assess_stall(*, state: RunEventState, now: datetime, interval_seconds: int) -> StallAssessment:
    """Say whether a Run has produced nothing for longer than it may.

    Args:
        state: The Run's reduced event stream.
        now: The reference clock. It is passed in rather than read here
            so a caller can pin the exact instant a threshold is crossed.
        interval_seconds: The silence the Run is allowed. Zero means any
            measurable silence counts, which is how a test crosses the
            threshold without waiting.

    Returns:
        The assessment. A Run that has recorded nothing is ``unknown``
        rather than live, and no verdict moves the Run's status.
    """
    if state.last_activity_at is None:
        return StallAssessment(
            verdict=RunLiveness.UNKNOWN,
            last_activity_at=None,
            last_activity_kind=None,
            elapsed_seconds=0.0,
            interval_seconds=interval_seconds,
            resume_control=STALL_RESUME_CONTROL,
        )
    elapsed = (now - state.last_activity_at).total_seconds()
    verdict = RunLiveness.STALLED if elapsed >= interval_seconds else RunLiveness.LIVE
    if verdict is RunLiveness.STALLED:
        logger.info(
            f"assess_stall verdict=stalled elapsed_seconds={elapsed:.3f} "
            f"interval_seconds={interval_seconds}"
        )
    return StallAssessment(
        verdict=verdict,
        last_activity_at=state.last_activity_at,
        last_activity_kind=state.last_activity_kind,
        elapsed_seconds=elapsed,
        interval_seconds=interval_seconds,
        resume_control=STALL_RESUME_CONTROL,
    )


def next_hello_sequence(facts: Sequence[WorkerHelloFact]) -> int:
    """Return the sequence the next acceptable hello must carry.

    Args:
        facts: Every handshake fact of the Run, in ledger order.

    Returns:
        One past the highest recorded hello sequence, or one when the
        Run has never been announced.
    """
    if not facts:
        return 1
    return max(fact.hello.hello_sequence for fact in facts) + 1


def _standing_event(
    events: Sequence[RunEventRecord], event_ref: RunEventId
) -> RunEventRecord | None:
    """Return the line a retry of this event is already answered by."""
    for event in events:
        if event.event_ref == event_ref:
            return event
    return None


def _duplicate_plan(*, request: RunEventAppend, standing: RunEventRecord) -> AppendPlan:
    """Return the plan for an event id the ledger already holds.

    Raises:
        ValueError: The repeat carries different content under the same
            identity, so one of the two is not the event it claims to be.
    """
    repeated = (
        standing.run_sequence == request.run_sequence
        and standing.event_kind is request.event_kind
        and _payload_digest(standing.payload) == _payload_digest(request.payload)
    )
    if not repeated:
        raise ValueError(
            f"event {request.event_ref!r} is already recorded with different content, so the "
            "repeat is a different event wearing a recorded identity"
        )
    return AppendPlan(
        disposition=AppendDisposition.DUPLICATE,
        lines=(),
        event=standing,
        gap=None,
        reason=f"event {request.event_ref} is already recorded; nothing was appended",
    )


def _conflict_plan(
    *,
    request: RunEventAppend,
    events: Sequence[RunEventRecord],
    state: RunEventState,
    now: datetime,
) -> AppendPlan:
    """Return the plan for a sequence that is already spoken for.

    A gap marker does not speak for its own slot. It sits on the first
    missing sequence to keep the walk readable, but the event that
    belongs there was never received, so an arrival at that sequence is
    the missing event rather than a rival for a filled one.

    Raises:
        ValueError: The sequence falls inside a recorded gap, which is a
            replay fill rather than an append and is negotiated by the
            replay path instead.
    """
    held = any(
        event.quarantine is None
        and event.event_kind is not RunEventKind.EVENT_GAP
        and event.run_sequence == request.run_sequence
        for event in events
    )
    if not held:
        raise ValueError(
            f"sequence {request.run_sequence} falls inside a recorded gap of this Run, so it is "
            "a replay fill rather than an append"
        )
    quarantined = _record(request, now=now, quarantine=QuarantineReason.SEQUENCE_CONFLICT)
    logger.info(
        f"plan_event_append conflict run_sequence={request.run_sequence} "
        f"next_sequence={state.next_sequence}"
    )
    return AppendPlan(
        disposition=AppendDisposition.QUARANTINED,
        lines=(quarantined,),
        event=quarantined,
        gap=None,
        reason=(
            f"another event already holds sequence {request.run_sequence}, so this one is "
            "retained as a quarantined diagnostic"
        ),
    )


def _gap_plan(
    *, request: RunEventAppend, state: RunEventState, gap_ref: EventGapId, now: datetime
) -> AppendPlan:
    """Return the plan for a sequence that jumped ahead of the stream."""
    gap = EventGapPayload(
        expected_sequence=state.next_sequence,
        observed_sequence=request.run_sequence,
        replay_requested=request.replay_capability == "verified",
        replay_capability_state=request.replay_capability,
        gap_ref=gap_ref,
    )
    marker = RunEventRecord(
        event_ref=_gap_event_ref(gap_ref),
        run_ref=request.urn,
        run_sequence=state.next_sequence,
        event_kind=RunEventKind.EVENT_GAP,
        provenance="daemon_observed",
        payload=gap,
        actor=request.actor,
        recorded_at=now,
    )
    appended = _record(request, now=now, quarantine=None)
    logger.info(
        f"plan_event_append gap expected={gap.expected_sequence} "
        f"observed={gap.observed_sequence} replay_requested={gap.replay_requested}"
    )
    return AppendPlan(
        disposition=AppendDisposition.GAP_RECORDED,
        lines=(marker, appended),
        event=appended,
        gap=gap,
        reason=(
            f"sequences {gap.expected_sequence} through {gap.observed_sequence - 1} never "
            "arrived, so the hole is recorded and derivation stops at it"
        ),
    )


def _record(
    request: RunEventAppend, *, now: datetime, quarantine: QuarantineReason | None
) -> RunEventRecord:
    """Build the line one append request occupies."""
    return RunEventRecord(
        event_ref=request.event_ref,
        run_ref=request.urn,
        run_sequence=request.run_sequence,
        event_kind=request.event_kind,
        provenance=request.provenance,
        payload=request.payload,
        actor=request.actor,
        observed_at=request.observed_at,
        recorded_at=now,
        quarantine=quarantine,
    )


def _gap_event_ref(gap_ref: EventGapId) -> RunEventId:
    """Return the event identity a gap marker is filed under.

    The gap's own identity supplies the body, so the marker and the hole
    it describes are one fact rather than two that have to be joined.
    """
    return f"EVT-{gap_ref.split('-', 1)[1]}"


def _gap_payload(event: RunEventRecord) -> EventGapPayload:
    """Return the gap a gap-kind line carries.

    Raises:
        ValueError: The line is a gap kind whose payload is not a gap,
            which the record validator makes unreachable through the
            model and which a hand-built record could still reach.
    """
    if not isinstance(event.payload, EventGapPayload):
        raise ValueError(f"event {event.event_ref!r} is a gap kind carrying no gap payload")
    return event.payload


def _payload_digest(payload: RunEventPayload) -> str:
    """Return the canonical digest of one event payload."""
    return canonical_digest(payload.model_dump(mode="json"))


__all__ = [
    "DEFAULT_STALL_INTERVAL_SECONDS",
    "STALL_RESUME_CONTROL",
    "AppendDisposition",
    "AppendPlan",
    "RunEventAppend",
    "RunEventState",
    "RunEventsRead",
    "RunLiveness",
    "RunScopedParams",
    "StallAssessment",
    "assess_stall",
    "hello_facts_of",
    "next_hello_sequence",
    "plan_event_append",
    "reduce_run_events",
    "run_events_of",
]
