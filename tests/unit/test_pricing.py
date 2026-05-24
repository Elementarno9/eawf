"""Unit tests for the telemetry pricing snapshot, drift check, and CLI verb.

Covers (C09 §5.9.6.1):

- :data:`PRICING` snapshot shape — Decimal per-token rates, the
  ``2026.05.17`` ``pricing_version`` literal, and the explicit cache
  multipliers stated by Anthropic (5m = 1.25x, 1h = 2x, read = 0.1x).
- :func:`lookup_pricing` exact match, longest-prefix fallback, and miss.
- ``extra="forbid"`` on the row models and ``ModelPricing``.
- :func:`check_pricing_currency` -> :class:`PricingDriftReport`.
- ``eawf telemetry pricing-currency-check`` text / JSON / ``--strict``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app
from eawf.telemetry.models import (
    EndMarker,
    RuntimeErrorClass,
    TelemetryRuntimeSwitch,
    TelemetrySession,
    ToolCallErrorKind,
)
from eawf.telemetry.pricing import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_1H_MULTIPLIER,
    CACHE_WRITE_5M_MULTIPLIER,
    PRICING,
    PRICING_FETCHED_AT,
    PRICING_VERSION,
    ModelPricing,
    PricingDriftReport,
    check_pricing_currency,
    lookup_pricing,
)

pytestmark = pytest.mark.unit

runner = CliRunner()


# --------------------------------------------------------------------------
# Snapshot shape: pricing_version, Decimal rates, cache multipliers.
# --------------------------------------------------------------------------


def test_pricing_version_literal() -> None:
    assert PRICING_VERSION == "2026.05.17"
    assert datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC) == PRICING_FETCHED_AT


def test_pricing_version_stamped_on_every_row() -> None:
    assert PRICING  # snapshot is non-empty
    for model_id, row in PRICING.items():
        assert row.pricing_version == PRICING_VERSION, model_id
        assert row.fetched_at == PRICING_FETCHED_AT, model_id


def test_pricing_rates_are_decimal_not_float() -> None:
    for model_id, row in PRICING.items():
        for field in (
            "input_per_token",
            "output_per_token",
            "cache_read_per_token",
            "cache_write_5m_per_token",
            "cache_write_1h_per_token",
        ):
            value = getattr(row, field)
            assert isinstance(value, Decimal), f"{model_id}.{field} is {type(value)}"


def test_opus_4_7_rates_match_2026_05_17_snapshot() -> None:
    row = PRICING["claude-opus-4-7"]
    assert row.input_per_token == Decimal("5e-6")
    assert row.output_per_token == Decimal("25e-6")
    assert row.cache_read_per_token == Decimal("0.5e-6")
    assert row.cache_write_5m_per_token == Decimal("6.25e-6")
    assert row.cache_write_1h_per_token == Decimal("10e-6")


def test_haiku_4_5_rates_match_2026_05_17_snapshot() -> None:
    row = PRICING["claude-haiku-4-5-20251001"]
    assert row.input_per_token == Decimal("1e-6")
    assert row.output_per_token == Decimal("5e-6")
    assert row.cache_read_per_token == Decimal("0.1e-6")


def test_cache_multipliers_explicit_and_consistent() -> None:
    # Anthropic-stated multipliers, encoded explicitly (not derived).
    assert Decimal("0.1") == CACHE_READ_MULTIPLIER
    assert Decimal("1.25") == CACHE_WRITE_5M_MULTIPLIER
    assert Decimal("2") == CACHE_WRITE_1H_MULTIPLIER
    for model_id, row in PRICING.items():
        assert row.cache_read_per_token == row.input_per_token * CACHE_READ_MULTIPLIER, model_id
        assert row.cache_write_5m_per_token == row.input_per_token * CACHE_WRITE_5M_MULTIPLIER, (
            model_id
        )
        assert row.cache_write_1h_per_token == row.input_per_token * CACHE_WRITE_1H_MULTIPLIER, (
            model_id
        )


# --------------------------------------------------------------------------
# lookup_pricing: exact, longest-prefix, miss.
# --------------------------------------------------------------------------


def test_lookup_pricing_exact_match() -> None:
    assert lookup_pricing("claude-opus-4-7") is PRICING["claude-opus-4-7"]


def test_lookup_pricing_longest_prefix_fallback() -> None:
    # Dated variant is not a key; falls back to the longest matching prefix.
    result = lookup_pricing("claude-opus-4-7-20260514")
    assert result is PRICING["claude-opus-4-7"]


def test_lookup_pricing_prefers_longest_prefix() -> None:
    # Both "claude-haiku-4-5" and "claude-haiku-4-5-20251001" are keys; a
    # dated id must resolve to the longer (more specific) prefix.
    result = lookup_pricing("claude-haiku-4-5-20251001-extra")
    assert result is PRICING["claude-haiku-4-5-20251001"]


def test_lookup_pricing_miss_returns_none() -> None:
    assert lookup_pricing("gpt-4o") is None


def test_lookup_pricing_empty_string_returns_none() -> None:
    assert lookup_pricing("") is None


# --------------------------------------------------------------------------
# extra="forbid" on the models.
# --------------------------------------------------------------------------


def test_model_pricing_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        ModelPricing(
            input_per_token=Decimal("5e-6"),
            output_per_token=Decimal("25e-6"),
            cache_read_per_token=Decimal("0.5e-6"),
            cache_write_5m_per_token=Decimal("6.25e-6"),
            cache_write_1h_per_token=Decimal("10e-6"),
            pricing_version=PRICING_VERSION,
            fetched_at=PRICING_FETCHED_AT,
            unexpected="boom",
        )


def test_model_pricing_is_frozen() -> None:
    row = PRICING["claude-opus-4-7"]
    with pytest.raises(ValidationError):
        row.input_per_token = Decimal("1e-6")


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


def test_telemetry_session_total_cost_usd_is_decimal() -> None:
    session = TelemetrySession(
        session_id="s1",
        project_id="p1",
        runtime="claude",
        wave_id="W11",
        attempt_id="a1",
        session_log_path="log",
        started_at=None,
        ended_at=None,
        duration_ms=None,
        model_primary="claude-opus-4-7",
        end_marker="clean_stop",
    )
    assert isinstance(session.total_cost_usd, Decimal)
    assert session.total_cost_usd == Decimal("0")


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


def test_check_pricing_currency_clean_snapshot() -> None:
    report = check_pricing_currency()
    assert isinstance(report, PricingDriftReport)
    assert report.is_current is True
    assert report.findings == []
    assert report.model_count == len(PRICING)
    assert report.pricing_version == PRICING_VERSION


def test_pricing_drift_report_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        PricingDriftReport(
            pricing_version=PRICING_VERSION,
            fetched_at=PRICING_FETCHED_AT,
            model_count=1,
            is_current=True,
            findings=[],
            extra="boom",
        )


def test_check_pricing_currency_detects_injected_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    drifted = ModelPricing(
        input_per_token=Decimal("5e-6"),
        output_per_token=Decimal("25e-6"),
        # cache read should be 0.5e-6 (0.1 x input); inject a stale value.
        cache_read_per_token=Decimal("1.5e-6"),
        cache_write_5m_per_token=Decimal("6.25e-6"),
        cache_write_1h_per_token=Decimal("10e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    )
    monkeypatch.setitem(PRICING, "claude-opus-4-7", drifted)
    report = check_pricing_currency()
    assert report.is_current is False
    assert any(
        f.model_id == "claude-opus-4-7" and f.field == "cache_read_per_token"
        for f in report.findings
    )


# --------------------------------------------------------------------------
# CLI: eawf telemetry pricing-currency-check.
# --------------------------------------------------------------------------


def test_cli_pricing_currency_check_text_ok() -> None:
    result = runner.invoke(app, ["telemetry", "pricing-currency-check"])
    assert result.exit_code == 0, result.output
    assert "CURRENT" in result.output
    assert PRICING_VERSION in result.output


def test_cli_pricing_currency_check_json_envelope() -> None:
    result = runner.invoke(app, ["--json", "telemetry", "pricing-currency-check"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["pricing_version"] == PRICING_VERSION
    assert payload["is_current"] is True
    assert payload["model_count"] == len(PRICING)
    assert payload["findings"] == []


def test_cli_pricing_currency_check_strict_exits_nonzero_on_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drifted = ModelPricing(
        input_per_token=Decimal("5e-6"),
        output_per_token=Decimal("25e-6"),
        cache_read_per_token=Decimal("9e-6"),
        cache_write_5m_per_token=Decimal("6.25e-6"),
        cache_write_1h_per_token=Decimal("10e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    )
    monkeypatch.setitem(PRICING, "claude-opus-4-7", drifted)
    result = runner.invoke(app, ["telemetry", "pricing-currency-check", "--strict"])
    assert result.exit_code == 2, result.output
    assert "DRIFT" in result.output


def test_cli_pricing_currency_check_drift_without_strict_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drifted = ModelPricing(
        input_per_token=Decimal("5e-6"),
        output_per_token=Decimal("25e-6"),
        cache_read_per_token=Decimal("9e-6"),
        cache_write_5m_per_token=Decimal("6.25e-6"),
        cache_write_1h_per_token=Decimal("10e-6"),
        pricing_version=PRICING_VERSION,
        fetched_at=PRICING_FETCHED_AT,
    )
    monkeypatch.setitem(PRICING, "claude-opus-4-7", drifted)
    result = runner.invoke(app, ["telemetry", "pricing-currency-check"])
    assert result.exit_code == 0, result.output
    assert "DRIFT" in result.output
