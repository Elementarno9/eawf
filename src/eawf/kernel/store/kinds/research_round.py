"""ResearchRoundPayload — payload model for StoreKind.RESEARCH_ROUND records.

A research-round store record persists one executed round of a bounded
campaign run: the per-domain findings the round's spawned researchers
returned, the Claim row ids the round-end reconcile wrote, the saturation
verdict that ended the round, and whether the round coincided with an
operator-review checkpoint. The append-only round store lets the board RUN /
ROUND bands + the snapshot RPC read the real run state off the store rather
than a synthetic node.

The canonical home for the payload is this kernel kind module so the
:data:`~eawf.kernel.store.kinds.PAYLOAD_MODELS` registry can map the kind
without a circular import on the daemon method layer; the daemon
``research`` method module re-exports it for back-compat.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ResearchRoundPayload(BaseModel):
    """Payload for one persisted research-campaign round record.

    Appended to the append-only ``research_round`` store once per executed
    round by :func:`~eawf.runtime.daemon.methods.research.run_campaign`. Carries
    the round's per-domain findings, the saturation verdict that ended the
    round, and whether the round coincided with an operator-review checkpoint --
    enough for the board RUN / ROUND bands + the snapshot RPC to read the real
    run state off the store rather than a synthetic node.

    Attributes:
        campaign_id: The campaign the round belongs to.
        round_number: The 1-based round index.
        domains: The domains spawned this round, in dispatch order.
        finding_lines: Every findings line the round's researchers returned.
        claim_ids: The Claim row ids the round-end reconcile wrote.
        saturated: Whether the round's saturation reducer declared the
            campaign dry.
        steer_notes: The operator steer / override notes read off the channel
            when this round's findings were reconciled. Empty when the operator
            pushed nothing. The notes are recorded and never reach a running
            round: every researcher is spawned before the channel is read, so a
            note queued mid-round lands on this record without altering the
            dispatch set of any round -- it is an audit trail of the operator's
            asks, not a live steering input.
        checkpoint: Whether the round coincided with an operator-review
            checkpoint (per the run's checkpoint policy).
        recorded_at: When the round record was persisted (UTC).
    """

    model_config = ConfigDict(extra="forbid")

    campaign_id: str = Field(min_length=1)
    round_number: int = Field(ge=1)
    domains: list[str] = Field(default_factory=list)
    finding_lines: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    saturated: bool = False
    steer_notes: list[str] = Field(default_factory=list)
    checkpoint: bool = False
    recorded_at: datetime
