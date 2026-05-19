"""SubscriptionLagPayload — backpressure-signal envelope for the event bus.

Emitted *inline on a subscriber's own queue* when the subscriber falls
behind the producer and the per-subscriber sliding window
(``maxlen=1024``) drops the oldest envelope to make room.

Subscribers reconnect with ``since_event_id=last_event_id`` to backfill
the gap from the persistent ``event.jsonl``. Per C02 §5.7 (drop-oldest
sliding window, revised 2026-05-18 per audit C02.F50): producer never
blocks; ordering preserved in the persistent log; only the live stream
is lossy under sustained backpressure.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SubscriptionLagPayload(BaseModel):
    """Payload for :class:`StoreKind.SUBSCRIPTION_LAG` envelopes.

    Attributes:
        dropped_count: Number of envelopes evicted from the subscriber
            queue since the last lag notice; resets each time a lag
            notice is delivered.
        last_event_id: ``Envelope.id`` of the most-recently dropped
            envelope. The subscriber passes this back as
            ``since_event_id`` on reconnect to seek into ``event.jsonl``
            at the correct point.
    """

    model_config = ConfigDict(extra="forbid")

    dropped_count: int = Field(ge=1)
    last_event_id: str = Field(min_length=1)
