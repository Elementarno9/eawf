"""Telemetry source adapters — typed readers for the projection (C09 §5.9).

Each adapter implements the :class:`~eawf.telemetry.sources.base.SessionSource`
protocol: it discovers source files under a root and yields typed rows the
projector upserts into the metrics store. The package is additive — sibling
waves land per-runtime adapters (``codex_session``, ``opencode_session``)
that import :class:`SessionSource` from :mod:`eawf.telemetry.sources.base`.

This wave (P27-I01-W13) lands:

- :class:`~eawf.telemetry.sources.base.SessionSource` — the shared protocol.
- :class:`~eawf.telemetry.sources.event_jsonl.EventJsonlSource` — reader for
  the canonical eawf JSONL stores (``event.jsonl`` + ``audit.jsonl`` +
  per-role ``<role>_report.jsonl``) that skips corrupt lines with a logged
  warning (C09 §6 F3).
- :class:`~eawf.telemetry.sources.claude_session.ClaudeSessionSource` — reader
  for Claude Code transcript logs, projected into
  :class:`~eawf.telemetry.models.TelemetrySession` rows.
"""

from __future__ import annotations

from eawf.telemetry.sources.base import SessionSource
from eawf.telemetry.sources.claude_session import ClaudeSessionSource
from eawf.telemetry.sources.event_jsonl import EventJsonlSource

__all__ = [
    "ClaudeSessionSource",
    "EventJsonlSource",
    "SessionSource",
]
