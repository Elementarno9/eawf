"""Claude 5 ids price at their own published row, never at a 4.x or sibling rate.

The snapshot carries cited rows for ``claude-opus-5-5``, ``claude-sonnet-5-5``
and ``claude-sonnet-5``. Every other Claude 5 id prefix-matches only a bare
family alias that prices at the 4.x rate, which is exactly the fallback that
would record a fabricated list price; those ids must resolve to no row.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest
from pydantic import ValidationError

from eawf.observability.telemetry.models import PriceSourceKind
from eawf.observability.telemetry.pricing import (
    PRICING,
    PRICING_VERSION,
    UNPRICED_MODELS,
    UnpricedModel,
    lookup_pricing,
    resolve_price_source,
    unpriced_model,
)
from eawf.runtime.runtimes.metering import price_token_counts

_PRICED_CLAUDE5 = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-sonnet-5")

#: Claude 5 spellings with no row of their own: a generation-only id, an
#: undeclared family and the opencode ``provider/model`` form.
_UNPRICED_CLAUDE5 = (
    "claude-opus-5",
    "claude-opus-5-20261001",
    "claude-haiku-5",
    "anthropic/claude-opus-5-5",
)


@pytest.mark.parametrize("model", _PRICED_CLAUDE5)
def test_claude5_id_and_its_dated_variant_price_at_their_own_row(model: str) -> None:
    for queried in (model, f"{model}-20261001"):
        assert lookup_pricing(queried) is PRICING[model], queried
        assert resolve_price_source(queried) == (
            PriceSourceKind.LIST_RECONSTRUCTED,
            PRICING_VERSION,
        )


def test_sonnet_5_5_does_not_prefix_match_the_sonnet_5_row() -> None:
    """Sonnet 5.5 reads cache at 0.05x input, so the Sonnet 5 row would overbill it."""
    row = lookup_pricing("claude-sonnet-5-5")

    assert row is PRICING["claude-sonnet-5-5"]
    assert row is not PRICING["claude-sonnet-5"]
    assert row.cache_read_per_token == Decimal("0.1e-6")
    assert PRICING["claude-sonnet-5"].cache_read_per_token == Decimal("0.2e-6")


@pytest.mark.parametrize("model", _UNPRICED_CLAUDE5)
def test_claude5_id_without_a_row_is_never_priced_by_a_4x_prefix_fallback(model: str) -> None:
    assert lookup_pricing(model) is None
    assert resolve_price_source(model) == (PriceSourceKind.UNPRICED, None)


@pytest.mark.parametrize("model", ["", *_PRICED_CLAUDE5, "claude-opus-5", "gpt-5.5"])
def test_no_id_falls_under_an_unpriced_entry(model: str) -> None:
    """Boundary: with the unpriced map empty, no id resolves an entry."""
    assert UNPRICED_MODELS == {}
    assert unpriced_model(model) is None


def test_unpriced_entry_is_a_closed_frozen_model() -> None:
    with pytest.raises(ValidationError):
        UnpricedModel.model_validate({"model_id": "claude-opus-5", "reason": "x", "rate": 1})
    with pytest.raises(ValidationError):
        UnpricedModel.model_validate({"model_id": "claude-opus-5"})
    entry = UnpricedModel(model_id="claude-opus-5", reason="no rate")
    with pytest.raises(ValidationError):
        entry.reason = "priced"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("model", "input_rate"),
    [
        ("claude-opus-4-7-20260514", Decimal("5e-6")),
        ("claude-opus-4-9", Decimal("5e-6")),
        ("claude-sonnet-4-6", Decimal("3e-6")),
        ("claude-opus", Decimal("5e-6")),
        ("opus", Decimal("5e-6")),
        ("anthropic/claude-opus-4-8-20260101", Decimal("5e-6")),
    ],
)
def test_4x_and_alias_ids_still_price(model: str, input_rate: Decimal) -> None:
    """Boundary: the generation guard leaves every 4.x and bare-alias id priced."""
    row = lookup_pricing(model)

    assert row is not None
    assert row.input_per_token == input_rate


def test_priced_claude5_cost_sums_its_published_rates() -> None:
    cost = price_token_counts(
        "claude-opus-5-5",
        input_tokens=1000,
        output_tokens=1000,
        cache_creation_5m_input_tokens=0,
        cache_creation_1h_input_tokens=0,
        cache_read_input_tokens=1000,
    )

    assert cost == pytest.approx(Decimal("0.0242"))


def test_unpriced_claude5_cost_is_null_and_logs_a_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="eawf.runtime.runtimes.metering"):
        cost = price_token_counts(
            "claude-opus-5",
            input_tokens=1000,
            output_tokens=1000,
            cache_creation_5m_input_tokens=0,
            cache_creation_1h_input_tokens=0,
            cache_read_input_tokens=0,
        )

    assert cost is None
    assert "no rate row matches" in caplog.text
