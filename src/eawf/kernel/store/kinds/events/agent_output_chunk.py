"""AgentOutputChunkPayload — live, persisted streaming-output chunk payload.

Emitted by the daemon's live-spawn dispatch path AS the spawned child's
stdout arrives (not only once the spawn completes). Where the terminal
``agent.output`` event fans the completed spawn's whole answer tail in one
row, this chunk event persists the output incrementally so the TUI Watch
mode renders the agent's words live AND the per-chunk history survives the
TUI being closed (the event store is durable). The dispatch path batches
incoming stdout lines and appends one chunk event per batch, keyed on the
wave's ``scope_id`` so the Watch filter routes it with no extra plumbing.

The ``seq`` field is a per-spawn monotonic counter so the chunk order is
reconstructible from the persisted rows even if the bus delivers them out
of order; ``lines`` carries the batched text newline-joined, mirroring how
the terminal ``agent.output`` event packs its line tail so the Watch render
path is reused.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field

from eawf.kernel.store.kinds.events.base import TracedEventPayload


class AgentOutputChunkPayload(TracedEventPayload):
    """Payload for an ``agent.output.chunk`` event.

    Attributes:
        event_type: Discriminator tag; always ``"agent.output.chunk"``.
        timestamp: When the chunk batch was flushed (during the spawn).
        wave_id: ``W<NN>`` wave the spawned session scopes to; the Watch tail
            filters on this so a multi-lane fleet routes each chunk to its own
            session.
        session_id: Runtime session id of the spawn the chunk came from, or
            ``None`` when the spawn produced none.
        seq: Per-spawn monotonic chunk index (0-based), so the chunk order is
            reconstructible from the persisted rows.
        lines: The batched output text, newline-joined (mirrors the terminal
            ``agent.output`` event's ``lines`` packing so the Watch render path
            is reused).
    """

    model_config = ConfigDict(extra="forbid")

    event_type: Literal["agent.output.chunk"] = "agent.output.chunk"
    timestamp: datetime
    wave_id: str
    session_id: str | None = None
    seq: int = Field(ge=0)
    lines: str
