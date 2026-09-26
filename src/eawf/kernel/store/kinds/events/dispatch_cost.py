"""DispatchCostPayload — post-dispatch token + cost accounting payload.

Emitted by the daemon's cost-projection step after a dispatch attempt
completes. Carries the per-invocation token tallies (input / output /
cache-creation / cache-read) and the priced ``cost_usd`` so the
telemetry projector can roll costs up per wave, runtime, and model. The
``pricing_version`` pins which ``PRICING`` snapshot computed the figure.

Rows written before the five-class split carry none of ``reasoning_tokens``,
``total_tokens``, ``price_source`` or ``rate_table_version``; they stay
readable because the event store is append-only. A row that does carry a
``total_tokens`` or a ``price_source`` is held to the same identity and
provenance checks as the projected telemetry row.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal, Self

from pydantic import ConfigDict, model_validator

from eawf.kernel.store.kinds.events.base import RuntimeTriple, TracedEventPayload
from eawf.observability.telemetry.models import (
    PriceSourceKind,
    check_price_source,
    check_token_identity,
    null_unpriced_zero_cost,
)


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
        reasoning_tokens: Reasoning slice of ``output_tokens``, or ``None``
            when the runtime reports no reasoning counter.
        total_tokens: Input + output + cache-read + cache-write; ``None``
            only on a row written before the split.
        cost_usd: Priced cost in USD (``Decimal`` for exact accounting), or
            ``None`` when the row is unpriced.
        price_source: Provenance of ``cost_usd``; ``None`` only on a row
            written before the split.
        rate_table_version: Rate-table revision a list-reconstructed cost
            was computed from.
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
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: Decimal | None
    price_source: PriceSourceKind | None = None
    rate_table_version: str | None = None
    pricing_version: str

    @model_validator(mode="before")
    @classmethod
    def _legacy_unpriced_zero(cls, data: object) -> object:
        """Read an older unpriced row's zero cost as null."""
        return null_unpriced_zero_cost(data)

    @model_validator(mode="after")
    def _usage_reconciles(self) -> Self:
        """Hold a split row to the token identity and price-source contract.

        Raises:
            ValueError: When ``total_tokens`` is set and the classes do not
                sum to it, when ``price_source`` is set and inconsistent
                with the cost, or when only one of the two is set (a
                half-split row is neither legacy nor current).
        """
        if (self.total_tokens is None) != (self.price_source is None):
            raise ValueError("total_tokens and price_source are written together or not at all")
        if self.price_source is None and self.cost_usd is None:
            raise ValueError("a row written before the split must record its cost_usd")
        if self.total_tokens is not None:
            check_token_identity(
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                cache_read_tokens=self.cache_read_input_tokens,
                cache_write_tokens=self.cache_creation_input_tokens,
                reasoning_tokens=self.reasoning_tokens,
                total_tokens=self.total_tokens,
            )
            check_price_source(
                cost_usd=self.cost_usd,
                price_source=self.price_source,
                rate_table_version=self.rate_table_version,
            )
        return self
