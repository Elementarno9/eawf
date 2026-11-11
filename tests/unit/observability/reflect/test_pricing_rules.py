"""The pricing rules: dated exact lookup, tiers, modifiers, the shipped table and overrides."""

from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

import eawf.kernel.config.layered as layered
import eawf.observability.reflect.rates as rates_module
import eawf.observability.telemetry.pricing as pricing
import eawf.observability.telemetry.projector as projector
from eawf.kernel.config.schema import PriceOverride, PriceRates
from eawf.observability.telemetry.models import ObservedRequest, ObservedSession, PriceSourceKind
from eawf.observability.telemetry.pricing import (
    OVERRIDE_RATE_VERSION,
    PriceBook,
    TokenUsage,
    load_price_book,
    price_overrides,
)
from eawf.observability.telemetry.projector import index_session_history
from eawf.observability.telemetry.sources.session_history import (
    RequestLedger,
    TranscriptFold,
    discover_claude_project,
    fold_history_lines,
)
from eawf.observability.telemetry.store import SqliteMetricsStore

_MILLION = 1_000_000
_ON = date(2026, 11, 10)


def _rates(input_rate: float, *, writes: dict[str, float] | None = None) -> dict[str, Any]:
    return {
        "input": input_rate,
        "output": input_rate * 5,
        "cache_read": input_rate / 10,
        "cache_write": writes if writes is not None else {"5m": 1.25, "1h": 2},
    }


def _row(model: str, input_rate: float, **changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": model,
        "match": [model],
        "effective_from": None,
        "effective_to": None,
        "source_url": "https://example.invalid/pricing",
        "verified_on": "2026-11-10",
        "verified_by": "eye",
        "rates": _rates(input_rate),
        "tiers": [],
        "modifiers": {"fast": None, "geo_us": None},
    }
    row.update(changes)
    return row


def _book(
    tmp_path: Path, *files: dict[str, Any], overrides: dict[str, PriceOverride] | None = None
) -> PriceBook:
    for body in files:
        (tmp_path / f"{body['vendor']}.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    return load_price_book(repo_root=None, directory=tmp_path, overrides=overrides or {})


def _shipped() -> PriceBook:
    return load_price_book(repo_root=None, overrides={})


def _input_cost(book: PriceBook, model: str, on: date, tokens: int = _MILLION) -> Decimal | None:
    return book.quote(model, TokenUsage(input=tokens), on=on).cost_usd


# ---------- dated, exact lookup ----------


def test_lookup_and_effective_dates(tmp_path: Path) -> None:
    book = _book(
        tmp_path,
        {
            "vendor": "anthropic",
            "aliases": [{"alias": "short", "target": "model-b", "from": "2026-11-10", "to": None}],
            "models": [
                _row("model-a", 1, effective_to="2026-06-01"),
                _row("model-a", 2, effective_from="2026-06-01"),
                _row("model-b", 3, match=["model-b", "model-b-20260101"]),
            ],
        },
    )

    # The row in effect at the request's date: from inclusive, to exclusive.
    assert _input_cost(book, "model-a", date(2026, 5, 31)) == Decimal(1)
    assert _input_cost(book, "model-a", date(2026, 6, 1)) == Decimal(2)
    # Exact id, then match after stripping [1m], then the alias in effect.
    assert _input_cost(book, "model-a[1m]", _ON) == Decimal(2)
    assert _input_cost(book, "model-b-20260101", _ON) == Decimal(3)
    assert _input_cost(book, "short", _ON) == Decimal(3)
    before_alias = book.quote("short", TokenUsage(input=_MILLION), on=date(2026, 11, 9))
    assert before_alias.cost_usd is None and before_alias.unpriced_tokens == _MILLION
    # No prefix fallback: a neighbour's id never lends its rate, and nothing is zero.
    for stranger in ("model-b-20260202", "model-a-2", "model", "", "unknown-model"):
        quote = book.quote(stranger, TokenUsage(input=10, output=5), on=_ON)
        assert quote.source is None and quote.cost_usd is None, stranger
        assert quote.unpriced_tokens == 15 and not quote.priced, stranger


def test_shipped_ids_resolve_exactly_and_aliases_by_date() -> None:
    book = _shipped()

    assert book.resolve("claude-opus-4-8[1m]", on=_ON).id == "claude-opus-4-8"  # type: ignore[union-attr]
    assert book.resolve("claude-haiku-4-5-20251001", on=_ON).id == "claude-haiku-4-5"  # type: ignore[union-attr]
    assert book.resolve("claude-opus-4-7-20260514", on=_ON) is None
    assert book.resolve("claude-opus-5-9", on=_ON) is None
    assert book.resolve("opus", on=date(2026, 11, 9)) is None
    assert book.resolve("opus", on=_ON).id == "claude-opus-5-5"  # type: ignore[union-attr]
    assert book.resolve("haiku", on=_ON).id == "claude-haiku-5-5"  # type: ignore[union-attr]


def test_the_old_table_and_prefix_fallback_are_gone() -> None:
    for name in ("PRICING", "lookup_pricing", "UNPRICED_MODELS", "FAMILY_ALIAS_GENERATION"):
        assert not hasattr(pricing, name), name
    assert not hasattr(rates_module, "_CODEX_REASON")


def test_request_date_reads_utc_and_naive_as_utc() -> None:
    late = datetime(2026, 11, 9, 23, 30, tzinfo=UTC)

    assert pricing.request_date(late) == date(2026, 11, 9)
    assert pricing.request_date(late.replace(tzinfo=None)) == date(2026, 11, 9)
    assert pricing.request_date(None) == datetime.now(tz=UTC).date()


# ---------- tiers and modifiers ----------


def test_haiku_tier_and_modifiers() -> None:
    book = _shipped()

    def quote(model: str, **usage: Any) -> pricing.PriceQuote:
        return book.quote(model, TokenUsage(**usage), on=_ON)

    # Haiku 5.5 bills input at 0.10 at 99,999 prompt tokens and 0.50 at 100,001.
    assert (
        quote("claude-haiku-5-5", input=99_999).cost_usd
        == Decimal(99_999) * Decimal("0.10") / _MILLION
    )
    assert (
        quote("claude-haiku-5-5", input=100_000).cost_usd
        == Decimal(100_000) * Decimal("0.10") / _MILLION
    )
    assert (
        quote("claude-haiku-5-5", input=100_001).cost_usd
        == Decimal(100_001) * Decimal("0.50") / _MILLION
    )
    # The prompt is input + cache read + cache write; every class then bills at tier rates.
    mixed = quote(
        "claude-haiku-5-5", input=50_000, cache_read=40_000, cache_write={"5m": 10_001}, output=10
    )
    assert (
        mixed.cost_usd
        == (
            50_000 * Decimal("0.50")
            + 40_000 * Decimal("0.05")
            + 10_001 * Decimal("0.625")
            + 10 * Decimal("2.50")
        )
        / _MILLION
    )
    # Fast mode applies the fast rates; a class it does not cover is unpriced.
    fast = quote("claude-opus-5-5", input=_MILLION, output=_MILLION, speed="fast")
    assert fast.cost_usd == Decimal(48) and fast.priced
    partial = quote("claude-opus-5-5", input=_MILLION, cache_read=10, speed="fast")
    assert partial.cost_usd == Decimal(8) and partial.unpriced_tokens == 10 and not partial.priced
    assert quote("claude-opus-5-5", input=_MILLION, speed="standard").cost_usd == Decimal(4)
    # A row with no fast rates prices no fast request; a tier states no fast rates either.
    assert quote("claude-sonnet-5-5", input=_MILLION, speed="fast").cost_usd is None
    assert quote("claude-haiku-5-5", input=200_000, speed="fast").cost_usd is None
    # The US region multiplies every class by geo_us.
    assert quote("claude-opus-5-5", input=_MILLION, inference_geo="us").cost_usd == Decimal("4.4")
    assert quote(
        "claude-opus-5-5", input=_MILLION, speed="fast", inference_geo="us"
    ).cost_usd == Decimal("8.8")
    assert quote("claude-haiku-5-5", input=100_001, inference_geo="us").cost_usd == (
        Decimal(100_001) * Decimal("0.50") * Decimal("1.1") / _MILLION
    )
    assert quote("claude-opus-5-5", input=_MILLION, inference_geo="eu").cost_usd == Decimal(4)


def test_mythos_5_2_fast_mode_prices_input_and_output_only() -> None:
    book = _shipped()

    def quote(**usage: Any) -> pricing.PriceQuote:
        return book.quote("claude-mythos-5-2", TokenUsage(speed="fast", **usage), on=_ON)

    full = quote(input=_MILLION, output=_MILLION)
    assert full.priced and full.cost_usd == Decimal(96)
    single = quote(input=1)
    assert single.cost_usd == Decimal(16) / _MILLION
    # Fast mode states no cache rates, so those classes stay unpriced.
    cached = quote(input=_MILLION, cache_read=10, cache_write={"5m": 20, "1h": 30})
    assert cached.cost_usd == Decimal(16) and cached.unpriced_tokens == 60 and not cached.priced
    assert quote(output=_MILLION, inference_geo="us").cost_usd == Decimal("88.0")


def test_a_modifier_the_row_does_not_state_leaves_the_request_unpriced(tmp_path: Path) -> None:
    book = _book(tmp_path, {"vendor": "anthropic", "aliases": [], "models": [_row("model-a", 1)]})

    geo = book.quote("model-a", TokenUsage(input=10), on=_ON)
    us = book.quote("model-a", TokenUsage(input=10, inference_geo="us"), on=_ON)

    assert geo.priced
    assert us.cost_usd is None and us.unpriced_tokens == 10


def test_a_write_class_the_row_does_not_name_is_unpriced() -> None:
    quote = _shipped().quote(
        "claude-opus-5-5", TokenUsage(input=10, cache_write={"30m": 7}), on=_ON
    )

    assert quote.cost_usd == Decimal(40) / _MILLION
    assert quote.unpriced_tokens == 7


def test_token_usage_refuses_a_negative_count_and_an_unknown_field() -> None:
    with pytest.raises(ValidationError):
        TokenUsage(input=-1)
    with pytest.raises(ValidationError):
        TokenUsage.model_validate({"input": 1, "dollars": 2})


# ---------- the shipped table ----------

#: Spec 16 "Price table at 2026-11-10": input, output, read, write 5m, write 1h, fast.
#: Mythos 5.2 fast mode follows the vendor page's fast-mode table, which lists it
#: at $16 / $80 where the spec table names no fast rate.
_SPEC_TABLE: dict[str, tuple[str, str, str, str, str, tuple[str, str] | None]] = {
    "claude-opus-5-5": ("4", "20", "0.20", "5", "8", ("8", "40")),
    "claude-sonnet-5-5": ("2", "10", "0.10", "2.50", "4", None),
    "claude-sonnet-5": ("2", "10", "0.20", "2.50", "4", None),
    "claude-fable-5-2": ("8", "40", "0.20", "10", "16", ("16", "80")),
    "claude-mythos-5-2": ("8", "40", "0.20", "10", "16", ("16", "80")),
    "claude-fable-5-1": ("10", "50", "0.25", "12.50", "20", None),
    "claude-mythos-5-1": ("10", "50", "0.25", "12.50", "20", None),
    "claude-fable-5": ("10", "50", "1", "12.50", "20", None),
    "claude-mythos-5": ("10", "50", "1", "12.50", "20", None),
    "claude-opus-5": ("5", "25", "0.50", "6.25", "10", ("10", "50")),
    "claude-opus-4-8": ("5", "25", "0.50", "6.25", "10", ("10", "50")),
    "claude-opus-4-7": ("5", "25", "0.50", "6.25", "10", None),
    "claude-opus-4-6": ("5", "25", "0.50", "6.25", "10", None),
    "claude-opus-4-5": ("5", "25", "0.50", "6.25", "10", None),
    "claude-opus-4-1": ("15", "75", "1.50", "18.75", "30", None),
    "claude-opus-4": ("15", "75", "1.50", "18.75", "30", None),
    "claude-sonnet-4-6": ("3", "15", "0.30", "3.75", "6", None),
    "claude-sonnet-4-5": ("3", "15", "0.30", "3.75", "6", None),
    "claude-sonnet-4": ("3", "15", "0.30", "3.75", "6", None),
    "claude-haiku-4-5": ("1", "5", "0.10", "1.25", "2", None),
    "claude-haiku-5-5": ("0.10", "0.50", "0.01", "0.125", "0.20", None),
}

_OPENAI_IDS = {"gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna", "gpt-5.5", "gpt-5", "gpt-5-mini"}
_ABSENT_CODEX_IDS = ("gpt-5-codex", "codex", "gpt-5.3-codex-spark", "codex-auto-review")


def test_table_rows() -> None:
    book = _shipped()
    anthropic = next(file for file in book.files if file.vendor == "anthropic")

    assert {row.id for row in anthropic.models} == set(_SPEC_TABLE)
    for row in anthropic.models:
        input_rate, output, read, write_5m, write_1h, fast = _SPEC_TABLE[row.id]
        assert row.rates is not None, row.id
        assert (row.rates.input, row.rates.output, row.rates.cache_read) == (
            Decimal(input_rate),
            Decimal(output),
            Decimal(read),
        ), row.id
        assert row.rates.cache_write == {"5m": Decimal(write_5m), "1h": Decimal(write_1h)}, row.id
        stated_fast = row.modifiers.fast
        assert (None if stated_fast is None else (stated_fast.input, stated_fast.output)) == (
            None if fast is None else (Decimal(fast[0]), Decimal(fast[1]))
        ), row.id
        assert row.modifiers.geo_us == Decimal("1.1"), row.id
    haiku = next(row for row in anthropic.models if row.id == "claude-haiku-5-5")
    assert [tier.above_prompt_tokens for tier in haiku.tiers] == [100_000]
    tier = haiku.tiers[0].rates
    assert (tier.input, tier.output, tier.cache_read, tier.cache_write) == (
        Decimal("0.50"),
        Decimal("2.50"),
        Decimal("0.05"),
        {"5m": Decimal("0.625"), "1h": Decimal(1)},
    )
    # The OpenAI rows are named; each prices exactly when its row states rates.
    openai = next(file for file in book.files if file.vendor == "openai")
    assert {row.id for row in openai.models} == _OPENAI_IDS
    for row in openai.models:
        assert book.quote(row.id, TokenUsage(input=10), on=_ON).priced == (row.rates is not None)
    for absent in _ABSENT_CODEX_IDS:
        assert book.resolve(absent, on=_ON) is None, absent
        assert book.quote(absent, TokenUsage(input=10), on=_ON).cost_usd is None, absent


def _codex_record(second: int, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"timestamp": f"2026-11-10T10:00:{second:02d}Z", "type": kind, "payload": payload}


def _codex_rollout(model: str) -> bytes:
    usage = {
        "input_tokens": 100,
        "cached_input_tokens": 40,
        "output_tokens": 10,
        "reasoning_output_tokens": 4,
        "total_tokens": 110,
    }
    records = [
        _codex_record(0, "session_meta", {"id": "codex-raw-id", "cwd": "/w"}),
        _codex_record(1, "turn_context", {"model": model, "cwd": "/w"}),
        _codex_record(
            2,
            "event_msg",
            {
                "type": "token_count",
                "info": {"total_token_usage": usage, "last_token_usage": usage},
            },
        ),
    ]
    return "".join(f"{json.dumps(record)}\n" for record in records).encode()


@pytest.mark.parametrize(("model", "priced"), [("gpt-6.1-sol", True), ("gpt-5-codex", False)])
def test_the_codex_dollar_ban_lifts_only_for_rows_in_the_file(
    tmp_path: Path, model: str, priced: bool
) -> None:
    openai = {
        "vendor": "openai",
        "aliases": [],
        "models": [
            _row(
                "gpt-6.1-sol",
                2,
                rates=_rates(2, writes={"30m": 2.5, "12h": 5}),
                modifiers={"fast": None, "geo_us": None},
            )
        ],
    }
    book = _book(tmp_path, openai)
    ledger = RequestLedger(prices=book)
    fold = TranscriptFold(runtime="codex")

    fold_history_lines(io.BytesIO(_codex_rollout(model)), fold, ledger, fallback_session_id="s")

    (row,) = ledger.rows.values()
    assert row.runtime == "codex"
    if priced:
        expected = book.quote(
            model,
            TokenUsage(
                input=row.input_tokens, output=row.output_tokens, cache_read=row.cache_read_tokens
            ),
            on=_ON,
        )
        assert row.price_source is PriceSourceKind.LIST_RECONSTRUCTED
        assert row.cost_usd == expected.cost_usd and row.cost_usd is not None
        assert row.rate_table_version == book.version
    else:
        assert row.price_source is PriceSourceKind.UNPRICED and row.cost_usd is None


# ---------- overrides and re-pricing ----------

_OVERRIDE = PriceOverride(
    rates=PriceRates(
        input=Decimal(1),
        output=Decimal(1),
        cache_read=Decimal(1),
        cache_write={"5m": Decimal(1), "1h": Decimal(1)},
    ),
    note="negotiated rate",
)


def _transcript(project: Path) -> None:
    records = [
        {
            "type": "user",
            "sessionId": "claude-raw-id",
            "cwd": "/w",
            "timestamp": "2026-11-10T10:00:00Z",
            "message": {"content": "do the thing"},
        },
        {
            "type": "assistant",
            "sessionId": "claude-raw-id",
            "timestamp": "2026-11-10T10:00:05Z",
            "message": {
                "id": "msg-1",
                "model": "claude-opus-4-7",
                "content": [],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 30,
                    "cache_creation_input_tokens": 40,
                    "cache_creation": {"ephemeral_1h_input_tokens": 15},
                },
            },
        },
    ]
    project.mkdir(parents=True)
    (project / "claude-raw-id.jsonl").write_text(
        "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
    )


def _rows(store: SqliteMetricsStore) -> tuple[ObservedRequest, ObservedSession]:
    (request,) = store.fetch_all("telemetry_observed_requests", ObservedRequest)
    (session,) = store.fetch_all("telemetry_observed_sessions", ObservedSession)
    assert isinstance(request, ObservedRequest) and isinstance(session, ObservedSession)
    return request, session


def test_overrides_and_reprice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The global layer applies outside a repository.
    global_layer = tmp_path / "global.yaml"
    global_layer.write_text(
        yaml.safe_dump(
            {
                "reflect": {
                    "pricing": {"overrides": {"claude-opus-4-7": _OVERRIDE.model_dump(mode="json")}}
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(layered, "global_config_path", lambda: global_layer)
    assert price_overrides(None) == {"claude-opus-4-7": _OVERRIDE}
    overridden = load_price_book(repo_root=None)
    quote = overridden.quote("claude-opus-4-7[1m]", TokenUsage(input=_MILLION), on=_ON)
    assert (quote.source, quote.cost_usd) == ("override", Decimal(1))
    assert overridden.rate_version(quote) == OVERRIDE_RATE_VERSION
    listed = load_price_book(repo_root=None, overrides={})
    assert overridden.digest != listed.digest

    project = tmp_path / "projects" / "p"
    _transcript(project)
    files = discover_claude_project(project)
    store = SqliteMetricsStore(tmp_path / "index.sqlite")
    store.init_schema()
    first = index_session_history(store, files, project_id="scope", prices=listed)
    request, session = _rows(store)
    assert first.files_read == 1
    assert (
        request.cost_usd
        == (
            10 * Decimal(5)
            + 20 * Decimal(25)
            + 30 * Decimal("0.5")
            + 25 * Decimal("6.25")
            + 15 * Decimal(10)
        )
        / _MILLION
    )
    assert request.rate_table_version == listed.version

    # A changed digest re-prices from the stored token classes; no transcript is read.
    def no_read(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the transcript was read again")

    monkeypatch.setattr(projector, "fold_history_lines", no_read)
    second = index_session_history(store, files, project_id="scope", prices=overridden)
    request, session = _rows(store)
    assert second.files_read == 0
    assert request.cost_usd == Decimal(100) / _MILLION
    assert request.rate_table_version == OVERRIDE_RATE_VERSION
    assert session.cost_usd == Decimal(100) / _MILLION

    # An unchanged digest re-prices nothing.
    monkeypatch.setattr(projector, "reprice_request", no_read)
    index_session_history(store, files, project_id="scope", prices=overridden)
    store.close()


def test_an_override_takes_effect_from_its_date(tmp_path: Path) -> None:
    dated = _OVERRIDE.model_copy(update={"effective_from": date(2026, 11, 10)})
    book = load_price_book(repo_root=None, overrides={"opus": dated})

    assert book.quote("opus", TokenUsage(input=_MILLION), on=_ON).source == "override"
    assert book.quote("opus", TokenUsage(input=_MILLION), on=date(2026, 11, 9)).source is None
    assert book.list_rates("opus", on=_ON) == dated.rates
    # An override states no modifier, so a fast request is unpriced under it.
    assert book.quote("opus", TokenUsage(input=1, speed="fast"), on=_ON).cost_usd is None


@pytest.mark.parametrize(
    "body",
    [
        {"rates": _OVERRIDE.rates.model_dump(mode="json"), "note": "x", "price": 1},
        {"rates": _OVERRIDE.rates.model_dump(mode="json"), "note": ""},
        {
            "rates": {"input": 1, "output": 1, "cache_read": 1, "cache_write": {"2h": 1}},
            "note": "x",
        },
        {"rates": {"input": -1, "output": 1, "cache_read": 1, "cache_write": {}}, "note": "x"},
        {"note": "x"},
    ],
)
def test_a_malformed_override_is_refused(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PriceOverride.model_validate(body)


def test_a_malformed_override_in_the_global_layer_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_layer = tmp_path / "global.yaml"
    global_layer.write_text(
        yaml.safe_dump({"reflect": {"pricing": {"overrides": {"m": {"note": "no rates"}}}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(layered, "global_config_path", lambda: global_layer)

    with pytest.raises(ValidationError):
        price_overrides(None)
