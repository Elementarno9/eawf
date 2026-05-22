"""DispatchCostPayload — post-dispatch token + cost accounting payload.

Emitted by the daemon's cost-projection step after a dispatch attempt
completes. Carries the per-invocation token tallies (input / output /
cache-creation / cache-read) and the priced ``cost_usd`` so the
telemetry projector can roll costs up per wave, runtime, and model. The
``pricing_version`` pins which ``PRICING`` snapshot computed the figure.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import ConfigDict

from eawf.store.kinds.events.base import RuntimeTriple, TracedEventPayload


class DispatchCostPayload(TracedEventPayload):
    """Payload for a ``dispatch_cost`` event.

    Attributes:
        event_type: Discriminator tag; always ``"dispatch_cost"``.
        timestamp: When the cost was projected (post-dispatch).
        wave_id: ``W<NN>`` wave the dispatch served, or ``None`` for an
            interactive (non-wave) CLI session.
        attempt_id: Dispatch-attempt id, or ``None`` for an interactive
            session with no attempt envelope.
        runtime: Runtime that incurred the cost.
        model: Model identifier the cost is priced against.
        input_tokens: Non-cached input tokens billed.
        output_tokens: Output tokens billed.
        cache_creation_input_tokens: Tokens written to the prompt cache.
        cache_read_input_tokens: Tokens served from the prompt cache.
        cost_usd: Priced cost in USD (``Decimal`` for exact accounting).
        pricing_version: ``PRICING`` snapshot version used to compute
            ``cost_usd``.
    """

    model_config = ConfigDict(extra="forbid")

    event_type: Literal["dispatch_cost"] = "dispatch_cost"
    timestamp: datetime
    wave_id: str | None
    attempt_id: str | None
    runtime: RuntimeTriple
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    cost_usd: Decimal
    pricing_version: str
