"""OpenTelemetry span projection for the local telemetry ledger.

The compatibility surface here is intentionally narrow: one stable
``client`` span per projected :class:`TelemetrySession`, with attributes
derived only from typed ledger columns. Experimental agent-internal spans
are not emitted by the stable helpers and are tagged with a separate
stability marker when built explicitly.

OTLP export is opt-in and best-effort. The OpenTelemetry SDK/exporter is
imported lazily only when enabled; missing packages or exporter failures are
logged and reported instead of failing telemetry rebuild/export paths.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.observability.telemetry.models import TelemetrySession
from eawf.observability.telemetry.pricing import PRICING_VERSION
from eawf.observability.telemetry.store.base import AbstractMetricsStore

logger = logging.getLogger(__name__)

__all__ = [
    "CLIENT_SPAN_SCHEMA_VERSION",
    "OTelAttribute",
    "OTelClientSpan",
    "OTelExportConfig",
    "OTelExportReport",
    "build_client_span",
    "build_client_spans",
    "build_client_spans_from_store",
    "emit_client_spans",
    "export_client_spans",
]

CLIENT_SPAN_SCHEMA_VERSION: Literal[1] = 1
"""Stable client-span attribute subset version."""

_CLIENT_SPAN_NAME = "eawf.telemetry.client.session"
_EXPERIMENTAL_AGENT_SPAN_NAME_PREFIX = "experimental.eawf.telemetry.agent."

OTelAttributeValue = str | int | float | bool


class OTelAttribute(BaseModel):
    """One stable OpenTelemetry attribute key/value pair."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    value: OTelAttributeValue


class OTelClientSpan(BaseModel):
    """Stable client span derived from one telemetry session ledger row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    start_time: datetime | None
    end_time: datetime | None
    attributes: tuple[OTelAttribute, ...] = Field(default_factory=tuple)
    stability: Literal["stable", "experimental"] = "stable"
    schema_version: Literal[1] = CLIENT_SPAN_SCHEMA_VERSION

    def attribute_mapping(self) -> dict[str, OTelAttributeValue]:
        """Return span attributes as a plain mapping for SDK export."""
        return {attribute.key: attribute.value for attribute in self.attributes}


class OTelExportConfig(BaseModel):
    """Opt-in OTLP export settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    endpoint: str | None = None
    headers: tuple[tuple[str, str], ...] = Field(default_factory=tuple)
    timeout_seconds: float = Field(default=2.0, gt=0)
    service_name: str = "eawf"


class OTelExportReport(BaseModel):
    """Result of an OTLP export attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    attempted: bool
    exported: int
    skipped: int
    error_class: str | None = None
    error_message: str | None = None


def build_client_span(session: TelemetrySession) -> OTelClientSpan:
    """Build the stable client span for one ledger session row.

    The subset is intentionally small and PII-light: it excludes local file
    paths such as ``session_log_path`` and does not include experimental
    agent-internal attributes.
    """
    attributes = _stable_session_attributes(session)
    return OTelClientSpan(
        name=_CLIENT_SPAN_NAME,
        start_time=session.started_at,
        end_time=session.ended_at,
        attributes=tuple(OTelAttribute(key=key, value=value) for key, value in attributes.items()),
    )


def build_client_spans(sessions: Iterable[TelemetrySession]) -> tuple[OTelClientSpan, ...]:
    """Build stable client spans for the provided telemetry sessions."""
    spans = tuple(build_client_span(session) for session in sessions)
    logger.info(f"build_client_spans spans={len(spans)}")
    return spans


def build_client_spans_from_store(store: AbstractMetricsStore) -> tuple[OTelClientSpan, ...]:
    """Read the telemetry ledger and build its stable client-span subset."""
    rows = [
        row
        for row in store.fetch_all("telemetry_sessions", TelemetrySession)
        if isinstance(row, TelemetrySession)
    ]
    return build_client_spans(rows)


def emit_client_spans(
    store: AbstractMetricsStore,
    *,
    config: OTelExportConfig | None = None,
) -> tuple[tuple[OTelClientSpan, ...], OTelExportReport]:
    """Build stable client spans from *store* and optionally export them."""
    spans = build_client_spans_from_store(store)
    report = export_client_spans(spans, config=config)
    return spans, report


def export_client_spans(
    spans: Iterable[OTelClientSpan],
    *,
    config: OTelExportConfig | None = None,
) -> OTelExportReport:
    """Best-effort OTLP export for stable client spans.

    Args:
        spans: Stable client spans to export.
        config: Export configuration. ``enabled=False`` skips all work and
            avoids importing OpenTelemetry packages.

    Returns:
        A report describing whether export was skipped, succeeded, or failed
        best-effort.
    """
    cfg = config or OTelExportConfig()
    span_tuple = tuple(spans)
    if not cfg.enabled:
        return OTelExportReport(
            enabled=False,
            attempted=False,
            exported=0,
            skipped=len(span_tuple),
        )
    try:
        _export_client_spans_via_sdk(span_tuple, cfg)
    except Exception as exc:  # pragma: no cover - exact SDK failures vary.
        logger.warning(
            f"export_client_spans attempted=True exported=0 skipped={len(span_tuple)} "
            f"error={type(exc).__name__!r}"
        )
        return OTelExportReport(
            enabled=True,
            attempted=True,
            exported=0,
            skipped=len(span_tuple),
            error_class=type(exc).__name__,
            error_message=str(exc),
        )
    logger.info(f"export_client_spans attempted=True exported={len(span_tuple)} skipped=0")
    return OTelExportReport(
        enabled=True,
        attempted=True,
        exported=len(span_tuple),
        skipped=0,
    )


def _stable_session_attributes(session: TelemetrySession) -> dict[str, OTelAttributeValue]:
    """Return deterministic stable attributes for a session client span."""
    attrs: dict[str, OTelAttributeValue] = {
        "eawf.telemetry.client_span.schema_version": CLIENT_SPAN_SCHEMA_VERSION,
        "eawf.telemetry.span.stability": "stable",
        "eawf.telemetry.pricing.version": PRICING_VERSION,
        "eawf.session.id": session.session_id,
        "eawf.project.id": session.project_id,
        "eawf.runtime.name": session.runtime,
        "eawf.session.end_marker": session.end_marker,
        "eawf.tokens.input": session.total_input_tokens,
        "eawf.tokens.output": session.total_output_tokens,
        "eawf.tokens.cache_read": session.total_cache_read,
        "eawf.tokens.cache_create": session.total_cache_write,
        "eawf.cost.usd": _decimal_attr(session.total_cost_usd),
        "eawf.turns.count": session.turn_count,
        "eawf.tool_calls.count": session.tool_call_count,
        "eawf.errors.count": session.error_count,
        "eawf.denials.count": session.denial_count,
        "eawf.interrupts.count": session.interrupt_count,
        "eawf.compactions.count": session.compaction_count,
        "eawf.subagent_dispatches.count": session.subagent_dispatch_count,
    }
    _set_optional(attrs, "eawf.wave.id", session.wave_id)
    _set_optional(attrs, "eawf.attempt.id", session.attempt_id)
    _set_optional(attrs, "gen_ai.request.model", session.model_primary)
    _set_optional(attrs, "eawf.session.duration_ms", session.duration_ms)
    return dict(sorted(attrs.items(), key=lambda item: item[0]))


def _decimal_attr(value: Decimal) -> str:
    """Render Decimal attributes losslessly for OTel string transport."""
    return str(value)


def _set_optional(
    attrs: dict[str, OTelAttributeValue],
    key: str,
    value: OTelAttributeValue | None,
) -> None:
    """Set an attribute only when the ledger value is present."""
    if value is not None:
        attrs[key] = value


def _export_client_spans_via_sdk(
    spans: tuple[OTelClientSpan, ...],
    config: OTelExportConfig,
) -> None:
    """Export spans through OpenTelemetry SDK modules imported lazily."""
    resources = importlib.import_module("opentelemetry.sdk.resources")
    trace_sdk = importlib.import_module("opentelemetry.sdk.trace")
    trace_export = importlib.import_module("opentelemetry.sdk.trace.export")
    otlp_export = importlib.import_module("opentelemetry.exporter.otlp.proto.grpc.trace_exporter")

    resource = resources.Resource.create({"service.name": config.service_name})
    provider = trace_sdk.TracerProvider(resource=resource)
    exporter_kwargs = _exporter_kwargs(config)
    exporter = otlp_export.OTLPSpanExporter(**exporter_kwargs)
    processor = trace_export.BatchSpanProcessor(exporter)
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("eawf.telemetry", str(CLIENT_SPAN_SCHEMA_VERSION))

    for span in spans:
        sdk_span = tracer.start_span(
            span.name,
            attributes=span.attribute_mapping(),
            start_time=_dt_to_ns(span.start_time),
        )
        sdk_span.end(end_time=_dt_to_ns(span.end_time))
    provider.force_flush(timeout_millis=int(config.timeout_seconds * 1000))
    provider.shutdown()


def _exporter_kwargs(config: OTelExportConfig) -> dict[str, Any]:
    """Return constructor kwargs accepted by the OTLP gRPC exporter."""
    kwargs: dict[str, Any] = {"timeout": config.timeout_seconds}
    if config.endpoint:
        kwargs["endpoint"] = config.endpoint
    if config.headers:
        kwargs["headers"] = dict(config.headers)
    return kwargs


def _dt_to_ns(value: datetime | None) -> int | None:
    """Convert a datetime to OpenTelemetry nanoseconds since epoch."""
    if value is None:
        return None
    return int(value.timestamp() * 1_000_000_000)


def _build_experimental_agent_span(
    *,
    name_suffix: str,
    attributes: Mapping[str, OTelAttributeValue],
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> OTelClientSpan:
    """Build an explicitly experimental agent span outside the compat surface."""
    attrs = {
        "eawf.telemetry.client_span.schema_version": CLIENT_SPAN_SCHEMA_VERSION,
        "eawf.telemetry.span.stability": "experimental",
        **attributes,
    }
    return OTelClientSpan(
        name=f"{_EXPERIMENTAL_AGENT_SPAN_NAME_PREFIX}{name_suffix}",
        start_time=started_at,
        end_time=ended_at,
        attributes=tuple(
            OTelAttribute(key=key, value=value) for key, value in sorted(attrs.items())
        ),
        stability="experimental",
    )
