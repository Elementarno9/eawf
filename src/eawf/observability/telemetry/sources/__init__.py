"""Telemetry source adapters — typed readers for the projection (C09 §5.9).

Each adapter implements the :class:`~eawf.observability.telemetry.sources.base.SessionSource`
protocol: it discovers source files under a root and yields typed rows the
projector upserts into the metrics store. The package is additive — sibling
waves land per-runtime adapters (``codex_session``, ``opencode_session``)
that import :class:`SessionSource` from :mod:`eawf.observability.telemetry.sources.base`.

This wave lands:

- :class:`~eawf.observability.telemetry.sources.base.SessionSource` — the shared protocol.
- :class:`~eawf.observability.telemetry.sources.event_jsonl.EventJsonlSource` — reader for
  the canonical eawf JSONL stores (``event.jsonl`` + ``audit.jsonl`` +
  per-role ``<role>_report.jsonl``) that skips corrupt lines with a logged
  warning (C09 §6 F3).
- :class:`~eawf.observability.telemetry.sources.claude_session.ClaudeSessionSource` — reader
  for Claude Code transcript logs, projected into
  :class:`~eawf.observability.telemetry.models.TelemetrySession` rows.
"""

from __future__ import annotations

from eawf.observability.telemetry.sources.base import SessionSource
from eawf.observability.telemetry.sources.claude_session import ClaudeSessionSource
from eawf.observability.telemetry.sources.codex_session import CodexSessionSource
from eawf.observability.telemetry.sources.dispatch_cost import DispatchCostSessionSource
from eawf.observability.telemetry.sources.event_jsonl import EventJsonlSource
from eawf.observability.telemetry.sources.opencode_session import OpenCodeSessionSource

__all__ = [
    "ClaudeSessionSource",
    "CodexSessionSource",
    "DispatchCostSessionSource",
    "EventJsonlSource",
    "OpenCodeSessionSource",
    "SessionSource",
]
