"""Tests for stable telemetry OTel client-span projection."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.observability.telemetry.models import TelemetrySession
from eawf.observability.telemetry.otel import (
    CLIENT_SPAN_SCHEMA_VERSION,
    OTelExportConfig,
    build_client_span,
    emit_client_spans,
    export_client_spans,
)
from eawf.observability.telemetry.store import SqliteMetricsStore

pytestmark = pytest.mark.unit

_TS = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


def _session() -> TelemetrySession:
    return TelemetrySession(
        session_id="sess-otel",
        project_id="proj123",
        runtime="claude",
        wave_id="P28-I03-W15",
        attempt_id="attempt-1",
        session_log_path="opaque://session/sess-otel",
        started_at=_TS,
        ended_at=_TS,
        duration_ms=1234,
        model_primary="claude-opus-4-7",
        total_input_tokens=100,
        total_output_tokens=20,
        total_cache_read=30,
        total_cache_write=40,
        total_cost_usd=Decimal("0.00123"),
        turn_count=2,
        tool_call_count=3,
        error_count=1,
        denial_count=0,
        interrupt_count=0,
        compaction_count=1,
        subagent_dispatch_count=1,
        end_marker="clean_stop",
    )


def test_build_client_span_emits_stable_subset() -> None:
    span = build_client_span(_session())
    attrs = span.attribute_mapping()

    assert span.name == "eawf.telemetry.client.session"
    assert span.stability == "stable"
    assert span.schema_version == CLIENT_SPAN_SCHEMA_VERSION
    assert attrs["eawf.telemetry.span.stability"] == "stable"
    assert attrs["eawf.session.id"] == "sess-otel"
    assert attrs["eawf.wave.id"] == "P28-I03-W15"
    assert attrs["gen_ai.request.model"] == "claude-opus-4-7"
    assert attrs["eawf.cost.usd"] == "0.00123"


def test_build_client_span_excludes_paths_and_agent_experiment() -> None:
    span = build_client_span(_session())
    attrs = span.attribute_mapping()

    assert "session_log_path" not in attrs
    assert not any("path" in key for key in attrs)
    assert not any(key.startswith("experimental.") for key in attrs)
    assert not span.name.startswith("experimental.")


def test_build_client_span_attribute_order_is_stable() -> None:
    span = build_client_span(_session())
    keys = [attribute.key for attribute in span.attributes]
    assert keys == sorted(keys)


def test_emit_client_spans_reads_ledger_and_skips_disabled_export(tmp_path: Path) -> None:
    store = SqliteMetricsStore(tmp_path / "telemetry.db")
    store.init_schema()
    store.upsert("telemetry_sessions", _session())
    store.commit()

    spans, report = emit_client_spans(store)
    store.close()

    assert len(spans) == 1
    assert spans[0].attribute_mapping()["eawf.session.id"] == "sess-otel"
    assert report.enabled is False
    assert report.attempted is False
    assert report.exported == 0
    assert report.skipped == 1


def test_otlp_export_enabled_missing_sdk_is_best_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing_sdk(name: str) -> object:
        raise ImportError(f"missing {name}")

    monkeypatch.setattr("eawf.observability.telemetry.otel.importlib.import_module", _missing_sdk)

    report = export_client_spans((_session_span(),), config=OTelExportConfig(enabled=True))

    assert report.enabled is True
    assert report.attempted is True
    assert report.exported == 0
    assert report.skipped == 1
    assert report.error_class == "ImportError"


def test_export_config_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        OTelExportConfig(enabled=False, bogus=True)


def _session_span():
    """Return one stable client span for export tests."""
    return build_client_span(_session())
