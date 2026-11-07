"""The per-model rate table and the one cost-ordered ranking."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from eawf.observability.reflect.analysis import (
    HistorySession,
    Metric,
    StructuralIssue,
    analyze_sessions,
)
from eawf.observability.reflect.rates import (
    UNKNOWN_MODEL,
    PricingQuality,
    RateRow,
    cost_ranking,
    pricing_quality,
    rate_table,
)
from eawf.observability.telemetry.models import ObservedRequest, PriceSourceKind
from eawf.observability.telemetry.pricing import lookup_pricing

_NOW = datetime(2026, 11, 1, tzinfo=UTC)


def _request(
    session: int,
    ordinal: int,
    *,
    model: str | None,
    runtime: str = "claude",
    cost: str | None = "0.50",
) -> ObservedRequest:
    priced = cost is not None
    return ObservedRequest(
        vendor_session_ref=f"vsid-{session:032x}",
        runtime=runtime,  # type: ignore[arg-type]
        request_ref=f"req-{session:016x}{ordinal:016x}",
        ordinal=ordinal,
        model=model,
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=1_000,
        cache_write_tokens=50,
        cache_write_1h_tokens=10,
        total_tokens=1_170,
        cost_usd=Decimal(cost) if priced else None,
        price_source=PriceSourceKind.LIST_RECONSTRUCTED if priced else PriceSourceKind.UNPRICED,
        rate_table_version="v" if priced else None,
    )


def _rows() -> tuple[RateRow, ...]:
    return rate_table(
        [
            _request(1, 1, model="claude-opus-4-7"),
            _request(1, 2, model="claude-opus-4-7"),
            _request(2, 1, model="claude-opus-4-7", cost="1.00"),
            _request(2, 2, model="claude-haiku-4-5"),
            _request(3, 1, model="gpt-6.1-sol", runtime="codex", cost=None),
            _request(4, 1, model="claude-unreleased-9", cost=None),
            _request(5, 1, model=None, cost=None),
            _request(6, 1, model="<synthetic>", cost=None),
        ]
    )


def _row(rows: tuple[RateRow, ...], model: str) -> RateRow:
    return next(row for row in rows if row.model == model)


def test_rate_table_has_one_row_per_model_seen() -> None:
    rows = _rows()

    assert sorted(row.model for row in rows) == sorted(
        [
            "claude-opus-4-7",
            "claude-haiku-4-5",
            "gpt-6.1-sol",
            "claude-unreleased-9",
            UNKNOWN_MODEL,
            "<synthetic>",
        ]
    )
    opus = _row(rows, "claude-opus-4-7")
    assert opus.sessions == 2
    assert opus.requests == 3
    assert (opus.input_tokens, opus.output_tokens) == (300, 60)
    assert (opus.cache_write_tokens, opus.cache_read_tokens) == (150, 3_000)
    assert opus.total_tokens == 3_510
    assert opus.cost_usd == Decimal("2.00")
    assert opus.quality is PricingQuality.VERIFIED
    assert opus.unpriced_reason is None
    pricing = lookup_pricing("claude-opus-4-7")
    assert pricing is not None
    assert opus.rates is not None
    assert opus.rates.input == pricing.input_per_token * 1_000_000
    assert opus.rates.output == pricing.output_per_token * 1_000_000
    assert opus.rates.cache_write_5m == pricing.cache_write_5m_per_token * 1_000_000
    assert opus.rates.cache_write_1h == pricing.cache_write_1h_per_token * 1_000_000
    assert opus.rates.cache_read == pricing.cache_read_per_token * 1_000_000


@pytest.mark.parametrize(
    ("model", "reason"),
    [
        ("claude-unreleased-9", "No published rate for this model"),
        (UNKNOWN_MODEL, "The harness did not record which model ran"),
        ("<synthetic>", "Synthetic record with no real provider call"),
    ],
)
def test_an_unpriced_model_shows_no_rate_and_names_its_reason(model: str, reason: str) -> None:
    row = _row(_rows(), model)

    assert row.rates is None
    assert row.cost_usd is None
    assert row.quality is PricingQuality.UNAVAILABLE
    assert row.unpriced_reason == reason
    assert row.total_tokens == 1_170


def test_a_codex_row_carries_tokens_and_no_dollar_figure() -> None:
    codex = _row(_rows(), "gpt-6.1-sol")

    assert codex.runtimes == ("codex",)
    assert codex.total_tokens == 1_170
    assert codex.rates is None
    assert codex.cost_usd is None
    assert codex.quality is PricingQuality.PLACEHOLDER
    assert codex.unpriced_reason is not None
    assert "placeholder" in codex.unpriced_reason


def test_a_codex_request_never_lends_a_model_a_rate() -> None:
    # The embedded table resolves this id to a placeholder row.
    assert lookup_pricing("gpt-6.1-sol") is not None
    rows = rate_table(
        [
            _request(1, 1, model="gpt-6.1-sol", runtime="codex", cost=None),
            _request(2, 1, model="gpt-6.1-sol", runtime="claude", cost="3.00"),
        ]
    )

    (row,) = rows
    assert row.runtimes == ("claude", "codex")
    assert row.cost_usd is None
    assert row.rates is None


def test_cost_ranking_holds_no_codex_and_no_unpriced_row() -> None:
    ranked = cost_ranking(_rows())

    assert [row.model for row in ranked] == ["claude-opus-4-7", "claude-haiku-4-5"]
    assert all(row.quality is PricingQuality.VERIFIED for row in ranked)
    assert all("codex" not in row.runtimes for row in ranked)


def test_rate_table_and_ranking_of_nothing_are_empty() -> None:
    assert rate_table([]) == ()
    assert cost_ranking(()) == ()


def test_rate_table_orders_by_tokens_then_model() -> None:
    rows = rate_table(
        [_request(1, 1, model="claude-opus-4-7"), _request(1, 2, model="claude-haiku-4-5")]
    )

    assert [row.model for row in rows] == ["claude-haiku-4-5", "claude-opus-4-7"]


@pytest.mark.parametrize(
    ("runtime", "model", "quality"),
    [
        ("claude", "claude-opus-4-7", PricingQuality.VERIFIED),
        ("claude", "claude-opus-4-7-20260514", PricingQuality.VERIFIED),
        ("codex", "gpt-6.1-sol", PricingQuality.PLACEHOLDER),
        ("codex", None, PricingQuality.PLACEHOLDER),
        ("claude", None, PricingQuality.UNAVAILABLE),
        ("claude", "<synthetic>", PricingQuality.UNAVAILABLE),
        ("claude", "", PricingQuality.UNAVAILABLE),
        ("claude", "claude-unreleased-9", PricingQuality.UNAVAILABLE),
    ],
)
def test_pricing_quality_per_runtime_and_model(
    runtime: str, model: str | None, quality: PricingQuality
) -> None:
    assert pricing_quality(runtime, model) is quality  # type: ignore[arg-type]


def test_rate_row_refuses_an_unknown_field() -> None:
    row = _rows()[0]

    with pytest.raises(ValueError, match="extra"):
        RateRow.model_validate({**row.model_dump(), "dollars": 1})


def test_a_codex_session_is_in_no_cost_ordered_ranking() -> None:
    def session(index: int, runtime: str, cost: Decimal | None) -> HistorySession:
        return HistorySession(
            session_ref=f"vsid-{index:032x}",
            runtime=runtime,  # type: ignore[arg-type]
            project_key="cwd-a",
            project_label="SYN",
            started_at=_NOW,
            ended_at=_NOW,
            model="claude-opus-4-7" if runtime == "claude" else "gpt-6.1-sol",
            model_family="m",
            orchestrated=False,
            metrics={Metric.TOTAL_TOKENS: 1_000.0 * (index + 1), Metric.MODEL_STEPS: 4.0},
            cost_usd=cost,
            pricing_quality=pricing_quality(
                runtime, "claude-opus-4-7" if runtime == "claude" else "gpt-6.1-sol"
            ),  # type: ignore[arg-type]
            issues=(StructuralIssue.TIMESTAMP_REGRESSION,),
        )

    analysis = analyze_sessions(
        [session(1, "claude", Decimal(2)), session(2, "codex", Decimal(500))],
        requests=[
            _request(1, 1, model="claude-opus-4-7"),
            _request(2, 1, model="gpt-6.1-sol", runtime="codex", cost=None),
        ],
        now=_NOW,
    )

    codex_ref = f"vsid-{2:032x}"
    assert codex_ref not in analysis.session_cost_ranking
    assert codex_ref in analysis.session_token_ranking
    assert [row.model for row in analysis.model_cost_ranking] == ["claude-opus-4-7"]
    codex_row = next(row for row in analysis.sessions if row.session_ref == codex_ref)
    assert codex_row.cost_usd is None
    assert all(row.cost_usd is None for row in analysis.rates if "codex" in row.runtimes)
