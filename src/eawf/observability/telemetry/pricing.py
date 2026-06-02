"""Embedded Decimal pricing snapshot + currency-drift detection.

Canonical Anthropic published rates fetched 2026-05-17 12:00 UTC from
``https://platform.claude.com/docs/en/about-claude/pricing``. This module
is the cost-ledger source of truth committed at C09 vendor time; every
cost metric (M02 ``eawf_cost_usd_total``, M08-M10 burn-rate gauges) prices
tokens through :data:`PRICING`.

All rates are :class:`~decimal.Decimal` in USD **per token** (not per
million) so long-horizon ledger sums never accumulate float drift. Cache
multipliers are encoded explicitly per model rather than derived, so a
rounding change to one rate cannot silently shift the others.

:func:`lookup_pricing` resolves a model id by exact match first, then by
longest-prefix fallback (e.g. ``claude-opus-4-7-20260514`` →
``claude-opus-4-7``).

:func:`check_pricing_currency` validates the embedded snapshot's shape and
internal currency (the stated Anthropic cache multipliers) and returns a
typed :class:`PricingDriftReport`; the weekly CI gate
(``eawf telemetry pricing-currency-check``) consumes it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

__all__ = [
    "CACHE_READ_MULTIPLIER",
    "CACHE_WRITE_1H_MULTIPLIER",
    "CACHE_WRITE_5M_MULTIPLIER",
    "PRICING",
    "PRICING_FETCHED_AT",
    "PRICING_VERSION",
    "ModelPricing",
    "PricingDriftFinding",
    "PricingDriftReport",
    "check_pricing_currency",
    "lookup_pricing",
]


class ModelPricing(BaseModel):
    """Per-token USD prices for one model id.

    Covers the claude family at published rates plus the cross-vendor codex /
    opencode model ids the dispatch routing table emits: codex tier ids are
    placeholder rates (no OpenAI rate is embedded in this Anthropic-sourced
    snapshot), opencode ``provider/model`` ids price at the real anthropic
    rates (the OAuth-Claude opencode lane).

    All values in USD per token (NOT per million). Decimal-quantised to
    avoid float drift on long-horizon ledger sums.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_per_token: Decimal
    output_per_token: Decimal
    cache_read_per_token: Decimal
    cache_write_5m_per_token: Decimal
    cache_write_1h_per_token: Decimal
    pricing_version: str
    fetched_at: datetime


PRICING_VERSION = "2026.05.17"
PRICING_FETCHED_AT = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)

# Anthropic-stated cache multipliers on base input, encoded explicitly so the
# currency check can confirm the embedded snapshot is internally consistent.
CACHE_READ_MULTIPLIER = Decimal("0.1")
CACHE_WRITE_5M_MULTIPLIER = Decimal("1.25")
CACHE_WRITE_1H_MULTIPLIER = Decimal("2")


PRICING: dict[str, ModelPricing] = {
    # Opus 4.x — $5 / $25 per MTok (2026-05-17 rates).
    "claude-opus-4-8": ModelPricing(
        input_per_token=Decimal("5e-6"),
        output_per_token=Decimal("25e-6"),
        cache_read_per_token=Decimal("0.5e-6"),
        cache_write_5m_per_token=Decimal("6.25e-6"),
        cache_write_1h_per_token=Decimal("10e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    "claude-opus-4-7": ModelPricing(
        input_per_token=Decimal("5e-6"),
        output_per_token=Decimal("25e-6"),
        cache_read_per_token=Decimal("0.5e-6"),
        cache_write_5m_per_token=Decimal("6.25e-6"),
        cache_write_1h_per_token=Decimal("10e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    "claude-opus-4-6": ModelPricing(
        input_per_token=Decimal("5e-6"),
        output_per_token=Decimal("25e-6"),
        cache_read_per_token=Decimal("0.5e-6"),
        cache_write_5m_per_token=Decimal("6.25e-6"),
        cache_write_1h_per_token=Decimal("10e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    "claude-opus-4-5": ModelPricing(
        input_per_token=Decimal("5e-6"),
        output_per_token=Decimal("25e-6"),
        cache_read_per_token=Decimal("0.5e-6"),
        cache_write_5m_per_token=Decimal("6.25e-6"),
        cache_write_1h_per_token=Decimal("10e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    "claude-opus-4-1": ModelPricing(
        input_per_token=Decimal("15e-6"),
        output_per_token=Decimal("75e-6"),
        cache_read_per_token=Decimal("1.5e-6"),
        cache_write_5m_per_token=Decimal("18.75e-6"),
        cache_write_1h_per_token=Decimal("30e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    # Sonnet 4.x — $3 / $15 per MTok.
    "claude-sonnet-4-6": ModelPricing(
        input_per_token=Decimal("3e-6"),
        output_per_token=Decimal("15e-6"),
        cache_read_per_token=Decimal("0.3e-6"),
        cache_write_5m_per_token=Decimal("3.75e-6"),
        cache_write_1h_per_token=Decimal("6e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    "claude-sonnet-4-5": ModelPricing(
        input_per_token=Decimal("3e-6"),
        output_per_token=Decimal("15e-6"),
        cache_read_per_token=Decimal("0.3e-6"),
        cache_write_5m_per_token=Decimal("3.75e-6"),
        cache_write_1h_per_token=Decimal("6e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    # Haiku 4.5 — $1 / $5 per MTok (2026-05-17 rates).
    "claude-haiku-4-5-20251001": ModelPricing(
        input_per_token=Decimal("1e-6"),
        output_per_token=Decimal("5e-6"),
        cache_read_per_token=Decimal("0.1e-6"),
        cache_write_5m_per_token=Decimal("1.25e-6"),
        cache_write_1h_per_token=Decimal("2e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    "claude-haiku-4-5": ModelPricing(  # alias-only entry
        input_per_token=Decimal("1e-6"),
        output_per_token=Decimal("5e-6"),
        cache_read_per_token=Decimal("0.1e-6"),
        cache_write_5m_per_token=Decimal("1.25e-6"),
        cache_write_1h_per_token=Decimal("2e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    # Bare family aliases. The longest-prefix resolver only matches a key
    # that is a prefix of the queried id, so a bare token like "opus" (used
    # on the dispatch / role-spec surface and by short-form runtime logs)
    # cannot reach a dated "claude-opus-4-*" row — it must be its own key.
    # Each alias prices to the current 4.x family rate so a short id resolves
    # to a priced row instead of falling through unpriced. These keys never
    # shadow a dated row: the resolver prefers the longest matching prefix,
    # so "claude-opus-4-8-20260101" still binds to "claude-opus-4-8".
    "opus": ModelPricing(
        input_per_token=Decimal("5e-6"),
        output_per_token=Decimal("25e-6"),
        cache_read_per_token=Decimal("0.5e-6"),
        cache_write_5m_per_token=Decimal("6.25e-6"),
        cache_write_1h_per_token=Decimal("10e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    "claude-opus": ModelPricing(
        input_per_token=Decimal("5e-6"),
        output_per_token=Decimal("25e-6"),
        cache_read_per_token=Decimal("0.5e-6"),
        cache_write_5m_per_token=Decimal("6.25e-6"),
        cache_write_1h_per_token=Decimal("10e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    "sonnet": ModelPricing(
        input_per_token=Decimal("3e-6"),
        output_per_token=Decimal("15e-6"),
        cache_read_per_token=Decimal("0.3e-6"),
        cache_write_5m_per_token=Decimal("3.75e-6"),
        cache_write_1h_per_token=Decimal("6e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    "claude-sonnet": ModelPricing(
        input_per_token=Decimal("3e-6"),
        output_per_token=Decimal("15e-6"),
        cache_read_per_token=Decimal("0.3e-6"),
        cache_write_5m_per_token=Decimal("3.75e-6"),
        cache_write_1h_per_token=Decimal("6e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    "haiku": ModelPricing(
        input_per_token=Decimal("1e-6"),
        output_per_token=Decimal("5e-6"),
        cache_read_per_token=Decimal("0.1e-6"),
        cache_write_5m_per_token=Decimal("1.25e-6"),
        cache_write_1h_per_token=Decimal("2e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    "claude-haiku": ModelPricing(
        input_per_token=Decimal("1e-6"),
        output_per_token=Decimal("5e-6"),
        cache_read_per_token=Decimal("0.1e-6"),
        cache_write_5m_per_token=Decimal("1.25e-6"),
        cache_write_1h_per_token=Decimal("2e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    # Codex (OpenAI) — PLACEHOLDER RATE. No published OpenAI/codex rate is
    # embedded in this Anthropic-sourced snapshot; this row uses the Opus 4.x
    # input/output rate as the closest reasonable stand-in so codex sessions
    # price to a non-zero, currency-consistent figure instead of $0. The
    # exact codex rate needs operator confirmation before this is treated as
    # authoritative. Cache rates are the standard 0.1x / 1.25x / 2x of input
    # so check_pricing_currency stays green.
    "codex": ModelPricing(
        input_per_token=Decimal("5e-6"),
        output_per_token=Decimal("25e-6"),
        cache_read_per_token=Decimal("0.5e-6"),
        cache_write_5m_per_token=Decimal("6.25e-6"),
        cache_write_1h_per_token=Decimal("10e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    # Codex per-tier model ids the dispatch routing table emits for the codex
    # runtime (cheapest -> most capable). PLACEHOLDER RATES, same provenance as
    # the bare ``codex`` row above: no published OpenAI rate is embedded in this
    # Anthropic-sourced snapshot, so each tier prices to a non-zero,
    # currency-consistent stand-in (the matching claude tier's input/output
    # rate) so a codex juror spawn prices honestly (priced=True) instead of
    # silently $0. The exact OpenAI rates need operator confirmation before
    # these are treated as authoritative. A dated/suffixed variant (e.g.
    # ``gpt-5-codex-preview``) longest-prefix-matches the tier row.
    "gpt-5-mini": ModelPricing(
        input_per_token=Decimal("1e-6"),
        output_per_token=Decimal("5e-6"),
        cache_read_per_token=Decimal("0.1e-6"),
        cache_write_5m_per_token=Decimal("1.25e-6"),
        cache_write_1h_per_token=Decimal("2e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    # ``gpt-5-codex`` is listed before ``gpt-5`` only for readability; the
    # longest-prefix resolver always prefers ``gpt-5-codex`` for that id
    # regardless of dict order, and ``gpt-5`` never shadows it.
    "gpt-5-codex": ModelPricing(
        input_per_token=Decimal("5e-6"),
        output_per_token=Decimal("25e-6"),
        cache_read_per_token=Decimal("0.5e-6"),
        cache_write_5m_per_token=Decimal("6.25e-6"),
        cache_write_1h_per_token=Decimal("10e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    "gpt-5": ModelPricing(
        input_per_token=Decimal("3e-6"),
        output_per_token=Decimal("15e-6"),
        cache_read_per_token=Decimal("0.3e-6"),
        cache_write_5m_per_token=Decimal("3.75e-6"),
        cache_write_1h_per_token=Decimal("6e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    # OpenCode per-tier model ids the dispatch routing table emits for the
    # opencode runtime. OpenCode addresses models in ``provider/model`` form;
    # the routing table routes the anthropic provider (the OAuth-Claude
    # opencode lane), so these ids price at the REAL anthropic rates (not a
    # placeholder) -- an opencode-via-anthropic spawn bills the same as the
    # native claude lane. The ``provider/`` prefix keeps these keys disjoint
    # from the bare claude ids, and a dated suffix longest-prefix-matches the
    # tier row.
    "anthropic/claude-haiku-4-5": ModelPricing(
        input_per_token=Decimal("1e-6"),
        output_per_token=Decimal("5e-6"),
        cache_read_per_token=Decimal("0.1e-6"),
        cache_write_5m_per_token=Decimal("1.25e-6"),
        cache_write_1h_per_token=Decimal("2e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    "anthropic/claude-sonnet-4-6": ModelPricing(
        input_per_token=Decimal("3e-6"),
        output_per_token=Decimal("15e-6"),
        cache_read_per_token=Decimal("0.3e-6"),
        cache_write_5m_per_token=Decimal("3.75e-6"),
        cache_write_1h_per_token=Decimal("6e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
    "anthropic/claude-opus-4-8": ModelPricing(
        input_per_token=Decimal("5e-6"),
        output_per_token=Decimal("25e-6"),
        cache_read_per_token=Decimal("0.5e-6"),
        cache_write_5m_per_token=Decimal("6.25e-6"),
        cache_write_1h_per_token=Decimal("10e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    ),
}


def lookup_pricing(model: str) -> ModelPricing | None:
    """Look up pricing by exact model id, then longest-prefix fallback.

    Args:
        model: Model identifier, e.g. ``claude-opus-4-7`` or a dated
            variant like ``claude-opus-4-7-20260514``.

    Returns:
        The :class:`ModelPricing` row for an exact match; otherwise the row
        whose key is the longest prefix of *model*; otherwise ``None`` when
        no key matches.
    """
    if model in PRICING:
        return PRICING[model]
    matches = sorted(
        ((k, v) for k, v in PRICING.items() if model.startswith(k)),
        key=lambda kv: len(kv[0]),
        reverse=True,
    )
    return matches[0][1] if matches else None


class PricingDriftFinding(BaseModel):
    """One drift finding for a single model row.

    Attributes:
        model_id: The :data:`PRICING` key the finding pertains to.
        field: The :class:`ModelPricing` field that drifted.
        expected: The value the currency check derived as correct.
        actual: The value embedded in :data:`PRICING`.
        detail: Human-readable explanation of the drift.
    """

    model_config = ConfigDict(extra="forbid")

    model_id: str
    field: str
    expected: Decimal | str
    actual: Decimal | str
    detail: str


class PricingDriftReport(BaseModel):
    """Typed result of a pricing-currency check.

    Attributes:
        pricing_version: The embedded :data:`PRICING_VERSION` snapshot tag.
        fetched_at: The embedded :data:`PRICING_FETCHED_AT` snapshot stamp.
        model_count: Number of :data:`PRICING` rows checked.
        is_current: ``True`` when no findings were raised.
        findings: Per-row drift findings (empty when current).
    """

    model_config = ConfigDict(extra="forbid")

    pricing_version: str
    fetched_at: datetime
    model_count: int
    is_current: bool
    findings: list[PricingDriftFinding] = Field(default_factory=list)


def check_pricing_currency() -> PricingDriftReport:
    """Validate the embedded pricing snapshot's shape and internal currency.

    The check is offline: it confirms (a) every row carries the embedded
    :data:`PRICING_VERSION`, and (b) each row's cache rates equal the
    Anthropic-stated multipliers applied to that row's base input rate
    (5m write = 1.25x, 1h write = 2x, cache read = 0.1x). Either condition
    failing produces a :class:`PricingDriftFinding`.

    The weekly CI gate may additionally fetch live rates; that network leg
    is intentionally out of scope here so the verb stays deterministic in
    CI and offline shells.

    Returns:
        A :class:`PricingDriftReport`; :attr:`PricingDriftReport.is_current`
        is ``True`` and :attr:`PricingDriftReport.findings` empty when the
        embedded snapshot is internally consistent.
    """
    findings: list[PricingDriftFinding] = []
    for model_id, row in PRICING.items():
        if row.pricing_version != PRICING_VERSION:
            findings.append(
                PricingDriftFinding(
                    model_id=model_id,
                    field="pricing_version",
                    expected=PRICING_VERSION,
                    actual=row.pricing_version,
                    detail="row pricing_version does not match the embedded snapshot tag",
                )
            )
        _check_multiplier(
            findings,
            model_id=model_id,
            field="cache_read_per_token",
            actual=row.cache_read_per_token,
            expected=row.input_per_token * CACHE_READ_MULTIPLIER,
            multiplier=CACHE_READ_MULTIPLIER,
        )
        _check_multiplier(
            findings,
            model_id=model_id,
            field="cache_write_5m_per_token",
            actual=row.cache_write_5m_per_token,
            expected=row.input_per_token * CACHE_WRITE_5M_MULTIPLIER,
            multiplier=CACHE_WRITE_5M_MULTIPLIER,
        )
        _check_multiplier(
            findings,
            model_id=model_id,
            field="cache_write_1h_per_token",
            actual=row.cache_write_1h_per_token,
            expected=row.input_per_token * CACHE_WRITE_1H_MULTIPLIER,
            multiplier=CACHE_WRITE_1H_MULTIPLIER,
        )
    report = PricingDriftReport(
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
        model_count=len(PRICING),
        is_current=not findings,
        findings=findings,
    )
    logger.info(
        f"check_pricing_currency models={report.model_count} "
        f"current={report.is_current} findings={len(report.findings)}"
    )
    return report


def _check_multiplier(
    findings: list[PricingDriftFinding],
    *,
    model_id: str,
    field: str,
    actual: Decimal,
    expected: Decimal,
    multiplier: Decimal,
) -> None:
    """Append a finding when *actual* drifts from the derived *expected* rate."""
    if actual != expected:
        findings.append(
            PricingDriftFinding(
                model_id=model_id,
                field=field,
                expected=expected,
                actual=actual,
                detail=f"expected input_per_token x {multiplier} cache multiplier",
            )
        )
