"""The per-model rate table of a history analysis, and the one cost-ordered ranking.

Rates come from the embedded pricing snapshot in
:mod:`eawf.observability.telemetry.pricing`, the same table the session-history
sweep priced every request with, so a model is never priced on one side and
unknown on the other.

The Codex rows of that snapshot are placeholders copied from Anthropic rates,
not OpenAI's published prices. A Codex row therefore shows its tokens and no
rate and no dollar figure, and no Codex model enters a cost-ordered ranking:
a placeholder price can rank nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from eawf.observability.telemetry.models import ObservedRequest, RuntimeName
from eawf.observability.telemetry.pricing import ModelPricing, lookup_pricing

logger = logging.getLogger(__name__)

#: The model label a request that recorded no model is grouped under.
UNKNOWN_MODEL: Final = "unknown"

_PER_MILLION: Final = Decimal(1_000_000)

#: Model names that are not models: no provider call stands behind them.
_NON_MODELS: Final[dict[str, str]] = {
    UNKNOWN_MODEL: "The harness did not record which model ran",
    "<synthetic>": "Synthetic record with no real provider call",
}

_CODEX_REASON: Final = (
    "Codex rates in the embedded table are placeholders, not published OpenAI rates; tokens only"
)
_NO_RATE_REASON: Final = "No published rate for this model"


class PricingQuality(StrEnum):
    """How far a dollar figure for one model can be trusted.

    Values:
        VERIFIED: A published rate row prices the model.
        PLACEHOLDER: A stand-in rate exists but backs no dollar claim.
        UNAVAILABLE: No rate resolves for the model.
    """

    VERIFIED = "verified"
    PLACEHOLDER = "placeholder"
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
        rates: The published rates, or ``None`` unless the quality is verified.
        cost_usd: The summed cost of its requests, or ``None`` unless verified.
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


def pricing_quality(runtime: RuntimeName, model: str | None) -> PricingQuality:
    """Return how far a dollar figure for *model* on *runtime* can be trusted.

    Args:
        runtime: The runtime the model ran under.
        model: The model id, or ``None`` when none was recorded.

    Returns:
        ``placeholder`` for any Codex model, ``unavailable`` when no rate row
        resolves, ``verified`` otherwise.
    """
    if runtime == "codex":
        return PricingQuality.PLACEHOLDER
    if model is None or model in _NON_MODELS or lookup_pricing(model) is None:
        return PricingQuality.UNAVAILABLE
    return PricingQuality.VERIFIED


def _unpriced_reason(runtime_set: Iterable[RuntimeName], model: str) -> str | None:
    """Return why a row carries no rate, or ``None`` when it is priced."""
    if "codex" in runtime_set:
        return _CODEX_REASON
    if model in _NON_MODELS:
        return _NON_MODELS[model]
    if lookup_pricing(model) is None:
        return _NO_RATE_REASON
    return None


def _per_million(row: ModelPricing) -> ModelRates:
    return ModelRates(
        input=row.input_per_token * _PER_MILLION,
        output=row.output_per_token * _PER_MILLION,
        cache_write_5m=row.cache_write_5m_per_token * _PER_MILLION,
        cache_write_1h=row.cache_write_1h_per_token * _PER_MILLION,
        cache_read=row.cache_read_per_token * _PER_MILLION,
    )


def rate_table(requests: Sequence[ObservedRequest]) -> tuple[RateRow, ...]:
    """Return one rate row per model the requests billed against.

    A model run under Codex is a placeholder row even when the same id also
    ran elsewhere, so a Codex request never lends a row a dollar figure.

    Args:
        requests: Every request of the analysed sessions.

    Returns:
        The rows, most tokens first, then by model id.
    """
    grouped: dict[str, list[ObservedRequest]] = {}
    for request in requests:
        grouped.setdefault(request.model or UNKNOWN_MODEL, []).append(request)
    rows: list[RateRow] = []
    for model, members in grouped.items():
        runtimes = tuple(sorted({row.runtime for row in members}))
        reason = _unpriced_reason(runtimes, model)
        quality = (
            PricingQuality.VERIFIED
            if reason is None
            else PricingQuality.PLACEHOLDER
            if "codex" in runtimes
            else PricingQuality.UNAVAILABLE
        )
        pricing = lookup_pricing(model) if reason is None else None
        costs = [row.cost_usd for row in members if row.cost_usd is not None]
        rows.append(
            RateRow(
                model=model,
                runtimes=runtimes,
                sessions=len({row.vendor_session_ref for row in members}),
                requests=len(members),
                input_tokens=sum(row.input_tokens for row in members),
                output_tokens=sum(row.output_tokens for row in members),
                cache_write_tokens=sum(row.cache_write_tokens for row in members),
                cache_read_tokens=sum(row.cache_read_tokens for row in members),
                total_tokens=sum(row.total_tokens for row in members),
                quality=quality,
                rates=None if pricing is None else _per_million(pricing),
                cost_usd=sum(costs, Decimal(0)) if pricing is not None and costs else None,
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
        Only verified rows that carry a cost; a Codex or unpriced row is left out.
    """
    priced = [
        row for row in rows if row.quality is PricingQuality.VERIFIED and row.cost_usd is not None
    ]
    return tuple(sorted(priced, key=lambda row: (-(row.cost_usd or Decimal(0)), row.model)))


__all__ = [
    "UNKNOWN_MODEL",
    "ModelRates",
    "PricingQuality",
    "RateRow",
    "cost_ranking",
    "pricing_quality",
    "rate_table",
]
