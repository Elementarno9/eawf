"""Pydantic v2 telemetry row models (vendored shape, retyped).

These rows are the typed projection target for the observability subsystem
(C09 §5.9.2). Each model is retyped from the upstream prototype's dataclasses
(see :mod:`eawf.observability.telemetry`'s ``_VENDOR_PROVENANCE.txt`` for upstream
provenance) into a strict Pydantic v2 ``BaseModel`` with
``ConfigDict(extra="forbid")`` so the projection rejects unknown columns at
the boundary instead of silently dropping data.

Closed enums new to eawf (not present in the upstream prototype) live here
alongside the rows that consume them:

- :data:`EndMarker` — session end-state classifier; the ``runtime_switched``
  member is the V5 extension.
- :class:`ToolCallErrorKind` — closed taxonomy for tool-call failures,
  retyped from the upstream free-string ``error_kind``.
- :class:`RuntimeErrorClass` — 5-class runtime-fallback cause enum (C07a
  §5.5). :class:`~eawf.kernel.store.kinds.events.runtime_switched.RuntimeSwitchedPayload`
  carries ``cause`` as a typed member of this enum.
- :class:`TokenClass` / :class:`PriceSourceKind` — the five token classes a
  run's usage splits into and the closed provenance of the price applied to
  it. :func:`check_token_identity` and :func:`check_price_source` are the
  shared invariants every usage row (the ``dispatch_cost`` event payload
  and its projected :class:`TelemetryDispatchCost` row) enforces.

:class:`~eawf.kernel.state.enums.IncidentSeverity` and
:class:`~eawf.kernel.state.enums.IncidentCause` already live in
:mod:`eawf.kernel.state.enums` (the canonical state-enum module) and are imported
here so the incident row shares one taxonomy with the rest of the state
subsystem.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.state.enums import IncidentCause, IncidentSeverity

__all__ = [
    "DURATION_LABELS",
    "OTHER_LABEL",
    "DurationKind",
    "EndMarker",
    "ObservedDuration",
    "ObservedSession",
    "PriceSourceKind",
    "RuntimeErrorClass",
    "TelemetryCompaction",
    "TelemetryDispatchCost",
    "TelemetryFileMeta",
    "TelemetryIncident",
    "TelemetryProject",
    "TelemetryRuntimeSwitch",
    "TelemetrySchemaMeta",
    "TelemetrySession",
    "TelemetryToolCall",
    "TelemetryTurn",
    "TokenClass",
    "ToolCallErrorKind",
    "check_price_source",
    "check_token_identity",
    "null_unpriced_zero_cost",
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
members are vendored from the upstream prototype.
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


class TokenClass(StrEnum):
    """The five classes one run's token usage splits into.

    ``REASONING`` is a subset of ``OUTPUT`` on every supported runtime (one
    reports it as a counter, the other not at all), so it is a descriptive
    sub-metric and never a summand of the token total.
    """

    INPUT = "input"
    OUTPUT = "output"
    CACHE_READ = "cache_read"
    CACHE_WRITE = "cache_write"
    REASONING = "reasoning"


class PriceSourceKind(StrEnum):
    """Closed provenance of the cost recorded on one run's usage.

    Values:
        BILLED: The provider reported the charge itself.
        LIST_RECONSTRUCTED: eawf multiplied the measured token classes by a
            published rate table; the table revision rides beside the cost
            as ``rate_table_version``.
        UNPRICED: No rate resolved for the model. The recorded cost is null,
            never zero, so an unpriced run cannot be summed as a billed zero.
    """

    BILLED = "billed"
    LIST_RECONSTRUCTED = "list-reconstructed"
    UNPRICED = "unpriced"


def check_token_identity(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    reasoning_tokens: int | None,
    total_tokens: int,
) -> None:
    """Refuse a usage row whose token classes do not reconcile.

    The total is exactly input + output + cache-read + cache-write.
    Reasoning is excluded because it already lies inside output; a row
    that added it a second time would publish an inflated total.

    Args:
        input_tokens: Non-cached input tokens.
        output_tokens: Output tokens, reasoning included.
        cache_read_tokens: Prompt-cache read tokens.
        cache_write_tokens: Prompt-cache write tokens.
        reasoning_tokens: The reasoning slice of *output_tokens*, or
            ``None`` when the runtime reports no reasoning counter.
        total_tokens: The row's declared token total.

    Raises:
        ValueError: When the four summed classes differ from
            *total_tokens*, or when *reasoning_tokens* exceeds
            *output_tokens*.
    """
    summed = input_tokens + output_tokens + cache_read_tokens + cache_write_tokens
    if summed != total_tokens:
        raise ValueError(
            f"total_tokens {total_tokens} != input + output + cache_read + cache_write "
            f"({summed}); reasoning is never a summand"
        )
    if reasoning_tokens is not None and reasoning_tokens > output_tokens:
        raise ValueError(
            f"reasoning_tokens {reasoning_tokens} exceeds output_tokens {output_tokens}; "
            "reasoning is a subset of output"
        )


def check_price_source(
    *,
    cost_usd: Decimal | None,
    price_source: PriceSourceKind | None,
    rate_table_version: str | None,
) -> None:
    """Refuse a cost whose price provenance is missing or inconsistent.

    Args:
        cost_usd: The recorded cost, or ``None`` when nothing priced it.
        price_source: Where the cost came from, or ``None`` when the row
            names no source.
        rate_table_version: Revision of the rate table a
            list-reconstructed cost was computed from.

    Raises:
        ValueError: When the row names no *price_source*, when a
            ``list-reconstructed`` cost names no *rate_table_version*, when
            an ``unpriced`` row carries a cost, or when a priced row carries
            none.
    """
    if price_source is None:
        raise ValueError(f"cost_usd {cost_usd} carries no price_source")
    if price_source is PriceSourceKind.LIST_RECONSTRUCTED and not rate_table_version:
        raise ValueError("a list-reconstructed cost must name its rate_table_version")
    if price_source is PriceSourceKind.UNPRICED and cost_usd is not None:
        raise ValueError(f"an unpriced row records a null cost_usd, not {cost_usd}")
    if price_source is not PriceSourceKind.UNPRICED and cost_usd is None:
        raise ValueError(f"a {price_source.value} row must record its cost_usd")


def null_unpriced_zero_cost(data: object) -> object:
    """Map the zero cost an older unpriced row persisted onto null.

    Rows written before unpriced costs became null recorded them as zero;
    the event log and the telemetry cache still hold such rows, and they
    must keep validating. Any other input passes through unchanged.

    Args:
        data: The raw mapping a row model is validated from.

    Returns:
        *data* with ``cost_usd`` set to ``None`` when the row is unpriced
        and records a zero cost; otherwise *data* itself.
    """
    if not isinstance(data, dict) or data.get("price_source") != PriceSourceKind.UNPRICED:
        return data
    cost = data.get("cost_usd")
    if cost is None:
        return data
    try:
        is_zero = Decimal(str(cost)) == 0
    except InvalidOperation:
        return data
    return {**data, "cost_usd": None} if is_zero else data


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
    """One row per V5 runtime switchover (NEW vs the upstream prototype)."""

    model_config = ConfigDict(extra="forbid")

    wave_id: str
    attempt_id_from: str
    attempt_id_to: str
    runtime_from: RuntimeName
    runtime_to: RuntimeName
    cause: RuntimeErrorClass
    ts: datetime


class TelemetryDispatchCost(BaseModel):
    """One row per ``dispatch_cost`` event — the EU-forward capture source.

    Projected from a ``dispatch_cost`` event envelope (the post-dispatch
    token + cost accounting payload the daemon emits through the canonical
    writer). The row carries the dispatch forward facts so a later metering
    writer can fold per-session estimated-units into the unified
    ``actual_eu`` accessor — the *disconnect-EU* path that records effort
    forward at dispatch time independent of the per-runtime session join.

    Keying note (the W02 spike finding): the
    :class:`~eawf.kernel.store.kinds.events.dispatch_cost.DispatchCostPayload`
    carries **no** ``session_id`` field. Its correlation keys are
    ``wave_id`` plus ``attempt_id``, and the daemon mints that ``attempt_id``
    as a per-dispatch UUID in
    :func:`eawf.runtime.daemon.dispatch_runner.run_dispatch` that is **never**
    written back into :attr:`eawf.kernel.state.models.SessionAttempt.session_id`.
    So ``dispatch_cost`` rows do **not** map 1:1 onto
    ``Wave.sessions[*].session_id``; this row keys on the
    ``(envelope_id, wave_id, attempt_id)`` tuple the event actually
    provides. ``envelope_id`` is the primary key because two attempts of a
    wave (or an interactive session with ``wave_id``/``attempt_id`` both
    ``None``) would otherwise collide.

    Attributes:
        envelope_id: Id of the source ``dispatch_cost`` event envelope
            (the row's stable primary key).
        wave_id: ``W<NN>`` wave the dispatch served, or ``None`` for an
            interactive (non-wave) CLI session.
        attempt_id: Per-dispatch attempt id minted by the runner, or
            ``None`` for an interactive session with no attempt envelope.
        runtime: Runtime that incurred the cost.
        model: Model identifier the cost is priced against.
        input_tokens: Non-cached input tokens billed.
        output_tokens: Output tokens billed.
        cache_creation_input_tokens: Tokens written to the prompt cache.
        cache_read_input_tokens: Tokens served from the prompt cache.
        reasoning_tokens: Reasoning slice of ``output_tokens``, or ``None``
            when the runtime reports no reasoning counter (unknown, not
            zero).
        total_tokens: Input + output + cache-read + cache-write. Validated
            against the classes, so a row cannot publish a total its
            classes do not add up to.
        cost_usd: Priced cost in USD (``Decimal`` for exact accounting), or
            ``None`` on an unpriced row.
        price_source: Provenance of ``cost_usd``.
        rate_table_version: Rate-table revision a list-reconstructed cost
            was computed from; ``None`` for billed or unpriced rows.
        pricing_version: ``PRICING`` snapshot version that priced the cost.
        ts: When the cost was projected (post-dispatch).
    """

    model_config = ConfigDict(extra="forbid")

    envelope_id: str
    wave_id: str | None = None
    attempt_id: str | None = None
    runtime: RuntimeName
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    reasoning_tokens: int | None = None
    total_tokens: int
    cost_usd: Decimal | None = None
    price_source: PriceSourceKind
    rate_table_version: str | None = None
    pricing_version: str
    ts: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _legacy_unpriced_zero(cls, data: object) -> object:
        """Read an older unpriced row's zero cost as null."""
        return null_unpriced_zero_cost(data)

    @model_validator(mode="after")
    def _usage_reconciles(self) -> Self:
        """Enforce the token-total identity and the price-source contract.

        Raises:
            ValueError: Propagated from :func:`check_token_identity` or
                :func:`check_price_source`.
        """
        check_token_identity(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_input_tokens,
            cache_write_tokens=self.cache_creation_input_tokens,
            reasoning_tokens=self.reasoning_tokens,
            total_tokens=self.total_tokens,
        )
        check_price_source(
            cost_usd=self.cost_usd,
            price_source=self.price_source,
            rate_table_version=self.rate_table_version,
        )
        return self


class DurationKind(StrEnum):
    """The transcript block kinds whose measured durations are observed.

    Values:
        TOOL_CALL: Any tool invocation, from its request to its result.
        COMMAND: A shell tool invocation, labelled by command family.
        SUBAGENT: A delegated child agent, labelled by delegation purpose.
    """

    TOOL_CALL = "tool_call"
    COMMAND = "command"
    SUBAGENT = "subagent"


OTHER_LABEL: Final[str] = "other"
"""The fallback every duration classifier emits for a name outside its vocabulary."""

DURATION_LABELS: Final[dict[DurationKind, frozenset[str]]] = {
    DurationKind.TOOL_CALL: frozenset(
        {
            "Agent",
            "Bash",
            "Edit",
            "Glob",
            "Grep",
            "NotebookEdit",
            "Read",
            "Skill",
            "Task",
            "TodoWrite",
            "WebFetch",
            "WebSearch",
            "Write",
            "mcp",
            OTHER_LABEL,
        }
    ),
    DurationKind.COMMAND: frozenset(
        {
            "cat",
            "eawf",
            "find",
            "gh",
            "git",
            "grep",
            "just",
            "ls",
            "make",
            "npm",
            "pytest",
            "python",
            "rg",
            "sed",
            "uv",
            OTHER_LABEL,
        }
    ),
    DurationKind.SUBAGENT: frozenset({"Explore", "Plan", "general-purpose", OTHER_LABEL}),
}
"""Closed label vocabulary per duration kind.

A label is persisted into a local store, so it must never carry transcript
text: a command's first token can come from a heredoc body. Only names in
this table are written; anything else becomes :data:`OTHER_LABEL`.
"""


class ObservedSession(BaseModel):
    """One vendor runtime session swept from local session history.

    Observed rows have no Run subject: the key is the hashed vendor session
    reference plus the runtime. They live only in the gitignored telemetry
    cache and never enter a committed artifact.

    Attributes:
        vendor_session_ref: Hashed vendor session id (``vsid-`` digest).
        runtime: Runtime whose history the session came from.
        project_id: Scope label of the project the history belongs to.
        model: First model the session billed against, or ``None``.
        started_at: Earliest record timestamp.
        ended_at: Latest record timestamp.
        duration_ms: Wall-clock span between the two, or ``None``.
        turn_count: Number of distinct billed assistant messages.
        input_tokens: Non-cached input tokens.
        output_tokens: Output tokens, reasoning included.
        cache_read_tokens: Prompt-cache read tokens.
        cache_write_tokens: Prompt-cache write tokens.
        reasoning_tokens: Reasoning slice of output, or ``None`` when the
            runtime reports no reasoning counter.
        total_tokens: Input + output + cache-read + cache-write.
        cost_usd: Priced cost in USD, or ``None`` on an unpriced row.
        price_source: Provenance of ``cost_usd``.
        rate_table_version: Rate-table revision behind a list-reconstructed
            cost.
    """

    model_config = ConfigDict(extra="forbid")

    vendor_session_ref: str = Field(pattern=r"^vsid-[0-9a-f]{32}$")
    runtime: RuntimeName
    project_id: str
    model: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    turn_count: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int = Field(ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=Decimal("0"))
    price_source: PriceSourceKind
    rate_table_version: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _legacy_unpriced_zero(cls, data: object) -> object:
        """Read an older unpriced row's zero cost as null."""
        return null_unpriced_zero_cost(data)

    @model_validator(mode="after")
    def _usage_reconciles(self) -> Self:
        """Enforce the token-total identity and the price-source contract.

        Raises:
            ValueError: Propagated from :func:`check_token_identity` or
                :func:`check_price_source`.
        """
        check_token_identity(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens,
            total_tokens=self.total_tokens,
        )
        check_price_source(
            cost_usd=self.cost_usd,
            price_source=self.price_source,
            rate_table_version=self.rate_table_version,
        )
        return self


class ObservedDuration(BaseModel):
    """Measured durations of one block kind + label within one observed session.

    Attributes:
        vendor_session_ref: Hashed vendor session id of the owning session.
        runtime: Runtime whose history the session came from.
        kind: The transcript block kind measured.
        label: Closed-vocabulary label within *kind*.
        count: Number of completed blocks measured.
        total_ms: Summed block duration.
        max_ms: Longest single block.
    """

    model_config = ConfigDict(extra="forbid")

    vendor_session_ref: str = Field(pattern=r"^vsid-[0-9a-f]{32}$")
    runtime: RuntimeName
    kind: DurationKind
    label: str
    count: int = Field(ge=1)
    total_ms: int = Field(ge=0)
    max_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _label_in_vocabulary(self) -> Self:
        """Refuse a label outside the kind's closed vocabulary.

        Raises:
            ValueError: When *label* is not in :data:`DURATION_LABELS` for
                *kind*, or *max_ms* exceeds *total_ms*.
        """
        if self.label not in DURATION_LABELS[self.kind]:
            raise ValueError(f"label {self.label!r} is outside the {self.kind.value} vocabulary")
        if self.max_ms > self.total_ms:
            raise ValueError(f"max_ms {self.max_ms} exceeds total_ms {self.total_ms}")
        return self


class TelemetryIncident(BaseModel):
    """One row per recorded incident (NEW vs the upstream prototype; V7)."""

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
