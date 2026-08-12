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

from eawf.observability.telemetry.models import (
    EndMarker,
    RuntimeErrorClass,
    TelemetryRuntimeSwitch,
    TelemetrySession,
    ToolCallErrorKind,
)
from eawf.observability.telemetry.pricing import (
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
from eawf.surfaces.cli.app import app

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
# claude-opus-4-8 row + bare-alias resolution + codex row.
# --------------------------------------------------------------------------


def test_opus_4_8_row_present_and_priced() -> None:
    row = PRICING["claude-opus-4-8"]
    assert row.input_per_token == Decimal("5e-6")
    assert row.output_per_token == Decimal("25e-6")
    assert row.cache_read_per_token == Decimal("0.5e-6")
    assert row.cache_write_5m_per_token == Decimal("6.25e-6")
    assert row.cache_write_1h_per_token == Decimal("10e-6")


def test_opus_4_8_row_mirrors_opus_4_7() -> None:
    new = PRICING["claude-opus-4-8"]
    prior = PRICING["claude-opus-4-7"]
    for field in (
        "input_per_token",
        "output_per_token",
        "cache_read_per_token",
        "cache_write_5m_per_token",
        "cache_write_1h_per_token",
    ):
        assert getattr(new, field) == getattr(prior, field), field


def test_lookup_pricing_opus_4_8_dated_variant_resolves() -> None:
    # A dated/bracketed runtime id falls back to the longest matching prefix.
    result = lookup_pricing("claude-opus-4-8-20260101")
    assert result is PRICING["claude-opus-4-8"]


def test_lookup_pricing_opus_4_8_bracket_variant_resolves() -> None:
    # The live runtime id form (e.g. "claude-opus-4-8[1m]") prefix-matches.
    result = lookup_pricing("claude-opus-4-8[1m]")
    assert result is PRICING["claude-opus-4-8"]


@pytest.mark.parametrize(
    ("alias", "expected_input"),
    [
        ("opus", Decimal("5e-6")),
        ("claude-opus", Decimal("5e-6")),
        ("sonnet", Decimal("3e-6")),
        ("claude-sonnet", Decimal("3e-6")),
        ("haiku", Decimal("1e-6")),
        ("claude-haiku", Decimal("1e-6")),
        ("codex", Decimal("5e-6")),
    ],
)
def test_lookup_pricing_bare_alias_resolves_to_priced_row(
    alias: str, expected_input: Decimal
) -> None:
    # Bare aliases used on the dispatch / role-spec surface (and short-form
    # runtime logs) must price to a real row, not fall through unpriced.
    row = lookup_pricing(alias)
    assert row is not None, alias
    assert row.input_per_token == expected_input


def test_lookup_pricing_bare_alias_does_not_shadow_dated_row() -> None:
    # The "claude-opus" alias is a prefix of "claude-opus-4-8"; the resolver
    # must still bind the longer, more specific dated row.
    assert lookup_pricing("claude-opus-4-8") is PRICING["claude-opus-4-8"]
    assert lookup_pricing("claude-opus-4-7-20260514") is PRICING["claude-opus-4-7"]


def test_codex_row_present_and_priced() -> None:
    # Codex is net-new (placeholder rate pending operator confirmation); it
    # must exist and price non-zero so codex sessions are not silently $0.
    row = PRICING["codex"]
    assert row.input_per_token > Decimal("0")
    assert row.output_per_token > Decimal("0")


def test_codex_row_currency_invariant_holds() -> None:
    row = PRICING["codex"]
    assert row.cache_read_per_token == row.input_per_token * CACHE_READ_MULTIPLIER
    assert row.cache_write_5m_per_token == row.input_per_token * CACHE_WRITE_5M_MULTIPLIER
    assert row.cache_write_1h_per_token == row.input_per_token * CACHE_WRITE_1H_MULTIPLIER


def test_lookup_pricing_codex_dated_variant_resolves() -> None:
    # A model-specific codex id (e.g. "codex-mini-latest") prefix-matches the
    # placeholder codex row rather than returning None.
    result = lookup_pricing("codex-mini-latest")
    assert result is PRICING["codex"]


def test_lookup_pricing_opencode_still_unpriced() -> None:
    # The bare runtime id "opencode" is NOT a model id (opencode addresses
    # models in provider/model form); no key is a prefix of it, so it stays
    # unpriced (None), confirming the new keys did not widen the match set
    # wrongly. The opencode MODEL ids ("anthropic/...") are priced separately.
    assert lookup_pricing("opencode") is None


# --------------------------------------------------------------------------
# Cross-vendor per-tier model rows: codex bare ids + opencode
# provider/model ids the dispatch routing table emits must all price.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("model_id", ["gpt-5-mini", "gpt-5", "gpt-5-codex"])
def test_codex_tier_rows_present_and_priced(model_id: str) -> None:
    # Each codex per-tier id the routing table emits prices non-zero
    # (placeholder rate pending operator confirmation) -- a codex juror spawn
    # must not silently bill $0.
    row = PRICING[model_id]
    assert row.input_per_token > Decimal("0")
    assert row.output_per_token > Decimal("0")


@pytest.mark.parametrize(
    "model_id",
    [
        "anthropic/claude-haiku-4-5",
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-opus-4-8",
    ],
)
def test_opencode_provider_model_rows_present_and_priced(model_id: str) -> None:
    # The opencode provider/model ids price at the REAL anthropic rates (the
    # OAuth-Claude lane), so they mirror the bare claude row's input rate.
    row = PRICING[model_id]
    bare = model_id.removeprefix("anthropic/")
    assert row.input_per_token == PRICING[bare].input_per_token
    assert row.output_per_token == PRICING[bare].output_per_token


def test_lookup_pricing_gpt5_codex_prefers_longest_prefix_over_gpt5() -> None:
    # Both "gpt-5" and "gpt-5-codex" are keys; the codex id must bind the
    # longer, more specific row rather than the gpt-5 mid-tier row.
    assert lookup_pricing("gpt-5-codex") is PRICING["gpt-5-codex"]
    # A suffixed codex id still binds the codex row, not gpt-5.
    assert lookup_pricing("gpt-5-codex-preview") is PRICING["gpt-5-codex"]


def test_lookup_pricing_gpt5_dated_variant_resolves() -> None:
    # A dated/suffixed gpt-5 id with no exact row prices via the gpt-5 prefix
    # row (and NOT gpt-5-codex, which is not a prefix of "gpt-5-2026").
    assert lookup_pricing("gpt-5-2026") is PRICING["gpt-5"]


def test_lookup_pricing_opencode_model_dated_variant_resolves() -> None:
    # A dated opencode provider/model id longest-prefix-matches its tier row.
    result = lookup_pricing("anthropic/claude-opus-4-8-20260101")
    assert result is PRICING["anthropic/claude-opus-4-8"]


def test_lookup_pricing_gpt4o_still_miss() -> None:
    # The new gpt-5* keys must not widen the match set to an unrelated OpenAI
    # id: "gpt-5" is not a prefix of "gpt-4o", so it stays a miss.
    assert lookup_pricing("gpt-4o") is None


def test_cross_vendor_rows_keep_currency_check_green() -> None:
    # The W15 codex + opencode additions must not introduce drift.
    report = check_pricing_currency()
    assert report.is_current is True
    assert report.findings == []


def test_new_rows_keep_currency_check_green() -> None:
    # The W20 additions must not introduce drift in the snapshot.
    report = check_pricing_currency()
    assert report.is_current is True
    assert report.findings == []


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
