"""Claude 5 model ids resolve unpriced, with a typed reason, never at a 4.x rate.

No published Claude 5 rate is cited in the embedded snapshot, so the ids
eawf dispatches to (``claude-opus-5-5``, ``claude-sonnet-5``) must resolve
to no row. Every one of them prefix-matches a bare family alias that
prices at the 4.x rate, which is exactly the fallback that would record a
fabricated list price; these tests red if any Claude 5 id reaches it.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest
from pydantic import ValidationError

from eawf.observability.telemetry.models import PriceSourceKind
from eawf.observability.telemetry.pricing import (
    NO_CITED_RATE_SOURCE,
    PRICING,
    UNPRICED_MODELS,
    UnpricedModel,
    lookup_pricing,
    resolve_price_source,
    unpriced_model,
)
from eawf.runtime.runtimes.metering import price_token_counts

_DISPATCHED_CLAUDE5 = ("claude-opus-5-5", "claude-sonnet-5")

#: Claude 5 spellings a prefix fallback would otherwise bind to a 4.x row:
#: the dispatched ids, dated variants, a generation-only id, an undeclared
#: family and the opencode ``provider/model`` form.
_CLAUDE5_IDS = (
    *_DISPATCHED_CLAUDE5,
    "claude-opus-5-5-20261001",
    "claude-sonnet-5-20261001",
    "claude-opus-5",
    "claude-haiku-5",
    "anthropic/claude-opus-5-5",
)


@pytest.mark.parametrize("model", _CLAUDE5_IDS)
def test_claude5_id_is_never_priced_by_a_4x_prefix_fallback(model: str) -> None:
    assert lookup_pricing(model) is None
    assert resolve_price_source(model) == (PriceSourceKind.UNPRICED, None)


@pytest.mark.parametrize("model", _DISPATCHED_CLAUDE5)
def test_dispatched_claude5_id_carries_a_typed_unpriced_reason(model: str) -> None:
    entry = unpriced_model(model)

    assert isinstance(entry, UnpricedModel)
    assert entry.model_id == model
    assert entry.reason == NO_CITED_RATE_SOURCE


def test_dated_variant_falls_under_its_unpriced_entry() -> None:
    entry = unpriced_model("claude-opus-5-5-20261001")

    assert entry is not None
    assert entry.model_id == "claude-opus-5-5"


@pytest.mark.parametrize("model", ["", "claude-opus-4-7", "claude-opus-5-4", "gpt-5.5"])
def test_id_outside_the_unpriced_entries_has_no_entry(model: str) -> None:
    """Boundary: empty, priced and near-miss ids resolve no unpriced entry."""
    assert unpriced_model(model) is None


def test_unpriced_entries_are_keyed_by_their_own_id_and_carry_no_rate() -> None:
    assert set(UNPRICED_MODELS) == set(_DISPATCHED_CLAUDE5)
    assert all(key == entry.model_id for key, entry in UNPRICED_MODELS.items())
    assert not set(UNPRICED_MODELS) & set(PRICING)


def test_unpriced_entry_is_a_closed_frozen_model() -> None:
    with pytest.raises(ValidationError):
        UnpricedModel.model_validate({"model_id": "claude-opus-5-5", "reason": "x", "rate": 1})
    with pytest.raises(ValidationError):
        UnpricedModel.model_validate({"model_id": "claude-opus-5-5"})
    entry = UNPRICED_MODELS["claude-opus-5-5"]
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


def test_unpriced_claude5_cost_is_null_and_logs_its_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="eawf.runtime.runtimes.metering"):
        cost = price_token_counts(
            "claude-opus-5-5",
            input_tokens=1000,
            output_tokens=1000,
            cache_creation_5m_input_tokens=0,
            cache_creation_1h_input_tokens=0,
            cache_read_input_tokens=0,
        )

    assert cost is None
    assert NO_CITED_RATE_SOURCE in caplog.text
