"""Pricing coverage is weighed by tokens and split by cause; an aggregate states its coverage.

A row count reads healthy while one large unpriced session carries most of
the volume, so the coverage is taken over tokens. Every unpriced token is
assigned the cause its row can name, and a share over nothing is
undefined rather than zero.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from itertools import count

import pytest
from pydantic import ValidationError

from eawf.observability.measurement.coverage import Coverage, PricingCoverage, pricing_coverage
from eawf.observability.telemetry import pricing
from eawf.observability.telemetry.models import ObservedSession, PriceSourceKind

_REF = count(1)

#: Published per-MTok rates for each Claude 5 id: input, output, 5m write,
#: 1h write, cache read.
_CLAUDE_5_RATES = {
    "claude-opus-5-5": ("4", "20", "5", "8", "0.20"),
    "claude-sonnet-5-5": ("2", "10", "2.50", "4", "0.10"),
    "claude-sonnet-5": ("2", "10", "2.50", "4", "0.20"),
}
_SOURCE_URL = re.compile(r"^https://[\w.]*claude\.com/\S+$")
_ON = date(2026, 11, 10)


def _session(
    tokens: int, source: PriceSourceKind, *, model: str | None = "claude-opus-5-5"
) -> ObservedSession:
    priced = source is not PriceSourceKind.UNPRICED
    return ObservedSession(
        vendor_session_ref=f"vsid-{next(_REF):032x}",
        runtime="claude",
        project_id="proj",
        model=model,
        input_tokens=tokens,
        total_tokens=tokens,
        cost_usd=Decimal("0.01") if priced else None,
        price_source=source,
        rate_table_version="v1" if source is PriceSourceKind.LIST_RECONSTRUCTED else None,
    )


def test_meas_061_one_large_unpriced_session_outweighs_many_priced_rows() -> None:
    priced = [_session(100, PriceSourceKind.LIST_RECONSTRUCTED) for _ in range(9)]
    unpriced = _session(9_100, PriceSourceKind.UNPRICED, model="vendor-new-model")

    result = pricing_coverage([*priced, unpriced])

    # nine of ten rows are priced, but only 9% of the tokens are
    assert result.total_tokens == 10_000
    assert result.share_by_source == {
        "billed": pytest.approx(0.0),
        "list-reconstructed": pytest.approx(0.09),
        "unpriced": pytest.approx(0.91),
    }
    assert result.unpriced_tokens_by_cause == {"model_not_in_rate_table": 9_100}


def test_meas_061_each_unpriced_token_is_assigned_its_cause() -> None:
    rows = [
        _session(40, PriceSourceKind.BILLED),
        _session(25, PriceSourceKind.UNPRICED, model=None),
        _session(35, PriceSourceKind.UNPRICED, model="unlisted"),
    ]

    result = pricing_coverage(rows)

    assert result.unpriced_tokens_by_cause == {
        "model_unrecorded": 25,
        "model_not_in_rate_table": 35,
    }
    assert result.share_by_source["billed"] == pytest.approx(0.4)


def test_meas_052_the_sweep_aggregate_declares_its_coverage() -> None:
    rows = [
        _session(40, PriceSourceKind.BILLED),
        _session(0, PriceSourceKind.UNPRICED),
        _session(60, PriceSourceKind.UNPRICED, model=None),
    ]

    coverage = pricing_coverage(rows).coverage

    assert (coverage.subjects_total, coverage.subjects_contributing) == (3, 2)
    assert coverage.subjects_unattributed == 1
    assert coverage.contributing_share == pytest.approx(2 / 3)
    assert coverage.unattributed_share == pytest.approx(0.5)
    assert coverage.null_fields == ("cost_usd",)


def test_meas_052_an_empty_sweep_states_undefined_shares_not_zero() -> None:
    result = pricing_coverage([])

    assert result.total_tokens == 0
    assert set(result.share_by_source.values()) == {None}
    assert result.coverage.contributing_share is None
    assert result.coverage.null_fields == ()
    assert result.model_dump(mode="json")["share_by_source"]["unpriced"] is None


def test_meas_052_a_single_priced_session_is_fully_covered() -> None:
    result = pricing_coverage([_session(7, PriceSourceKind.BILLED)])

    assert result.share_by_source["billed"] == pytest.approx(1.0)
    assert result.unpriced_tokens_by_cause == {}
    assert result.coverage.contributing_share == pytest.approx(1.0)


def test_meas_052_coverage_refuses_parts_larger_than_the_whole() -> None:
    with pytest.raises(ValidationError, match="exceed"):
        Coverage(subjects_total=1, subjects_contributing=2)
    with pytest.raises(ValidationError, match="unattributed"):
        Coverage(subjects_total=3, subjects_contributing=1, subjects_unattributed=2)


def test_meas_061_breakdowns_that_do_not_reconcile_are_refused() -> None:
    with pytest.raises(ValidationError, match="sum to the total"):
        PricingCoverage(
            total_tokens=10,
            tokens_by_source={PriceSourceKind.BILLED: 9},
            unpriced_tokens_by_cause={},
            coverage=Coverage(subjects_total=1, subjects_contributing=1),
        )
    with pytest.raises(ValidationError, match="by cause"):
        PricingCoverage(
            total_tokens=10,
            tokens_by_source={PriceSourceKind.UNPRICED: 10},
            unpriced_tokens_by_cause={"model_unrecorded": 4},
            coverage=Coverage(subjects_total=1, subjects_contributing=1),
        )


@pytest.mark.parametrize("model", sorted(_CLAUDE_5_RATES))
def test_claude_5_models_are_priced(model: str) -> None:
    """Each Claude 5 id and its ``[1m]`` form price at the published rate row."""
    expected = tuple(Decimal(rate) for rate in _CLAUDE_5_RATES[model])
    book = pricing.load_price_book(repo_root=None, overrides={})
    rates = book.list_rates(model, on=_ON)

    assert rates is not None
    assert (
        rates.input,
        rates.output,
        rates.cache_write["5m"],
        rates.cache_write["1h"],
        rates.cache_read,
    ) == expected
    for queried in (model, f"{model}[1m]"):
        row = book.resolve(queried, on=_ON)
        assert row is not None and row.id == model, queried


@pytest.mark.parametrize("model", sorted(_CLAUDE_5_RATES))
def test_claude_5_row_cites_its_published_source(model: str) -> None:
    """Each Claude 5 row names the vendor pricing page and the date it was read."""
    row = pricing.load_price_book(repo_root=None, overrides={}).resolve(model, on=_ON)

    assert row is not None
    assert _SOURCE_URL.match(row.source_url), row.source_url
    assert row.verified_on is not None


@pytest.mark.parametrize("model", ["claude-opus-5-9", "claude-haiku-5", "claude-opus-5-5-20261001"])
def test_unlisted_claude_5_ids_stay_unpriced(model: str) -> None:
    """Boundary: a Claude 5 id with no row of its own never takes another row's rate."""
    book = pricing.load_price_book(repo_root=None, overrides={})

    assert book.resolve(model, on=_ON) is None
