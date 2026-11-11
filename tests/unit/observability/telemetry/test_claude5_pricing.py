"""Claude 5 ids price at their own published row, never at a 4.x or sibling rate.

Lookup is exact: an id with no row of its own, a dated spelling no row lists
and the opencode ``provider/model`` form of a Claude 5 id all resolve to no
row, so none of them can be billed at another model's rate.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from eawf.observability.telemetry.pricing import TokenUsage, load_price_book

_ON = date(2026, 11, 10)
_BOOK = load_price_book(repo_root=None, overrides={})

_PRICED_CLAUDE5 = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-sonnet-5", "claude-opus-5")

#: Claude 5 spellings with no row of their own: an undeclared generation, an
#: undeclared family, a dated variant no row lists and the opencode form.
_UNPRICED_CLAUDE5 = (
    "claude-opus-5-9",
    "claude-haiku-5",
    "claude-opus-5-20261001",
    "anthropic/claude-opus-5-5",
)


@pytest.mark.parametrize("model", _PRICED_CLAUDE5)
def test_claude5_id_prices_at_its_own_row(model: str) -> None:
    row = _BOOK.resolve(model, on=_ON)

    assert row is not None and row.id == model
    assert _BOOK.resolve(f"{model}[1m]", on=_ON) is row


def test_sonnet_5_5_does_not_take_the_sonnet_5_cache_read_rate() -> None:
    """Sonnet 5.5 reads cache at 0.05x input, so the Sonnet 5 row would overbill it."""
    sonnet_5_5 = _BOOK.list_rates("claude-sonnet-5-5", on=_ON)
    sonnet_5 = _BOOK.list_rates("claude-sonnet-5", on=_ON)

    assert sonnet_5_5 is not None and sonnet_5 is not None
    assert sonnet_5_5.cache_read == Decimal("0.10")
    assert sonnet_5.cache_read == Decimal("0.20")


@pytest.mark.parametrize("model", _UNPRICED_CLAUDE5)
def test_claude5_id_without_a_row_is_never_priced(model: str) -> None:
    quote = _BOOK.quote(model, TokenUsage(input=1000), on=_ON)

    assert _BOOK.resolve(model, on=_ON) is None
    assert quote.cost_usd is None and quote.unpriced_tokens == 1000


def test_priced_claude5_cost_sums_its_published_rates() -> None:
    quote = _BOOK.quote(
        "claude-opus-5-5", TokenUsage(input=1000, output=1000, cache_read=1000), on=_ON
    )

    assert quote.cost_usd == pytest.approx(Decimal("0.0242"))


def test_unpriced_claude5_cost_is_null_and_names_a_reason() -> None:
    quote = _BOOK.quote("claude-opus-5-9", TokenUsage(input=1000, output=1000), on=_ON)

    assert quote.cost_usd is None
    assert quote.reason == f"no price row for claude-opus-5-9 on {_ON}"
