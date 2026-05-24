"""Pydantic v2 telemetry row models (vendored shape, retyped).

These rows are the typed projection target for the observability subsystem
(C09 §5.9.2). Each model is retyped from the upstream agent-lens dataclasses
(see :mod:`eawf.telemetry`'s ``_AGENT_LENS_AUDIT_COMMIT.txt`` for upstream
provenance) into a strict Pydantic v2 ``BaseModel`` with
``ConfigDict(extra="forbid")`` so the projection rejects unknown columns at
the boundary instead of silently dropping data.

Closed enums new to eawf (not present in the agent-lens upstream) live here
alongside the rows that consume them:

- :data:`EndMarker` — session end-state classifier; the ``runtime_switched``
  member is the V5 extension.
- :class:`ToolCallErrorKind` — closed taxonomy for tool-call failures,
  retyped from the upstream free-string ``error_kind``.
- :class:`RuntimeErrorClass` — 5-class runtime-fallback cause enum (C07a
  §5.5). :class:`~eawf.kernel.store.kinds.events.runtime_switched.RuntimeSwitchedPayload`
  carries ``cause`` as a typed member of this enum.

:class:`~eawf.kernel.state.enums.IncidentSeverity` and
:class:`~eawf.kernel.state.enums.IncidentCause` already live in
:mod:`eawf.kernel.state.enums` (the canonical state-enum module) and are imported
here so the incident row shares one taxonomy with the rest of the state
subsystem.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import IncidentCause, IncidentSeverity

__all__ = [
    "EndMarker",
    "RuntimeErrorClass",
    "TelemetryCompaction",
    "TelemetryFileMeta",
    "TelemetryIncident",
    "TelemetryProject",
    "TelemetryRuntimeSwitch",
    "TelemetrySchemaMeta",
    "TelemetrySession",
    "TelemetryToolCall",
    "TelemetryTurn",
    "ToolCallErrorKind",
]


RuntimeName = Literal["claude", "codex", "opencode"]


EndMarker = Literal[
    "clean_stop",
    "away",
    "pr_link",
    "last_assistant_inflight",
    "last_user_typed",
    "permission_change_at_end",
    "runtime_switched",
    "other",
]
"""Session end-state classifier.

``runtime_switched`` is the V5 extension (eawf-specific); the remaining
members are vendored from the agent-lens upstream.
"""


class ToolCallErrorKind(StrEnum):
    """Closed taxonomy for tool-call failures (retyped from free str)."""

    TIMEOUT = "timeout"
    PERMISSION_DENIED = "permission_denied"
    RUNTIME_OOM = "runtime_oom"
    NETWORK_ERROR = "network_error"
    PARSE_ERROR = "parse_error"
    UNKNOWN = "unknown"


class RuntimeErrorClass(StrEnum):
    """5-class runtime-fallback cause taxonomy (C07a §5.5).

    The runtime-switch ladder classifies every switchover cause into one of
    these closed members so the projection can ``GROUP BY cause`` without
    string normalisation.
    """

    RUNTIME_RATE_LIMIT = "RUNTIME_RATE_LIMIT"
    RUNTIME_SERVER_ERROR = "RUNTIME_SERVER_ERROR"
    RUNTIME_TIMEOUT = "RUNTIME_TIMEOUT"
    RUNTIME_API_ERROR = "RUNTIME_API_ERROR"
    RUNTIME_AUTH_ERROR = "RUNTIME_AUTH_ERROR"


class TelemetryProject(BaseModel):
    """One row per eawf project (a single ``.ea/``-bearing repo)."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    cwd: str
    repo_name: str | None
    first_seen: datetime | None
    last_seen: datetime | None
    has_settings_local: bool = False
    has_agents_md: bool = False
    has_eawf_state: bool = False


class TelemetrySession(BaseModel):
    """One row per dispatched session (wave attempt or interactive CLI)."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    project_id: str
    runtime: RuntimeName
    wave_id: str | None
    attempt_id: str | None
    session_log_path: str
    started_at: datetime | None
    ended_at: datetime | None
    duration_ms: int | None
    model_primary: str | None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read: int = 0
    total_cache_write: int = 0
    total_cost_usd: Decimal = Field(default=Decimal("0"))
    turn_count: int = 0
    tool_call_count: int = 0
    error_count: int = 0
    denial_count: int = 0
    interrupt_count: int = 0
    compaction_count: int = 0
    subagent_dispatch_count: int = 0
    end_marker: EndMarker
    parent_uuid_orphan_rate: float = 0.0
    git_branch_first: str | None = None
    custom_title: str | None = None
    ai_title: str | None = None


class TelemetryTurn(BaseModel):
    """One row per assistant turn within a session."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    turn_idx: int
    ts: datetime | None
    duration_ms: int | None
    model: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    thinking_only: bool = False


class TelemetryToolCall(BaseModel):
    """One row per tool invocation within a turn."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    turn_idx: int
    tool_use_id: str
    tool_name: str
    input_hash: str
    ts: datetime | None
    ended_ts: datetime | None
    is_error: bool = False
    error_kind: ToolCallErrorKind
    retry_of: str | None = None


class TelemetryCompaction(BaseModel):
    """One row per context-window compaction event within a session."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    ts: datetime | None
    pre_tokens: int | None
    trigger: str | None


class TelemetryRuntimeSwitch(BaseModel):
    """One row per V5 runtime switchover (NEW vs agent-lens)."""

    model_config = ConfigDict(extra="forbid")

    wave_id: str
    attempt_id_from: str
    attempt_id_to: str
    runtime_from: RuntimeName
    runtime_to: RuntimeName
    cause: RuntimeErrorClass
    ts: datetime


class TelemetryIncident(BaseModel):
    """One row per recorded incident (NEW vs agent-lens; V7)."""

    model_config = ConfigDict(extra="forbid")

    incident_id: str
    severity: IncidentSeverity
    cause: IncidentCause
    ts: datetime
    summary: str
    wave_id: str | None = None
    attempt_id: str | None = None


class TelemetryFileMeta(BaseModel):
    """Per-source-file scan cursor for incremental tail projection."""

    model_config = ConfigDict(extra="forbid")

    jsonl_path: str
    mtime: float
    size: int
    last_offset: int
    last_scan_ts: datetime


class TelemetrySchemaMeta(BaseModel):
    """One key/value row of projection schema metadata."""

    model_config = ConfigDict(extra="forbid")

    key: str
    value: str
