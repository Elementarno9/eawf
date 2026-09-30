"""Whether a running Run is still producing, as a truth field that carries its freshness.

"The number stopped moving" has two causes with opposite meanings: the reader lost its
source, or the work stopped. This module keeps them apart. A Run the daemon's stall sweep
raised a standing stall for is *stalled* -- running and not advancing -- and the value says
so with the fact's own instant. The read that says so is itself aged: once it is older than
:data:`STALE_AFTER_SECONDS` its freshness is ``stale``, whatever it last said, because a
console that stopped hearing about the Run knows nothing about whether it moved since.

The input is the answer of the daemon's stall read -- the standing facts and the instant
it was taken -- which the caller holds; nothing here reads a ledger.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from eawf.kernel.projection.truth import Freshness, Precision, TruthField, TruthKind, TruthState
from eawf.kernel.runtime.stall import RunStallFact
from eawf.kernel.state.enums import MeasurementQuality

#: How old a liveness read may be before what it says is stale rather than current. The
#: console re-reads it every few seconds while it is on screen, so a read this old means
#: the re-reads stopped arriving.
STALE_AFTER_SECONDS: Final = 60

#: Who states a Run's liveness.
LIVENESS_PRODUCER: Final = "the daemon stall sweep"

#: A Run with a standing stall: running, and not advancing.
STALLED: Final = "stalled"

#: A running Run with no standing stall: it produced something inside its interval.
PRODUCING: Final = "producing"

#: Why liveness is unknown before the stall read arrives.
UNREAD_REASON: Final = "the stall read has not arrived"


@dataclass(frozen=True, slots=True, kw_only=True)
class HeldLiveness:
    """The stall read a surface holds.

    Attributes:
        stalls: Every stall standing when the read was answered.
        read_at: When the daemon answered it.
    """

    stalls: Sequence[RunStallFact]
    read_at: datetime

    def stalled_keys(self) -> frozenset[str]:
        """Return the keys of the Runs a stall stands over."""
        return frozenset(fact.run_ref.entity_key for fact in self.stalls)

    def stall_of(self, run_key: str) -> RunStallFact | None:
        """Return the stall standing over Run ``run_key``, or ``None``."""
        return next((fact for fact in self.stalls if fact.run_ref.entity_key == run_key), None)


def freshness_of(read_at: datetime, now: datetime) -> Freshness:
    """Return how current a read taken at ``read_at`` is at ``now``."""
    age = (now - read_at).total_seconds()
    if age > STALE_AFTER_SECONDS:
        return Freshness.STALE
    return Freshness.AGING if age > STALE_AFTER_SECONDS / 2 else Freshness.LIVE


def run_liveness(run_key: str, held: HeldLiveness | None, *, now: datetime) -> TruthField[str]:
    """Return whether Run ``run_key`` is producing or stalled, with the read's freshness.

    Args:
        run_key: The running Run asked about.
        held: The stall read the caller holds, or ``None`` before it arrives.
        now: The instant the answer is drawn at, which the read is aged against.

    Returns:
        ``stalled`` with the instant its silence began when a stall stands over the Run, else
        ``producing``; in either case ``stale`` freshness once the read is older than
        :data:`STALE_AFTER_SECONDS`. Unknown, naming why, before the read arrives.
    """
    if held is None:
        return TruthField[str](
            value=None,
            state=TruthState.UNKNOWN,
            truth_kind=TruthKind.OBSERVED,
            producer=LIVENESS_PRODUCER,
            producer_revision=1,
            precision=Precision.UNAVAILABLE,
            measurement_quality=MeasurementQuality.UNAVAILABLE,
            freshness=Freshness.STALE,
            provenance_refs=(),
            missing_reason=UNREAD_REASON,
        )
    fact = held.stall_of(run_key)
    return TruthField[str](
        value=STALLED if fact is not None else PRODUCING,
        state=TruthState.KNOWN,
        truth_kind=TruthKind.OBSERVED,
        producer=LIVENESS_PRODUCER,
        producer_revision=max(1, int(held.read_at.timestamp())),
        occurred_at=fact.last_activity_at if fact is not None else None,
        received_at=held.read_at,
        precision=Precision.EXACT,
        measurement_quality=MeasurementQuality.EXACT,
        freshness=freshness_of(held.read_at, now),
        provenance_refs=(str(fact.run_ref) if fact is not None else run_key,),
    )


__all__ = [
    "LIVENESS_PRODUCER",
    "PRODUCING",
    "STALE_AFTER_SECONDS",
    "STALLED",
    "UNREAD_REASON",
    "HeldLiveness",
    "freshness_of",
    "run_liveness",
]
