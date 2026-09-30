"""The reads a route owes beside its projection, and keeps current while it is on screen.

A route projection is patched by the feed, so a console that holds one is current without
asking again. Some of what a frame draws is not a projection row: a Run's event lines are
ledger lines appended with no canonical move, so nothing is published when one lands and
no patch ever carries it. Such a read is declared here once, as a :class:`LiveRead`: the
route that draws it, how the seam finds what it is about, and how it is fetched. The seam
owes every declared read of the route on screen as soon as it can be addressed, and the
console reads it again while the route stays on screen, so what is appended lands without
a relaunch. A new read is one more entry in :data:`LIVE_READS`; neither the seam nor the
console names any read itself.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Protocol

from eawf.kernel.projection.compute import RouteProjection
from eawf.kernel.projection.transcript import TRANSCRIPT_ROUTE
from eawf.kernel.runtime.events import ChildRunPayload, RunEventRecord
from eawf.runtime.daemon.methods.run import RUN_EVENTS_READ_METHOD, RunEventsAnswer

logger = logging.getLogger(__name__)


class LiveReadHost(Protocol):
    """What a live read needs of the seam: where the console is, and a way to ask."""

    @property
    def route(self) -> str:
        """Return the console route key on screen."""
        ...

    @property
    def subject(self) -> str | None:
        """Return the key of the record the route on screen is about, if any."""
        ...

    def projection_for(self, route: str) -> RouteProjection | None:
        """Return the projection held for *route*, if any."""
        ...

    async def call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Send one read to the daemon for the seam's tree and return its answer."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class LiveRead:
    """One read a route owes beside its projection.

    Attributes:
        route: The console route key that draws what the read returns.
        address: What the read is about right now -- a URN, a key -- or ``None`` while
            it cannot be addressed yet, such as before the route's rows arrive. A held
            answer is shown only while its address is still the current one.
        fetch: Reads the answer for an address.
    """

    route: str
    address: Callable[[LiveReadHost], str | None]
    fetch: Callable[[LiveReadHost, str], Awaitable[Any]]


@dataclass(frozen=True, slots=True, kw_only=True)
class HeldTranscript:
    """The event lines one Run's transcript is drawn from, as last read.

    Attributes:
        events: The Run's own lines, in sequence order, of every kind the daemon holds.
        children: The lines of each child Run the Run delegated to, by child URN. A
            child whose read failed is absent, and the transcript draws it unreadable.
    """

    events: tuple[RunEventRecord, ...] = ()
    children: Mapping[str, tuple[RunEventRecord, ...]] = field(default_factory=dict)


def _transcript_address(host: LiveReadHost) -> str | None:
    """Return the URN of the Run the transcript is about, once its row is held."""
    held = host.projection_for(TRANSCRIPT_ROUTE)
    subject = host.subject
    if host.route != TRANSCRIPT_ROUTE or held is None or not subject:
        return None
    return next((row.urn for row in held.rows if row.key == subject), None)


async def _run_events(host: LiveReadHost, urn: str) -> tuple[RunEventRecord, ...]:
    """Read one Run's derived stream, in sequence order."""
    answer = RunEventsAnswer.model_validate(await host.call(RUN_EVENTS_READ_METHOD, {"urn": urn}))
    return tuple(RunEventRecord.model_validate(line) for line in answer.events)


async def _fetch_transcript(host: LiveReadHost, urn: str) -> HeldTranscript:
    """Read a Run's lines and the lines of each child Run its delegations name.

    The lines are taken whatever their kind, so a producer that starts appending a new
    kind is drawn with no change here. A child whose read fails is left out rather than
    failing the parent's read.
    """
    events = await _run_events(host, urn)
    children: dict[str, tuple[RunEventRecord, ...]] = {}
    for child in dict.fromkeys(
        str(line.payload.child_run_ref)
        for line in events
        if isinstance(line.payload, ChildRunPayload) and line.payload.child_run_ref
    ):
        try:
            children[child] = await _run_events(host, child)
        except Exception as exc:
            logger.warning(f"transcript child unreadable cause={exc!r}")
    logger.debug(f"fetch_transcript events={len(events)} children={len(children)}")
    return HeldTranscript(events=events, children=children)


#: The read the transcript route draws its blocks from.
TRANSCRIPT_READ: Final = RUN_EVENTS_READ_METHOD

#: Every live read, by the name the seam owes it under.
LIVE_READS: Final[Mapping[str, LiveRead]] = MappingProxyType(
    {
        TRANSCRIPT_READ: LiveRead(
            route=TRANSCRIPT_ROUTE, address=_transcript_address, fetch=_fetch_transcript
        ),
    }
)


__all__ = [
    "LIVE_READS",
    "TRANSCRIPT_READ",
    "HeldTranscript",
    "LiveRead",
    "LiveReadHost",
]
