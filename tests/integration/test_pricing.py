"""Telemetry price files, drift check and CLI verb.

Covers:

- The shipped price files: Decimal rates, the Anthropic-stated cache-write
  multipliers, exact lookup and the ids the dispatch routing table emits.
- ``extra="forbid"`` on the telemetry row models.
- :func:`check_pricing_currency` -> :class:`PricingDriftReport`.
- ``eawf telemetry pricing-currency-check`` text / JSON / ``--strict``.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

import eawf.observability.telemetry.pricing as pricing
from eawf.observability.telemetry.models import (
    EndMarker,
    RuntimeErrorClass,
    TelemetryRuntimeSwitch,
    TelemetrySession,
    ToolCallErrorKind,
)
from eawf.observability.telemetry.pricing import (
    CACHE_WRITE_1H_MULTIPLIER,
    CACHE_WRITE_5M_MULTIPLIER,
    PriceBook,
    PricingDriftReport,
    TokenUsage,
    check_pricing_currency,
    load_price_book,
)
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.unit

runner = CliRunner()

_ON = date(2026, 11, 10)
_BOOK = load_price_book(repo_root=None, overrides={})


# --------------------------------------------------------------------------
# Shipped rates: Decimal, cache-write multipliers, exact lookup.
# --------------------------------------------------------------------------


def test_every_priced_rate_is_decimal_not_float() -> None:
    for row in _BOOK.rows:
        if row.rates is None:
            continue
        for value in (row.rates.input, row.rates.output, row.rates.cache_read):
            assert isinstance(value, Decimal), row.id


def test_cache_write_multipliers_explicit_and_held_by_every_anthropic_row() -> None:
    assert Decimal("1.25") == CACHE_WRITE_5M_MULTIPLIER
    assert Decimal("2") == CACHE_WRITE_1H_MULTIPLIER
    anthropic = next(file for file in _BOOK.files if file.vendor == "anthropic")
    for row in anthropic.models:
        assert row.rates is not None and row.rates.input is not None, row.id
        assert row.rates.cache_write["5m"] == row.rates.input * CACHE_WRITE_5M_MULTIPLIER, row.id
        assert row.rates.cache_write["1h"] == row.rates.input * CACHE_WRITE_1H_MULTIPLIER, row.id


@pytest.mark.parametrize(
    ("model", "row_id"),
    [
        ("claude-opus-4-7", "claude-opus-4-7"),
        ("claude-opus-4-8[1m]", "claude-opus-4-8"),
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
        ("anthropic/claude-haiku-4-5", "claude-haiku-4-5"),
        ("anthropic/claude-sonnet-4-6", "claude-sonnet-4-6"),
        ("anthropic/claude-opus-4-8", "claude-opus-4-8"),
    ],
)
def test_listed_ids_resolve_to_their_row(model: str, row_id: str) -> None:
    row = _BOOK.resolve(model, on=_ON)

    assert row is not None and row.id == row_id


@pytest.mark.parametrize(
    "model",
    [
        "",
        "gpt-4o",
        "opencode",
        "codex",
        "codex-mini-latest",
        "gpt-5-codex",
        "gpt-5-2026",
        "claude-opus",
        "claude-opus-4-7-20260514",
        "anthropic/claude-opus-4-8-20260101",
    ],
)
def test_an_id_no_row_lists_is_unpriced(model: str) -> None:
    quote = _BOOK.quote(model, TokenUsage(input=10), on=_ON)

    assert _BOOK.resolve(model, on=_ON) is None
    assert quote.cost_usd is None and quote.unpriced_tokens == 10


# --------------------------------------------------------------------------
# extra="forbid" on the models.
# --------------------------------------------------------------------------


def test_telemetry_session_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        TelemetrySession(
            session_id="s1",
            project_id="p1",
            runtime="claude",
            wave_id=None,
            attempt_id=None,
            session_log_path="/tmp/log",
            started_at=None,
            ended_at=None,
            duration_ms=None,
            model_primary=None,
            end_marker="clean_stop",
            bogus_field=1,
        )


def test_telemetry_session_total_cost_usd_is_decimal_or_unpriced() -> None:
    fields: dict[str, object] = {
        "session_id": "s1",
        "project_id": "p1",
        "runtime": "claude",
        "wave_id": "W11",
        "attempt_id": "a1",
        "session_log_path": "log",
        "started_at": None,
        "ended_at": None,
        "duration_ms": None,
        "model_primary": "claude-opus-4-7",
        "end_marker": "clean_stop",
    }
    # A row nothing priced records no cost rather than a zero that reads as free.
    assert TelemetrySession.model_validate(fields).total_cost_usd is None
    priced = TelemetrySession.model_validate({**fields, "total_cost_usd": "0.017"})
    assert isinstance(priced.total_cost_usd, Decimal)
    assert priced.total_cost_usd == Decimal("0.017")


def test_telemetry_session_rejects_unknown_runtime() -> None:
    with pytest.raises(ValidationError):
        TelemetrySession(
            session_id="s1",
            project_id="p1",
            runtime="gemini",
            wave_id=None,
            attempt_id=None,
            session_log_path="log",
            started_at=None,
            ended_at=None,
            duration_ms=None,
            model_primary=None,
            end_marker="clean_stop",
        )


# --------------------------------------------------------------------------
# New closed enums.
# --------------------------------------------------------------------------


def test_runtime_error_class_members() -> None:
    assert {member.value for member in RuntimeErrorClass} == {
        "RUNTIME_RATE_LIMIT",
        "RUNTIME_SERVER_ERROR",
        "RUNTIME_TIMEOUT",
        "RUNTIME_API_ERROR",
        "RUNTIME_AUTH_ERROR",
    }


def test_tool_call_error_kind_members() -> None:
    assert "timeout" in {member.value for member in ToolCallErrorKind}
    assert "unknown" in {member.value for member in ToolCallErrorKind}


def test_end_marker_includes_runtime_switched_extension() -> None:
    # ``EndMarker`` is a Literal alias; its args carry the allowed strings.
    from typing import get_args

    members = set(get_args(EndMarker))
    assert "runtime_switched" in members
    assert "clean_stop" in members


def test_runtime_switch_row_uses_typed_cause_enum() -> None:
    row = TelemetryRuntimeSwitch(
        wave_id="W11",
        attempt_id_from="a1",
        attempt_id_to="a2",
        runtime_from="claude",
        runtime_to="codex",
        cause=RuntimeErrorClass.RUNTIME_RATE_LIMIT,
        ts=datetime(2026, 5, 17, tzinfo=UTC),
    )
    assert row.cause is RuntimeErrorClass.RUNTIME_RATE_LIMIT
    with pytest.raises(ValidationError):
        TelemetryRuntimeSwitch(
            wave_id="W11",
            attempt_id_from="a1",
            attempt_id_to="a2",
            runtime_from="claude",
            runtime_to="codex",
            cause="not-a-class",
            ts=datetime(2026, 5, 17, tzinfo=UTC),
        )


# --------------------------------------------------------------------------
# PricingDriftReport + check_pricing_currency.
# --------------------------------------------------------------------------


def _drifted_book(tmp_path: Path, write_5m: float = 9) -> PriceBook:
    body: dict[str, Any] = {
        "vendor": "anthropic",
        "aliases": [],
        "models": [
            {
                "id": "claude-opus-4-7",
                "match": ["claude-opus-4-7"],
                "effective_from": None,
                "effective_to": None,
                "source_url": "https://example.invalid/pricing",
                "verified_on": "2026-11-10",
                "verified_by": "fetch",
                "rates": {
                    "input": 5,
                    "output": 25,
                    "cache_read": 0.5,
                    "cache_write": {"5m": write_5m, "1h": 10},
                },
                "tiers": [],
                "modifiers": {"fast": None, "geo_us": 1.1},
            }
        ],
    }
    (tmp_path / "anthropic.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    return load_price_book(repo_root=None, directory=tmp_path, overrides={})


def test_check_pricing_currency_clean_files() -> None:
    report = check_pricing_currency()
    assert isinstance(report, PricingDriftReport)
    assert report.is_current is True
    assert report.findings == []
    assert report.model_count == len(_BOOK.rows)
    assert report.pricing_version == _BOOK.version
    assert report.fetched_at == datetime(2026, 11, 10, tzinfo=UTC)


def test_pricing_drift_report_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        PricingDriftReport(
            pricing_version=_BOOK.version,
            fetched_at=datetime(2026, 11, 10, tzinfo=UTC),
            model_count=1,
            is_current=True,
            findings=[],
            extra="boom",
        )


def test_check_pricing_currency_detects_a_drifted_write_rate(tmp_path: Path) -> None:
    report = check_pricing_currency(_drifted_book(tmp_path))

    assert report.is_current is False
    assert [(f.model_id, f.field, f.actual) for f in report.findings] == [
        ("claude-opus-4-7", "cache_write.5m", Decimal(9))
    ]


def test_check_pricing_currency_on_a_consistent_fixture_is_current(tmp_path: Path) -> None:
    assert check_pricing_currency(_drifted_book(tmp_path, write_5m=6.25)).is_current


# --------------------------------------------------------------------------
# CLI: eawf telemetry pricing-currency-check.
# --------------------------------------------------------------------------


def _serve(monkeypatch: pytest.MonkeyPatch, book: PriceBook) -> None:
    monkeypatch.setattr(pricing, "load_price_book", lambda **_kwargs: book)


def test_cli_pricing_currency_check_text_ok() -> None:
    result = runner.invoke(app, ["telemetry", "pricing-currency-check"])
    assert result.exit_code == 0, result.output
    assert "CURRENT" in result.output
    assert _BOOK.version in result.output


def test_cli_pricing_currency_check_json_envelope() -> None:
    result = runner.invoke(app, ["--json", "telemetry", "pricing-currency-check"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["pricing_version"] == _BOOK.version
    assert payload["is_current"] is True
    assert payload["model_count"] == len(_BOOK.rows)
    assert payload["findings"] == []


def test_cli_pricing_currency_check_strict_exits_nonzero_on_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _serve(monkeypatch, _drifted_book(tmp_path))
    result = runner.invoke(app, ["telemetry", "pricing-currency-check", "--strict"])
    assert result.exit_code == 2, result.output
    assert "DRIFT" in result.output


def test_cli_pricing_currency_check_drift_without_strict_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _serve(monkeypatch, _drifted_book(tmp_path))
    result = runner.invoke(app, ["telemetry", "pricing-currency-check"])
    assert result.exit_code == 0, result.output
    assert "DRIFT" in result.output
