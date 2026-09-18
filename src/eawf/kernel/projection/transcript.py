"""The transcript read model: a Run's blocks in order, its holes, and what it is doing.

One route answers what an agent has been doing, and the thing that makes an answer
readable is that it never invents the parts it does not hold.

*Order comes from the stream, not from the file.* The blocks are the Run's event lines in
``run_sequence`` order, which is the order the daemon assigned; ledger order is arrival
order and the two differ whenever a line was retried. Quarantined lines are left out of
the run entirely, exactly as the reducer leaves them out of every other derivation: a
line retained as a diagnostic is not part of the history, and drawing it in sequence
would put a repudiated observation in the middle of the story.

*A hole is drawn, not skipped.* A recorded gap covers a range of sequences the daemon
never received, so the console does not hold them and never will. The range becomes a
block of its own in the position it occupies, carrying a value in the
:attr:`~eawf.kernel.projection.truth.TruthState.PURGED` state that names how many
sequences are missing. A transcript that silently renumbered around the hole would be
shorter than the episode it claims to show, and nothing on the frame would say so.

*The thinking state is derived and says it is.* No event states "the agent is thinking":
what exists is a reasoning turn that opened and has not been summarized. The state is
folded out of that pairing and carried as a
:class:`~eawf.kernel.projection.truth.TruthField` whose kind is
:attr:`~eawf.kernel.projection.truth.TruthKind.DERIVED`, so a frame drawing it is
drawing an inference the read model owns rather than a fact a provider reported.

Nothing here reads a ledger or a lock. The inputs are one validated
:class:`~eawf.kernel.projection.compute.RouteProjection` and the already-validated event
records the caller holds.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final

from eawf.kernel.projection.compute import PROJECTION_PRODUCER, RouteProjection
from eawf.kernel.projection.route_view import (
    RouteFieldSpec,
    RouteReadModel,
    build_route_read_model,
    check_field_tables,
    known_field,
    status_and,
    unknown_field,
    unstated,
)
from eawf.kernel.projection.truth import (
    Freshness,
    Precision,
    TruthField,
    TruthKind,
    TruthState,
)
from eawf.kernel.runtime.events import (
    CommandPayload,
    EventGapPayload,
    ReasoningSummaryPayload,
    RunEventKind,
    RunEventRecord,
)
from eawf.kernel.state.enums import MeasurementQuality
from eawf.runtime.daemon.run_events import reduce_run_events

logger = logging.getLogger(__name__)

#: The family name a refusal from this module names.
FAMILY: Final = "transcript"

#: The one console route this module states a read model for.
TRANSCRIPT_ROUTE: Final = "transcript"

#: The routes of this family, stated as a tuple so the family check reads like the others.
TRANSCRIPT_ROUTES: Final[tuple[str, ...]] = (TRANSCRIPT_ROUTE,)

#: The item whose producer would state what a Run cost and how it ended. Those are
#: metering and outcome facts rather than stream facts, so no event line carries them.
RUN_OUTCOME_PRODUCER: Final = "RUN-061 run outcome and metering rollup"

#: The revision every derived transcript cell states. A block is folded out of one line
#: that is never revised, so the first revision is the only one it stands at.
BLOCK_REVISION: Final = 1

#: What the transcript states while a reasoning turn is open.
THINKING: Final = "thinking"

#: Why the transcript states no thinking. The absence of an open turn is the whole
#: derivation, so the reason names it rather than blaming a missing producer.
NO_OPEN_TURN_REASON: Final = "no reasoning turn is open, so nothing states a thought in flight"

#: What a block stands for when its sequences were never received.
PURGED_REASON: Final = "sequences {first}-{last} never reached the daemon, so none is held"

#: What a reasoning block says before its summary exists.
OPEN_TURN_TEXT: Final = "a reasoning turn opened and has not been summarized"

#: Why a block states no text. A supported kind whose payload carries no renderable
#: sentence is a line the console holds and cannot read aloud.
NO_TEXT_REASON: Final = "this event kind states no text the transcript can render"

#: What each transcript row renders, in column order.
TRANSCRIPT_FIELDS: Final[Mapping[str, tuple[RouteFieldSpec, ...]]] = MappingProxyType(
    {
        TRANSCRIPT_ROUTE: status_and(
            unstated("outcome", missing_producer=RUN_OUTCOME_PRODUCER),
            unstated("cost", missing_producer=RUN_OUTCOME_PRODUCER),
        )
    }
)


check_field_tables(family=FAMILY, routes=TRANSCRIPT_ROUTES, fields=TRANSCRIPT_FIELDS)


@dataclass(frozen=True, slots=True, kw_only=True)
class PurgedRange:
    """A run of sequences the console does not hold.

    Attributes:
        first: The first missing sequence.
        last: The last missing sequence, which is the observed sequence less one.
        gap_ref: The recorded gap's own identity.
        replay_requested: Whether the daemon asked the provider to replay the range.
        replay_available: Whether replay was available to ask for.
    """

    first: int
    last: int
    gap_ref: str
    replay_requested: bool
    replay_available: bool

    @property
    def count(self) -> int:
        """Return how many sequences the range covers; never fewer than one."""
        return self.last - self.first + 1


@dataclass(frozen=True, slots=True, kw_only=True)
class TranscriptBlock:
    """One block of a Run's transcript, in stream order.

    Attributes:
        sequence: The block's position in the Run's stream. A purged block carries the
            first missing sequence of its range.
        kind: The event kind the block was folded out of.
        at: When the daemon recorded the line. The provider's own stamp is not used: a
            worker that has stopped responding is in no position to be believed about
            what time it is.
        text: What the block says, or the token naming why it says nothing. A purged
            block's value is in the purged state rather than the unknown one, because
            the console knows precisely what is missing.
        purged: The range this block stands for, or ``None`` for an observed event.
    """

    sequence: int
    kind: RunEventKind
    at: datetime
    text: TruthField[str]
    purged: PurgedRange | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TranscriptReadModel(RouteReadModel):
    """The transcript route's read model: the projection's rows and the Run's blocks.

    Attributes:
        blocks: The Run's blocks in stream order, purged ranges in their own positions.
        purged: Every range the console does not hold, in stream order.
        thinking: Whether a reasoning turn is open, derived and labelled derived.
        last_contiguous_sequence: The highest sequence with nothing missing in front of
            it; zero for a Run that has produced nothing.
        derivation_stopped: Whether a hole sits between the start of the stream and its
            end, which is what makes the block run shorter than the episode.
        quarantined: The lines retained as diagnostics and drawn by nothing.
    """

    blocks: tuple[TranscriptBlock, ...] = ()
    purged: tuple[PurgedRange, ...] = ()
    thinking: TruthField[str]
    last_contiguous_sequence: int = 0
    derivation_stopped: bool = False
    quarantined: int = 0

    def block_at(self, position: int) -> TranscriptBlock | None:
        """Return the block at a zero-based offset, or ``None`` past either end."""
        if position < 0 or position >= len(self.blocks):
            return None
        return self.blocks[position]

    def is_thinking(self) -> bool:
        """Return whether a reasoning turn is open right now."""
        return self.thinking.value == THINKING


def _derived(value: str, *, urn: str) -> TruthField[str]:
    """Return one stated transcript cell, labelled derived."""
    return known_field(value=value, urn=urn, revision=BLOCK_REVISION)


def _purged_field(purged: PurgedRange, *, urn: str) -> TruthField[str]:
    """Return the cell a purged range renders as: no value, and exactly why.

    The purged state is used rather than the unknown one on purpose. Unknown says the
    projection never learned a value; purged says the store does not hold one and the
    console can name the range it would have covered.
    """
    return TruthField[str](
        value=None,
        state=TruthState.PURGED,
        truth_kind=TruthKind.DERIVED,
        producer=PROJECTION_PRODUCER,
        producer_revision=BLOCK_REVISION,
        precision=Precision.UNAVAILABLE,
        measurement_quality=MeasurementQuality.UNAVAILABLE,
        freshness=Freshness.LIVE,
        provenance_refs=(urn,),
        missing_reason=PURGED_REASON.format(first=purged.first, last=purged.last),
    )


def block_text(event: RunEventRecord) -> TruthField[str]:
    """Return what one observed event says, or the cell naming why it says nothing.

    The payload union is declared to grow: forty event kinds are canonical and three
    payload shapes exist. A kind whose payload states no sentence the transcript can
    read aloud comes back unknown with :data:`NO_TEXT_REASON` rather than blank, so the
    console keeps drawing a stream that has outgrown it instead of refusing to.

    Args:
        event: One live event line.

    Returns:
        The block's text as a derived truth field.
    """
    urn = event.event_ref
    payload = event.payload
    if isinstance(payload, ReasoningSummaryPayload):
        summary = payload.summary
        return _derived(summary if summary is not None else OPEN_TURN_TEXT, urn=urn)
    if isinstance(payload, CommandPayload):
        detail = f"{payload.command_family_ref} · {payload.execution} · {payload.phase}"
        if payload.outcome is not None:
            detail += f" · {payload.outcome}"
        return _derived(detail, urn=urn)
    return unknown_field(urn=urn, revision=BLOCK_REVISION, reason=NO_TEXT_REASON)


def _purged_range(payload: EventGapPayload) -> PurgedRange:
    """Return the range one recorded gap covers.

    The gap states the sequence it expected and the one that arrived instead, so the
    missing run ends one before the observed sequence.
    """
    return PurgedRange(
        first=payload.expected_sequence,
        last=payload.observed_sequence - 1,
        gap_ref=payload.gap_ref,
        replay_requested=payload.replay_requested,
        replay_available=payload.replay_capability_state == "verified",
    )


def open_reasoning_turn(events: Sequence[RunEventRecord]) -> RunEventRecord | None:
    """Return the event that opened the reasoning turn still open, or ``None``.

    A turn opens on ``reasoning_started`` and closes on ``reasoning_summarized``. The
    walk is over the stream in sequence order rather than in arrival order, and it skips
    quarantined lines, so a repudiated line neither opens a turn nor closes one.

    Args:
        events: The Run's event lines, in any order.

    Returns:
        The opening event of the turn that has not been summarized, or ``None`` when
        every turn was closed and when none was ever opened.
    """
    opened: RunEventRecord | None = None
    for event in _ordered(events):
        if event.event_kind is RunEventKind.REASONING_STARTED:
            opened = event
        elif event.event_kind is RunEventKind.REASONING_SUMMARIZED:
            opened = None
    return opened


def _ordered(events: Sequence[RunEventRecord]) -> tuple[RunEventRecord, ...]:
    """Return the stream's live lines in sequence order, quarantined lines dropped."""
    return tuple(
        sorted(
            (event for event in events if event.quarantine is None),
            key=lambda event: event.run_sequence,
        )
    )


def _thinking_field(events: Sequence[RunEventRecord], *, scope_id: str) -> TruthField[str]:
    """Return whether a reasoning turn is open, as a derived cell that says so."""
    opened = open_reasoning_turn(events)
    if opened is None:
        return unknown_field(urn=scope_id, revision=BLOCK_REVISION, reason=NO_OPEN_TURN_REASON)
    return _derived(THINKING, urn=opened.event_ref)


def build_transcript_blocks(
    events: Sequence[RunEventRecord],
) -> tuple[tuple[TranscriptBlock, ...], tuple[PurgedRange, ...]]:
    """Return the Run's blocks in stream order and the ranges the console does not hold.

    Args:
        events: The Run's event lines, in any order. Quarantined lines are dropped.

    Returns:
        One block per live line, a recorded gap becoming a purged block in its own
        position, and the purged ranges on their own in the same order.
    """
    blocks: list[TranscriptBlock] = []
    purged: list[PurgedRange] = []
    for event in _ordered(events):
        if isinstance(event.payload, EventGapPayload):
            covered = _purged_range(event.payload)
            purged.append(covered)
            blocks.append(
                TranscriptBlock(
                    sequence=event.run_sequence,
                    kind=event.event_kind,
                    at=event.recorded_at,
                    text=_purged_field(covered, urn=event.event_ref),
                    purged=covered,
                )
            )
            continue
        blocks.append(
            TranscriptBlock(
                sequence=event.run_sequence,
                kind=event.event_kind,
                at=event.recorded_at,
                text=block_text(event),
            )
        )
    logger.debug(f"build_transcript_blocks blocks={len(blocks)} purged={len(purged)}")
    return tuple(blocks), tuple(purged)


def build_transcript_view(
    projection: RouteProjection,
    *,
    events: Sequence[RunEventRecord] = (),
) -> TranscriptReadModel:
    """Return the read model the transcript route draws from one served projection.

    Args:
        projection: The route projection the daemon answered, already validated.
        events: The Run's event lines, in any order; empty for a Run that has produced
            nothing, which draws no block rather than a block saying nothing.

    Returns:
        The route's rows, the Run's blocks, the ranges the console does not hold, and
        the derived thinking state.

    Raises:
        ValueError: The projection is for another route, or the live lines are not a
            run of sequences once every recorded gap is accounted for. An unexplained
            hole means a line was lost, and folding over the survivors would draw a
            transcript that skips it without saying so.
    """
    model = build_route_read_model(projection, family=FAMILY, fields=TRANSCRIPT_FIELDS)
    state = reduce_run_events(events)
    blocks, purged = build_transcript_blocks(events)
    return TranscriptReadModel(
        route=model.route,
        read_model=model.read_model,
        scope_id=model.scope_id,
        source_cursor=model.source_cursor,
        digest=model.digest,
        complete=model.complete,
        rows=model.rows,
        counts=model.counts,
        specs=model.specs,
        blocks=blocks,
        purged=purged,
        thinking=_thinking_field(events, scope_id=model.scope_id),
        last_contiguous_sequence=state.last_contiguous_sequence,
        derivation_stopped=state.derivation_stopped,
        quarantined=len(state.quarantined),
    )


__all__ = [
    "BLOCK_REVISION",
    "FAMILY",
    "NO_OPEN_TURN_REASON",
    "NO_TEXT_REASON",
    "OPEN_TURN_TEXT",
    "PURGED_REASON",
    "RUN_OUTCOME_PRODUCER",
    "THINKING",
    "TRANSCRIPT_FIELDS",
    "TRANSCRIPT_ROUTE",
    "TRANSCRIPT_ROUTES",
    "PurgedRange",
    "TranscriptBlock",
    "TranscriptReadModel",
    "block_text",
    "build_transcript_blocks",
    "build_transcript_view",
    "open_reasoning_turn",
]
