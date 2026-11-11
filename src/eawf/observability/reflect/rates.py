"""The per-model rate table of a history analysis, and the one cost-ordered ranking.

Rates come from the price files in :mod:`eawf.observability.telemetry.pricing`,
the same book the session-history sweep priced every request with, so a model
is never priced on one side and unknown on the other. A model, Codex or not,
carries a rate and a dollar figure only when a row in those files prices it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.config.schema import PriceRates
from eawf.observability.telemetry.models import ObservedRequest, RuntimeName
from eawf.observability.telemetry.pricing import active_price_book

logger = logging.getLogger(__name__)

#: The model label a request that recorded no model is grouped under.
UNKNOWN_MODEL: Final = "unknown"

#: Model names that are not models: no provider call stands behind them.
_NON_MODELS: Final[dict[str, str]] = {
    UNKNOWN_MODEL: "The harness did not record which model ran",
    "<synthetic>": "Synthetic record with no real provider call",
}

#: The model labels that name no model, so no page lists them as unpriced.
NON_MODELS: Final = frozenset(_NON_MODELS)

_NO_RATE_REASON: Final = "No published rate for this model"


class PricingQuality(StrEnum):
    """How far a dollar figure for one model can be trusted.

    Values:
        VERIFIED: A price-file row or an operator override prices the model.
        UNAVAILABLE: No rate resolves for the model.
    """

    VERIFIED = "verified"
    UNAVAILABLE = "unavailable"


class ModelRates(BaseModel):
    """USD per million tokens for one model, per token class.

    Attributes:
        input: Non-cached input.
        output: Output, reasoning included.
        cache_write_5m: Prompt-cache writes at the 5-minute rate.
        cache_write_1h: Prompt-cache writes at the 1-hour rate.
        cache_read: Prompt-cache reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    input: Decimal
    output: Decimal
    cache_write_5m: Decimal
    cache_write_1h: Decimal
    cache_read: Decimal


class RateRow(BaseModel):
    """One model's sessions, tokens, rates and attributed cost.

    Attributes:
        model: The model id, or ``unknown``.
        runtimes: The runtimes that ran it, sorted.
        sessions: Sessions with at least one request on the model.
        requests: Requests billed against it.
        input_tokens: Non-cached input tokens.
        output_tokens: Output tokens.
        cache_write_tokens: Prompt-cache writes at either rate.
        cache_read_tokens: Prompt-cache reads.
        total_tokens: Every token class summed.
        quality: How far a dollar figure for the model can be trusted.
        rates: The standard rates when they state every Anthropic class,
            else ``None``.
        cost_usd: The summed cost of its priced requests, or ``None`` unless verified.
        unpriced_reason: Why the row carries no rate, or ``None`` when it does.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    runtimes: tuple[RuntimeName, ...]
    sessions: int = Field(ge=0)
    requests: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    quality: PricingQuality
    rates: ModelRates | None
    cost_usd: Decimal | None
    unpriced_reason: str | None


def pricing_quality(model: str | None) -> PricingQuality:
    """Return how far a dollar figure for *model* can be trusted.

    Args:
        model: The model id, or ``None`` when none was recorded.

    Returns:
        ``verified`` when a row or override states a rate for the model
        today, ``unavailable`` otherwise.
    """
    if not model or model in _NON_MODELS:
        return PricingQuality.UNAVAILABLE
    if active_price_book().list_rates(model, on=datetime.now(tz=UTC).date()) is None:
        return PricingQuality.UNAVAILABLE
    return PricingQuality.VERIFIED


def _model_rates(rates: PriceRates | None) -> ModelRates | None:
    """Return the five Anthropic-class rates, or ``None`` when any is not stated."""
    if rates is None:
        return None
    input_rate, output, cache_read = rates.input, rates.output, rates.cache_read
    write_5m, write_1h = rates.cache_write.get("5m"), rates.cache_write.get("1h")
    if input_rate is None or output is None or cache_read is None:
        return None
    if write_5m is None or write_1h is None:
        return None
    return ModelRates(
        input=input_rate,
        output=output,
        cache_write_5m=write_5m,
        cache_write_1h=write_1h,
        cache_read=cache_read,
    )


def rate_table(requests: Sequence[ObservedRequest]) -> tuple[RateRow, ...]:
    """Return one rate row per model the requests billed against.

    Args:
        requests: Every request of the analysed sessions.

    Returns:
        The rows, most tokens first, then by model id.
    """
    book = active_price_book()
    today = datetime.now(tz=UTC).date()
    grouped: dict[str, list[ObservedRequest]] = {}
    for request in requests:
        grouped.setdefault(request.model or UNKNOWN_MODEL, []).append(request)
    rows: list[RateRow] = []
    for model, members in grouped.items():
        listed = None if model in _NON_MODELS else book.list_rates(model, on=today)
        reason = _NON_MODELS.get(model) or (_NO_RATE_REASON if listed is None else None)
        costs = [row.cost_usd for row in members if row.cost_usd is not None]
        rows.append(
            RateRow(
                model=model,
                runtimes=tuple(sorted({row.runtime for row in members})),
                sessions=len({row.vendor_session_ref for row in members}),
                requests=len(members),
                input_tokens=sum(row.input_tokens for row in members),
                output_tokens=sum(row.output_tokens for row in members),
                cache_write_tokens=sum(row.cache_write_tokens for row in members),
                cache_read_tokens=sum(row.cache_read_tokens for row in members),
                total_tokens=sum(row.total_tokens for row in members),
                quality=PricingQuality.VERIFIED if reason is None else PricingQuality.UNAVAILABLE,
                rates=_model_rates(listed),
                cost_usd=sum(costs, Decimal(0)) if reason is None and costs else None,
                unpriced_reason=reason,
            )
        )
    rows.sort(key=lambda row: (-row.total_tokens, row.model))
    logger.debug(f"rate_table requests={len(requests)} models={len(rows)}")
    return tuple(rows)


def cost_ranking(rows: Sequence[RateRow]) -> tuple[RateRow, ...]:
    """Return the rows a dollar figure can rank, most expensive first.

    Args:
        rows: Rate rows from :func:`rate_table`.

    Returns:
        Only verified rows that carry a cost; an unpriced row is left out.
    """
    priced = [
        row for row in rows if row.quality is PricingQuality.VERIFIED and row.cost_usd is not None
    ]
    return tuple(sorted(priced, key=lambda row: (-(row.cost_usd or Decimal(0)), row.model)))


__all__ = [
    "NON_MODELS",
    "UNKNOWN_MODEL",
    "ModelRates",
    "PricingQuality",
    "RateRow",
    "cost_ranking",
    "pricing_quality",
    "rate_table",
]
