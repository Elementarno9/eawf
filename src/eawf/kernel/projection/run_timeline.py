"""A Run's event timeline: one row per event, with repeated low-priority rows coalesced.

The timeline answers what a Run did at a glance, and the rule that keeps it honest is that
coalescing only ever folds noise. Every event kind carries a priority. A ``P0`` event -- a
terminal transition, a decision or permission, a gap, a control, an error -- is a row of
its own, always. A run of adjacent ``P2`` events of one kind about one target, each
following the last in sequence and all inside :data:`COALESCE_WINDOW_SECONDS` of the first,
folds into one group that states its kind, how many events it holds, the sequences it
covers and how long it spanned. Anything else closes the group: another kind, another
target, a sequence that does not follow, or the window running out.

The grouping is a function of the Run's sequence alone. The lines are ordered by
``run_sequence`` and a line repeated under one event id is taken once, so a replay that
delivered the stream out of order, or twice, reduces to the same groups and the same
digest as a clean read. Quarantined lines are left out, exactly as every other derivation
leaves them out.

The daemon reduces the timeline and answers with its groups; a surface draws them and
never regroups.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel, ConfigDict

from eawf.kernel.runtime.compiled import canonical_digest
from eawf.kernel.runtime.events import RunEventKind, RunEventRecord
from eawf.kernel.state.epoch2.base import NonEmptyStr, StrictNonNegativeInt, StrictPositiveInt
from eawf.kernel.state.types import UtcDatetime

logger = logging.getLogger(__name__)

#: How long a coalesced group may span, first event to last, before it closes.
COALESCE_WINDOW_SECONDS: Final = 300


class EventPriority(StrEnum):
    """How much one event matters to a reader scanning the timeline."""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


_P0: Final = frozenset(
    {
        RunEventKind.CHILD_RUN_TERMINAL,
        RunEventKind.QUESTION_RAISED,
        RunEventKind.APPROVAL_REQUESTED,
        RunEventKind.APPROVAL_RESOLVED,
        RunEventKind.BUDGET_EXHAUSTED,
        RunEventKind.ERROR_OBSERVED,
        RunEventKind.CONTROL_REQUESTED,
        RunEventKind.CONTROL_ACKNOWLEDGED,
        RunEventKind.CONTROL_EFFECTED,
        RunEventKind.RECONCILIATION_COMPLETED,
        RunEventKind.PROVIDER_LOST,
        RunEventKind.EVENT_GAP,
        RunEventKind.CAPABILITY_REVOKED,
        RunEventKind.RUN_TRANSITION,
        RunEventKind.SESSION_ENDED,
        RunEventKind.FILE_CHANGED,
    }
)

_P2: Final = frozenset(
    {
        RunEventKind.HEARTBEAT,
        RunEventKind.USAGE_OBSERVED,
        RunEventKind.COMMAND_OUTPUT,
        RunEventKind.TOOL_ACCEPTED,
        RunEventKind.TURN_STARTED,
        RunEventKind.TURN_ENDED,
        RunEventKind.REASONING_STARTED,
    }
)

#: The priority of every event kind. Total over :class:`RunEventKind`, so a new kind is
#: a row here before it can reach a timeline.
EVENT_PRIORITIES: Final[Mapping[RunEventKind, EventPriority]] = MappingProxyType(
    {
        kind: EventPriority.P0
        if kind in _P0
        else EventPriority.P2
        if kind in _P2
        else EventPriority.P1
        for kind in RunEventKind
    }
)

#: The payload fields that name what an event is about, first match wins. Two events of
#: one kind about different targets never fold together.
_TARGET_FIELDS: Final = ("command_ref", "call_ref", "child_run_ref", "subject_ref")


class TimelineGroup(BaseModel):
    """One timeline row: a single event, or a coalesced run of low-priority ones.

    Attributes:
        event_kind: What happened.
        priority: How much it matters.
        target: What the events are about, when their payload names it.
        first_sequence: The first sequence the row covers.
        last_sequence: The last sequence it covers; equal to the first for one event.
        count: How many events the row holds.
        first_at: When the daemon recorded the first of them.
        last_at: When it recorded the last.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_kind: RunEventKind
    priority: EventPriority
    target: NonEmptyStr | None = None
    first_sequence: StrictPositiveInt
    last_sequence: StrictPositiveInt
    count: StrictPositiveInt
    first_at: UtcDatetime
    last_at: UtcDatetime

    @property
    def coalesced(self) -> bool:
        """Return whether the row folds more than one event."""
        return self.count > 1

    @property
    def span_seconds(self) -> int:
        """Return how long the row's events spanned, first to last, in whole seconds."""
        return int((self.last_at - self.first_at).total_seconds())


class RunTimeline(BaseModel):
    """A Run's timeline as the daemon reduced it.

    Attributes:
        groups: The rows, in sequence order.
        event_count: How many events the rows hold between them.
        p0_events: How many ``P0`` events the stream held.
        p0_coalesced: How many ``P0`` events were folded into a row with another event.
            Zero is the contract; the count is carried so a reader checks it rather than
            trusting it.
        digest: The digest of the groups, which a replay and a clean read agree on.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    groups: tuple[TimelineGroup, ...]
    event_count: StrictNonNegativeInt
    p0_events: StrictNonNegativeInt
    p0_coalesced: StrictNonNegativeInt
    digest: NonEmptyStr


def _target(event: RunEventRecord) -> str | None:
    """Return what one event is about, when its payload names it."""
    for name in _TARGET_FIELDS:
        value = getattr(event.payload, name, None)
        if value is not None:
            return str(value)
    return None


def _joins(group: TimelineGroup, event: RunEventRecord, target: str | None) -> bool:
    """Return whether ``event`` extends ``group`` rather than opening a row of its own."""
    return (
        group.priority is EventPriority.P2
        and group.event_kind is event.event_kind
        and group.target == target
        and event.run_sequence == group.last_sequence + 1
        and (event.recorded_at - group.first_at).total_seconds() <= COALESCE_WINDOW_SECONDS
    )


def reduce_timeline(events: Sequence[RunEventRecord]) -> RunTimeline:
    """Return a Run's timeline from its event lines, whatever order they arrived in.

    Args:
        events: The Run's event lines. Quarantined lines are left out, and a line whose
            event id was already taken is taken once.

    Returns:
        The rows in sequence order, with the counters and digest a replay is checked by.
    """
    seen: set[str] = set()
    live: list[RunEventRecord] = []
    for event in sorted(events, key=lambda item: (item.run_sequence, item.event_ref)):
        if event.quarantine is not None or event.event_ref in seen:
            continue
        seen.add(event.event_ref)
        live.append(event)
    groups: list[TimelineGroup] = []
    for event in live:
        target = _target(event)
        last = groups[-1] if groups else None
        if last is not None and _joins(last, event, target):
            groups[-1] = last.model_copy(
                update={
                    "last_sequence": event.run_sequence,
                    "count": last.count + 1,
                    "last_at": event.recorded_at,
                }
            )
            continue
        groups.append(
            TimelineGroup(
                event_kind=event.event_kind,
                priority=EVENT_PRIORITIES[event.event_kind],
                target=target,
                first_sequence=event.run_sequence,
                last_sequence=event.run_sequence,
                count=1,
                first_at=event.recorded_at,
                last_at=event.recorded_at,
            )
        )
    p0_events = sum(1 for event in live if EVENT_PRIORITIES[event.event_kind] is EventPriority.P0)
    p0_coalesced = sum(
        group.count for group in groups if group.priority is EventPriority.P0 and group.coalesced
    )
    digest = canonical_digest([group.model_dump(mode="json") for group in groups])
    logger.debug(f"reduce_timeline events={len(live)} groups={len(groups)}")
    return RunTimeline(
        groups=tuple(groups),
        event_count=len(live),
        p0_events=p0_events,
        p0_coalesced=p0_coalesced,
        digest=digest,
    )


__all__ = [
    "COALESCE_WINDOW_SECONDS",
    "EVENT_PRIORITIES",
    "EventPriority",
    "RunTimeline",
    "TimelineGroup",
    "reduce_timeline",
]
