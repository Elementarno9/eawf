"""Personal baselines, findings and usage over the local session history.

# noqa: EAWF010 one analysis pass over typed index rows; split seam: baselines and findings

The input is the session index the session-history reader keeps (one row for
each session, its requests and its measured tool durations) plus the detail of
the sessions a caller chose to read in full. Nothing here opens a history file.

Each session is compared with the user's own earlier sessions only. Its
baseline is the cohort of sessions that ended before it started, never itself
and never a later one, taken from the narrowest cohort rung that holds enough
of them: project, runtime, model, role and effort first, widening to
user-wide history. The cohort's median and its median absolute deviation, on a
``log1p`` scale, give a robust z; a finding is raised only when the value is
both statistically extreme and practically large, and with fewer than eight
prior sessions only notices are raised.

A finding has exactly one severity: ``notice``, ``anomaly``, ``investigate``
or ``confirmed_issue``. A noise session, one with no operator work in it or a
provider-run review pass, is classed ``noise`` instead: it carries no finding
and no severity and is left out of every baseline, but its tokens still count
in the usage totals because the provider billed them.
"""

from __future__ import annotations

import bisect
import itertools
import logging
import math
import os
import secrets
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.observability.reflect.generations import (
    GenerationSummary,
    SessionContext,
    refuse_opaque_blob,
    session_context,
)
from eawf.observability.reflect.rates import (
    PricingQuality,
    RateRow,
    cost_ranking,
    pricing_quality,
    rate_table,
)
from eawf.observability.reflect.titles import TitleSource
from eawf.observability.telemetry.models import (
    DurationKind,
    ObservedDuration,
    ObservedRequest,
    ObservedSession,
)
from eawf.observability.telemetry.sources.session_history import (
    MAIN_LANE,
    HistoryRuntime,
    SessionDetail,
)
from eawf.observability.telemetry.store.base import AbstractMetricsStore

logger = logging.getLogger(__name__)

#: The file name prefix a stored analysis carries, before its date.
ANALYSIS_PREFIX: Final = "analysis-"

#: The local session index the analysis reads, inside the reflection collection.
HISTORY_INDEX_FILENAME: Final = "history-index.sqlite"

#: Prior sessions a cohort needs before it can raise anything but a notice.
MIN_BASELINE: Final = 8

#: A cohort this large is used at once; a smaller one is kept only while no
#: wider rung holds more.
POPULATED_COHORT: Final = 20

#: The most sessions a ranking lists.
RANKING_LIMIT: Final = 10

#: The value of a role or effort no record names.
UNKNOWN: Final = "unknown"

_AUTOMATED_REVIEW_MODEL: Final = "codex-auto-review"
_PROGRESS_STAGE: Final = "sessions"
_MINIMAL_INPUT_CHARS: Final = 4

_OBSERVED_SESSIONS: Final = "telemetry_observed_sessions"
_OBSERVED_REQUESTS: Final = "telemetry_observed_requests"
_OBSERVED_DURATIONS: Final = "telemetry_observed_durations"


class Severity(StrEnum):
    """How strongly a finding is evidenced, weakest first."""

    NOTICE = "notice"
    ANOMALY = "anomaly"
    INVESTIGATE = "investigate"
    CONFIRMED_ISSUE = "confirmed_issue"

    @property
    def rank(self) -> int:
        """The severity's position, weakest at zero."""
        return list(Severity).index(self)


class SessionClass(StrEnum):
    """Whether a session is analysed or set aside as noise."""

    LISTED = "listed"
    NOISE = "noise"


class NoiseReason(StrEnum):
    """Why a session is noise.

    Values:
        AUTOMATED_REVIEW: A provider-run review pass no operator asked for.
        NO_OPERATOR_WORK: At most one model step and an observed zero of
            tool calls; an unrecorded tool count never fires it.
    """

    AUTOMATED_REVIEW = "automated_review"
    NO_OPERATOR_WORK = "no_operator_work"


class StructuralIssue(StrEnum):
    """A contradiction the index rows show on their own.

    Values:
        IMPOSSIBLE_TOKEN_TOTALS: The session row's token classes are not the
            sums of its request rows.
        TIMESTAMP_REGRESSION: A later request carries an earlier timestamp.
    """

    IMPOSSIBLE_TOKEN_TOTALS = "impossible_token_totals"
    TIMESTAMP_REGRESSION = "timestamp_regression"


class Metric(StrEnum):
    """A per-session measurement a baseline can be taken over."""

    TOTAL_TOKENS = "total_tokens"
    GROSS_INPUT_TOKENS = "gross_input_tokens"
    FRESH_INPUT_TOKENS = "fresh_input_tokens"
    CACHE_READ_TOKENS = "cache_read_tokens"
    CACHE_WRITE_TOKENS = "cache_write_tokens"
    OUTPUT_TOKENS = "output_tokens"
    REASONING_TOKENS = "reasoning_tokens"
    CALENDAR_MS = "calendar_ms"
    MODEL_STEPS = "model_steps"
    TOOL_CALLS = "tool_calls"
    CHILD_COUNT = "child_count"
    PEAK_CONTEXT_TOKENS = "peak_context_tokens"
    FINAL_CONTEXT_TOKENS = "final_context_tokens"
    COMPACTION_COUNT = "compaction_count"
    COST_USD = "cost_usd"


class CohortRung(StrEnum):
    """One rung of the cohort ladder, narrowest first."""

    PROJECT_EXACT = "project_exact"
    PERSONAL_EXACT = "personal_exact"
    PERSONAL_MODEL_FAMILY = "personal_model_family"
    PERSONAL_WORKFLOW = "personal_workflow"
    PERSONAL_RUNTIME = "personal_runtime"
    USER_WIDE = "user_wide"


Dimension = Literal[
    "project_key",
    "runtime",
    "model",
    "model_family",
    "workflow_role",
    "effort_band",
    "orchestrated",
]

#: The dimensions each rung holds equal, narrowest rung first.
COHORT_LADDER: Final[tuple[tuple[CohortRung, tuple[Dimension, ...]], ...]] = (
    (
        CohortRung.PROJECT_EXACT,
        ("project_key", "runtime", "model", "workflow_role", "effort_band", "orchestrated"),
    ),
    (
        CohortRung.PERSONAL_EXACT,
        ("runtime", "model", "workflow_role", "effort_band", "orchestrated"),
    ),
    (
        CohortRung.PERSONAL_MODEL_FAMILY,
        ("runtime", "model_family", "workflow_role", "effort_band", "orchestrated"),
    ),
    (CohortRung.PERSONAL_WORKFLOW, ("workflow_role", "effort_band", "orchestrated")),
    (CohortRung.PERSONAL_RUNTIME, ("orchestrated", "runtime")),
    (CohortRung.USER_WIDE, ()),
)

COHORT_LABELS: Final[dict[CohortRung, str]] = {
    CohortRung.PROJECT_EXACT: (
        "your earlier sessions in this project on the same model, role and effort"
    ),
    CohortRung.PERSONAL_EXACT: "your earlier sessions on the same model, role and effort",
    CohortRung.PERSONAL_MODEL_FAMILY: "your earlier sessions on the same model family",
    CohortRung.PERSONAL_WORKFLOW: "your earlier sessions in the same role and effort band",
    CohortRung.PERSONAL_RUNTIME: (
        "your earlier sessions on the same runtime and orchestration shape"
    ),
    CohortRung.USER_WIDE: "all of your earlier comparable sessions",
}

METRIC_NOUNS: Final[dict[Metric, str]] = {
    Metric.TOTAL_TOKENS: "total tokens handled",
    Metric.GROSS_INPUT_TOKENS: "input tokens sent",
    Metric.FRESH_INPUT_TOKENS: "uncached input tokens",
    Metric.CACHE_READ_TOKENS: "cached input tokens",
    Metric.CACHE_WRITE_TOKENS: "cache writes",
    Metric.OUTPUT_TOKENS: "output tokens",
    Metric.REASONING_TOKENS: "reasoning tokens",
    Metric.CALENDAR_MS: "wall-clock time",
    Metric.MODEL_STEPS: "model steps",
    Metric.TOOL_CALLS: "tool calls",
    Metric.CHILD_COUNT: "subagents started",
    Metric.PEAK_CONTEXT_TOKENS: "peak context held",
    Metric.FINAL_CONTEXT_TOKENS: "context held at the end",
    Metric.COMPACTION_COUNT: "context rewrites",
    Metric.COST_USD: "cost at list rates",
}


@dataclass(frozen=True, slots=True)
class AnomalyRule:
    """When a statistically extreme value is also large enough to matter.

    Attributes:
        kind: ``scale`` compares a ratio over the median; ``count`` an
            absolute gap; ``compaction`` an absolute gap above the 95th
            percentile.
        delta: The ratio or the gap the value must clear.
        requires_verified_pricing: Whether the session's rate must be verified.
    """

    kind: Literal["scale", "count", "compaction"]
    delta: float
    requires_verified_pricing: bool = False


_SCALE: Final = AnomalyRule("scale", 0.5)

ANOMALY_RULES: Final[dict[Metric, AnomalyRule]] = {
    Metric.TOTAL_TOKENS: _SCALE,
    Metric.GROSS_INPUT_TOKENS: _SCALE,
    Metric.FRESH_INPUT_TOKENS: _SCALE,
    Metric.CACHE_READ_TOKENS: _SCALE,
    Metric.CACHE_WRITE_TOKENS: _SCALE,
    Metric.OUTPUT_TOKENS: _SCALE,
    Metric.REASONING_TOKENS: _SCALE,
    Metric.CALENDAR_MS: _SCALE,
    Metric.TOOL_CALLS: _SCALE,
    Metric.CHILD_COUNT: AnomalyRule("count", 2.0),
    Metric.PEAK_CONTEXT_TOKENS: _SCALE,
    Metric.FINAL_CONTEXT_TOKENS: _SCALE,
    Metric.COMPACTION_COUNT: AnomalyRule("compaction", 1.0),
    Metric.COST_USD: AnomalyRule("scale", 0.5, requires_verified_pricing=True),
}

# Advice with no session-specific number in it, so a card standing for many
# sessions never attributes one session's measurements to all of them.
_COMPACTION_ADVICE: Final = (
    "Cut what re-enters the window between rebuilds, or split the task so each part keeps"
    " a smaller context."
)
_CACHE_READ_ADVICE: Final = (
    "Most of this is cache reads. The lever is the size of what is held, not the number of"
    " requests that hold it."
)
_ADVICE: Final[dict[str, str]] = {
    "compaction_churn": _COMPACTION_ADVICE,
    "compaction_count_anomaly": _COMPACTION_ADVICE,
    "cache_read_tokens_anomaly": _CACHE_READ_ADVICE,
    "total_tokens_anomaly": _CACHE_READ_ADVICE,
    "orchestration_scale": (
        "Subagent tokens are counted in the parent's totals. Check each one earned its context"
        " before widening the fan-out again."
    ),
    "fresh_input_tokens_anomaly": (
        "Keep the stable preamble byte-identical across requests so it stays a cache prefix."
    ),
    "cache_write_tokens_anomaly": (
        "Cache writes repeat when the prefix keeps changing; keep the preamble stable."
    ),
    "output_tokens_anomaly": "Ask for the diff or the changed region, not the whole artifact.",
    "reasoning_tokens_anomaly": "A narrower prompt or a lower effort spends less reasoning.",
    "tool_calls_anomaly": "Batch small reads, or ask for the whole answer once.",
    "child_count_anomaly": "Check each subagent earned its context before widening the fan-out.",
    "peak_context_tokens_anomaly": "Every later request carries the peak; trimming it pays back.",
    "final_context_tokens_anomaly": "Close or split a task before its context reaches the window.",
    "calendar_ms_anomaly": "Wall-clock counts idle time; check whether the task was left open.",
}


class HistorySession(BaseModel):
    """One indexed session, as the analysis reads it.

    Attributes:
        session_ref: The hashed vendor session id.
        runtime: The runtime whose history it is.
        project_key: The working-directory digest, or ``unknown``.
        project_label: What the project is called, or ``unknown``.
        started_at: The earliest record timestamp.
        ended_at: The latest record timestamp.
        model: The first model billed, or ``unknown``.
        model_family: The model id with its version suffixes dropped.
        workflow_role: The agent role, or ``unknown`` when no record names it.
        effort_band: ``XS/S``, ``M``, ``L/XL`` or ``unknown``.
        orchestrated: Whether it delegated to a subagent.
        metrics: Every measurement observed; an unobserved one is absent.
        cost_usd: The list-rate cost, or ``None`` when unpriced.
        pricing_quality: How far that cost can be trusted.
        first_input_chars: The first operator input's length, from detail.
        issues: Contradictions its rows show.
        context: Its context generations, when its detail was read.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_ref: str
    runtime: HistoryRuntime
    project_key: str
    project_label: str
    started_at: datetime | None
    ended_at: datetime | None
    model: str
    model_family: str
    workflow_role: str = UNKNOWN
    effort_band: str = UNKNOWN
    orchestrated: bool
    metrics: dict[Metric, float]
    cost_usd: Decimal | None
    pricing_quality: PricingQuality
    first_input_chars: int | None = Field(default=None, ge=0)
    issues: tuple[StructuralIssue, ...] = ()
    context: SessionContext | None = None

    @cached_property
    def cohort_keys(self) -> tuple[tuple[str | bool, ...], ...]:
        """The values each cohort rung compares, one tuple per rung in ladder order."""
        return tuple(
            tuple(getattr(self, name) for name in dimensions) for _rung, dimensions in COHORT_LADDER
        )


class CohortRef(BaseModel):
    """The prior cohort one metric of one session was compared with.

    Attributes:
        rung: The ladder rung used.
        label: What a reader sees the cohort called.
        dimensions: The dimensions the rung holds equal.
        pool: ``healthy_reference`` leaves out sessions with a confirmed
            issue; ``observed_history`` keeps them.
        sample_count: Sessions in the cohort.
        prior_only: Always true: only sessions that ended before this one
            started are in it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rung: CohortRung
    label: str
    dimensions: tuple[Dimension, ...]
    pool: Literal["healthy_reference", "observed_history"]
    sample_count: int = Field(ge=0)
    prior_only: Literal[True] = True


class Baseline(BaseModel):
    """The prior cohort of one session for one metric, and its members.

    Attributes:
        cohort: Which cohort it is.
        members: The member sessions' refs, oldest first.
        values: Their values of the metric, in the same order.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    cohort: CohortRef
    members: tuple[str, ...]
    values: tuple[float, ...]


class MetricAssessment(BaseModel):
    """One metric of one session set against its prior cohort.

    Attributes:
        metric: The metric.
        value: The session's value.
        cohort: The cohort it was compared with.
        median: The cohort's median value.
        mad: The cohort's median absolute deviation on the ``log1p`` scale
            the robust z is taken on.
        p95: The cohort's 95th percentile.
        percentile: Share of the cohort at or below the value, in percent.
        robust_z: The robust z, or ``None`` when it is unbounded.
        robust_method: ``mad``; ``iqr`` when the MAD is zero; or
            ``zero_dispersion`` when both are.
        practical_ratio: The value's ratio above the median, or ``None`` when
            the median is zero.
        raised: Whether the value is both statistically extreme and practically large.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: Metric
    value: float
    cohort: CohortRef
    median: float
    mad: float
    p95: float
    percentile: float
    robust_z: float | None
    robust_method: Literal["mad", "iqr", "zero_dispersion"]
    practical_ratio: float | None
    raised: bool


class Finding(BaseModel):
    """One thing worth a reader's attention about one session.

    Attributes:
        finding_type: What it reports, such as ``total_tokens_anomaly``.
        severity: Its one severity.
        actor: Who is most likely behind it.
        summary: What happened, in plain words.
        guard: What the finding does not claim.
        confidence: ``low``, ``medium`` or ``high``.
        metric: The metric it is about, when it is about one.
        value: The session's value of that metric.
        cohort: The prior cohort it was raised against, when it was; the
            rung named here is the one used.
        recommendation: What to change, with no session-specific number.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_type: str
    severity: Severity
    actor: str
    summary: str
    guard: str
    confidence: Literal["low", "medium", "high"]
    metric: Metric | None = None
    value: float | None = None
    cohort: CohortRef | None = None
    recommendation: str | None = None


class SessionTitle(BaseModel):
    """One session's title and where it came from.

    Attributes:
        session_ref: The session.
        title: The title, or ``None`` when its source is ``unavailable``.
        source: Where the title came from.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_ref: str
    title: str | None
    source: TitleSource


class SessionAnalysis(BaseModel):
    """One analysed session.

    Attributes:
        session_ref: The hashed vendor session id.
        runtime: Its runtime.
        project_key: Its working-directory digest, or ``unknown``.
        project_label: Its project's name, or ``unknown``.
        started_at: When it started.
        model: Its first billed model.
        workflow_role: Its role, or ``unknown``.
        effort_band: Its effort band, or ``unknown``.
        session_class: ``listed`` or ``noise``.
        noise_reasons: Why it is noise; empty when listed.
        pricing_quality: How far its cost can be trusted.
        cost_usd: Its list-rate cost; ``None`` for a Codex or unpriced session.
        metrics: Its observed measurements.
        anomalies: Every metric set against a populated cohort.
        findings: Its findings, strongest first; empty on a noise session.
        title: Its title, when one was resolved.
        context: Its context generations' totals, when its detail was read.
        context_limitations: What the context reading could not say.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_ref: str
    runtime: HistoryRuntime
    project_key: str
    project_label: str
    started_at: datetime | None
    model: str
    workflow_role: str
    effort_band: str
    session_class: SessionClass
    noise_reasons: tuple[NoiseReason, ...]
    pricing_quality: PricingQuality
    cost_usd: Decimal | None
    metrics: dict[Metric, float]
    anomalies: tuple[MetricAssessment, ...]
    findings: tuple[Finding, ...]
    title: SessionTitle | None = None
    context: tuple[GenerationSummary, ...] = ()
    context_limitations: tuple[str, ...] = ()


class UsageTotals(BaseModel):
    """What every session spent, noise included.

    Attributes:
        sessions: Sessions counted, noise included.
        noise_sessions: Of which noise.
        input_tokens: Non-cached input tokens.
        output_tokens: Output tokens.
        cache_read_tokens: Prompt-cache reads.
        cache_write_tokens: Prompt-cache writes.
        total_tokens: Every token class summed.
        cost_usd: The verified-priced sessions' cost, or ``None`` when none was.
        unpriced_sessions: Sessions with tokens but no verified cost, Codex included.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    sessions: int = Field(ge=0)
    noise_sessions: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cost_usd: Decimal | None
    unpriced_sessions: int = Field(ge=0)


class RepeatedPattern(BaseModel):
    """One finding type raised above ``notice`` on more than one session.

    Attributes:
        finding_type: The type.
        severity: The strongest severity it reached.
        summary: Its summary on the first session.
        sessions: The sessions, by ref.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_type: str
    severity: Severity
    summary: str
    sessions: tuple[str, ...]


class HistoryAnalysis(BaseModel):
    """The analysis of a local session history.

    Attributes:
        schema_version: The document's shape version.
        generated_at: When it was built.
        sessions: Every session, listed and noise, oldest first.
        usage: The usage totals, noise included.
        severity_counts: Findings per severity, every severity present.
        repeated_patterns: Finding types raised on more than one session.
        rates: One rate row per model seen.
        model_cost_ranking: The rate rows a dollar figure can rank.
        session_cost_ranking: Listed sessions with a finding, most expensive
            first; only verified-priced sessions, so no Codex session.
        session_token_ranking: Listed sessions with a finding that no dollar
            figure ranks, most tokens first.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    generated_at: datetime
    sessions: tuple[SessionAnalysis, ...]
    usage: UsageTotals
    severity_counts: dict[Severity, int]
    repeated_patterns: tuple[RepeatedPattern, ...]
    rates: tuple[RateRow, ...]
    model_cost_ranking: tuple[RateRow, ...]
    session_cost_ranking: tuple[str, ...]
    session_token_ranking: tuple[str, ...]


class IndexedHistory(BaseModel):
    """The session index rows an analysis reads.

    Attributes:
        sessions: Observed session rows.
        requests: Their request rows.
        durations: Their measured tool durations.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    sessions: tuple[ObservedSession, ...]
    requests: tuple[ObservedRequest, ...]
    durations: tuple[ObservedDuration, ...]


# ---- reading the index -----------------------------------------------------------------


def load_indexed_history(store: AbstractMetricsStore) -> IndexedHistory:
    """Return every observed session, request and duration row of *store*.

    Args:
        store: An initialised metrics store the session-history sweep wrote.

    Returns:
        The rows, sessions in start order.
    """
    sessions = [
        row
        for row in store.fetch_all(_OBSERVED_SESSIONS, ObservedSession)
        if isinstance(row, ObservedSession)
    ]
    sessions.sort(key=lambda row: (row.started_at is None, row.started_at, row.vendor_session_ref))
    return IndexedHistory(
        sessions=tuple(sessions),
        requests=tuple(
            row
            for row in store.fetch_all(_OBSERVED_REQUESTS, ObservedRequest)
            if isinstance(row, ObservedRequest)
        ),
        durations=tuple(
            row
            for row in store.fetch_all(_OBSERVED_DURATIONS, ObservedDuration)
            if isinstance(row, ObservedDuration)
        ),
    )


def _model_family(model: str) -> str:
    """Return *model* with every trailing part that carries a digit dropped."""
    parts = model.lower().replace("_", "-").split("-")
    while parts and any(character.isdigit() for character in parts[-1]):
        parts.pop()
    return "-".join(parts) or model.lower() or UNKNOWN


def _issues(
    row: ObservedSession, requests: Sequence[ObservedRequest]
) -> tuple[StructuralIssue, ...]:
    found: list[StructuralIssue] = []
    sums = (
        sum(item.input_tokens for item in requests),
        sum(item.output_tokens for item in requests),
        sum(item.cache_read_tokens for item in requests),
        sum(item.cache_write_tokens for item in requests),
    )
    held = (row.input_tokens, row.output_tokens, row.cache_read_tokens, row.cache_write_tokens)
    if requests and sums != held:
        found.append(StructuralIssue.IMPOSSIBLE_TOKEN_TOTALS)
    # Subagent lanes run alongside the session's own, so each keeps its own order.
    lanes: dict[str, list[datetime]] = defaultdict(list)
    for item in sorted(requests, key=lambda item: item.ordinal):
        lanes[item.lane].extend([item.ts] if item.ts else [])
    pairs = (pair for lane in lanes.values() for pair in itertools.pairwise(lane))
    if any(later < earlier for earlier, later in pairs):
        found.append(StructuralIssue.TIMESTAMP_REGRESSION)
    return tuple(found)


def _metrics(
    row: ObservedSession,
    requests: Sequence[ObservedRequest],
    durations: Sequence[ObservedDuration],
    context: SessionContext | None,
) -> dict[Metric, float]:
    gross = row.input_tokens + row.cache_read_tokens + row.cache_write_tokens
    values: dict[Metric, float | None] = {
        Metric.TOTAL_TOKENS: row.total_tokens,
        Metric.GROSS_INPUT_TOKENS: gross,
        Metric.FRESH_INPUT_TOKENS: row.input_tokens,
        Metric.CACHE_READ_TOKENS: row.cache_read_tokens,
        Metric.CACHE_WRITE_TOKENS: row.cache_write_tokens,
        Metric.OUTPUT_TOKENS: row.output_tokens,
        Metric.REASONING_TOKENS: row.reasoning_tokens,
        Metric.CALENDAR_MS: row.duration_ms,
        Metric.MODEL_STEPS: row.turn_count,
        Metric.COST_USD: None if row.cost_usd is None else float(row.cost_usd),
    }
    # A subagent's context is its own; the session's is read off its own lane.
    ordered = sorted((r for r in requests if r.lane == MAIN_LANE), key=lambda r: r.ordinal)
    if ordered:
        inputs = [
            item.input_tokens + item.cache_read_tokens + item.cache_write_tokens for item in ordered
        ]
        values[Metric.PEAK_CONTEXT_TOKENS] = max(inputs)
        values[Metric.FINAL_CONTEXT_TOKENS] = inputs[-1]
    # Only a Claude fold measures tool blocks, so a Codex session's tool and
    # subagent counts stay unobserved rather than reading as zero.
    if row.runtime == "claude":
        values[Metric.TOOL_CALLS] = sum(
            item.count for item in durations if item.kind is DurationKind.TOOL_CALL
        )
        values[Metric.CHILD_COUNT] = sum(
            item.count for item in durations if item.kind is DurationKind.SUBAGENT
        )
    if context is not None:
        values[Metric.COMPACTION_COUNT] = context.compaction_count
    return {metric: float(value) for metric, value in values.items() if value is not None}


def _first_input_chars(detail: SessionDetail) -> int | None:
    return next(
        (
            block.size_chars
            for block in detail.blocks
            if block.kind == "user" and block.lane == "main"
        ),
        None,
    )


def history_sessions(
    indexed: IndexedHistory,
    *,
    details: Mapping[str, SessionDetail],
    memory_files: Mapping[str, Sequence[tuple[str, int]]],
    project_labels: Mapping[str, str],
) -> tuple[HistorySession, ...]:
    """Return the analysis input for every indexed session.

    Args:
        indexed: The index rows.
        details: Session detail by session ref, for the sessions read in full;
            their compactions and first input are observed, the rest are not.
        memory_files: ``(label, tokens)`` memory files by working-directory
            digest, for the Claude preamble rebuild of the sessions in
            *details*; a session of a project not in it rebuilds no memory
            and records that its memory is unknown.
        project_labels: Project names by working-directory digest.

    Returns:
        One session per index row, in the index's order.
    """
    requests: dict[str, list[ObservedRequest]] = defaultdict(list)
    for request in indexed.requests:
        requests[request.vendor_session_ref].append(request)
    durations: dict[str, list[ObservedDuration]] = defaultdict(list)
    for duration in indexed.durations:
        durations[duration.vendor_session_ref].append(duration)
    sessions: list[HistorySession] = []
    for row in indexed.sessions:
        ref = row.vendor_session_ref
        detail = details.get(ref)
        memory = memory_files.get(row.cwd_digest) if row.cwd_digest is not None else None
        context = None if detail is None else session_context(detail, memory_files=memory)
        metrics = _metrics(row, requests[ref], durations[ref], context)
        model = row.model or UNKNOWN
        key = row.cwd_digest or UNKNOWN
        sessions.append(
            HistorySession(
                session_ref=ref,
                runtime="codex" if row.runtime == "codex" else "claude",
                project_key=key,
                project_label=project_labels.get(key, UNKNOWN),
                started_at=row.started_at,
                ended_at=row.ended_at,
                model=model,
                model_family=_model_family(model),
                orchestrated=metrics.get(Metric.CHILD_COUNT, 0.0) > 0,
                metrics=metrics,
                cost_usd=row.cost_usd,
                pricing_quality=pricing_quality(row.model),
                first_input_chars=None if detail is None else _first_input_chars(detail),
                issues=_issues(row, requests[ref]),
                context=context,
            )
        )
    logger.debug(f"history_sessions rows={len(sessions)} detailed={len(details)}")
    return tuple(sessions)


# ---- noise ----------------------------------------------------------------------------


def _noise_reasons(session: HistorySession) -> tuple[NoiseReason, ...]:
    """Return why *session* is noise; its input length never decides it."""
    reasons: list[NoiseReason] = []
    if session.model == _AUTOMATED_REVIEW_MODEL:
        reasons.append(NoiseReason.AUTOMATED_REVIEW)
    steps = session.metrics.get(Metric.MODEL_STEPS, 0.0)
    tools = session.metrics.get(Metric.TOOL_CALLS)
    if steps <= 1 and tools == 0:
        reasons.append(NoiseReason.NO_OPERATOR_WORK)
    return tuple(reasons)


# ---- baselines ------------------------------------------------------------------------


def _ended_before(candidate: HistorySession, moment: datetime) -> bool:
    return candidate.ended_at is not None and candidate.ended_at < moment


def prior_baseline(
    current: HistorySession,
    population: Sequence[HistorySession],
    metric: Metric,
    *,
    excluded: frozenset[str] = frozenset(),
    unhealthy: frozenset[str] = frozenset(),
) -> Baseline | None:
    """Return the prior cohort *current* is compared with on *metric*.

    Members ended before *current* started, so neither the session itself nor
    a later or overlapping one is ever in its own baseline. A member must have
    exercised the metric: a zero says the session never did the thing measured
    and would pull the centre to zero. The ladder is walked narrowest first,
    over the healthy pool and then over all observed history; the first rung
    with :data:`POPULATED_COHORT` members is used, otherwise the largest.

    Args:
        current: The session evaluated.
        population: Every session, in any order.
        metric: The metric compared.
        excluded: Sessions left out of every baseline, noise among them.
        unhealthy: Sessions left out of the healthy pool only.

    Returns:
        The baseline, or ``None`` when *current* has no start time to set a
        moment by.
    """
    moment = current.started_at
    if moment is None:
        return None
    prior = sorted(
        (
            session
            for session in population
            if session.session_ref != current.session_ref
            and session.session_ref not in excluded
            and _ended_before(session, moment)
            and session.metrics.get(metric, 0.0) > 0
            and (
                not ANOMALY_RULES.get(metric, _SCALE).requires_verified_pricing
                or session.pricing_quality is PricingQuality.VERIFIED
            )
        ),
        key=lambda session: (session.ended_at, session.session_ref),
    )
    healthy = [session for session in prior if session.session_ref not in unhealthy]
    best: Baseline | None = None
    for pool_name, pool in (("healthy_reference", healthy), ("observed_history", prior)):
        for position, (rung, dimensions) in enumerate(COHORT_LADDER):
            wanted = current.cohort_keys[position]
            members = [session for session in pool if session.cohort_keys[position] == wanted]
            baseline = Baseline(
                cohort=CohortRef(
                    rung=rung,
                    label=COHORT_LABELS[rung],
                    dimensions=dimensions,
                    pool="healthy_reference"
                    if pool_name == "healthy_reference"
                    else "observed_history",
                    sample_count=len(members),
                ),
                members=tuple(session.session_ref for session in members),
                values=tuple(session.metrics[metric] for session in members),
            )
            if len(members) >= POPULATED_COHORT:
                return baseline
            if best is None or len(members) > best.cohort.sample_count:
                best = baseline
    return best


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower, upper = math.floor(position), math.ceil(position)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _sample_floors(sample_count: int) -> tuple[float, float]:
    """Return the robust-z and percentile floors a cohort of this size must clear."""
    if sample_count < MIN_BASELINE:
        return math.inf, 100.0
    if sample_count < POPULATED_COHORT:
        return 4.0, 97.5
    return 3.5, 95.0


def _practical(rule: AnomalyRule, value: float, median: float, p95: float) -> bool:
    delta = value - median
    if rule.kind == "scale":
        return value > median and value >= median * (1 + rule.delta)
    if rule.kind == "count":
        return delta >= rule.delta
    return delta >= rule.delta and value > p95


def assess_metric(metric: Metric, value: float, baseline: Baseline) -> MetricAssessment:
    """Set one value against its prior baseline.

    The robust z is taken on a ``log1p`` scale from the cohort's median and
    MAD; a zero MAD falls back to the interquartile range, and a zero range to
    an unbounded z above the centre. A value is raised only when the z and the
    percentile clear the floors for the cohort's size, the cohort's median is
    above zero, the value is above the cohort's 95th percentile, and the gap
    is practically large; a cohort under :data:`MIN_BASELINE` never raises.

    Args:
        metric: The metric.
        value: The session's value.
        baseline: Its prior cohort, holding at least one value.

    Returns:
        The assessment.

    Raises:
        ValueError: The baseline holds no value.
    """
    values = list(baseline.values)
    if not values:
        raise ValueError("a baseline with no value cannot be assessed")
    median = statistics.median(values)
    p95 = _quantile(values, 0.95)
    percentile = 100.0 * sum(item <= value for item in values) / len(values)
    transformed = [math.log1p(max(item, 0.0)) for item in values]
    centre = statistics.median(transformed)
    point = math.log1p(max(value, 0.0))
    mad = statistics.median(abs(item - centre) for item in transformed)
    method: Literal["mad", "iqr", "zero_dispersion"] = "mad"
    if mad > 0:
        robust_z = 0.6745 * (point - centre) / mad
    else:
        spread = _quantile(transformed, 0.75) - _quantile(transformed, 0.25)
        if spread > 0:
            method, robust_z = "iqr", (point - centre) / (spread / 1.349)
        else:
            method, robust_z = "zero_dispersion", math.inf if point > centre else 0.0
    z_floor, percentile_floor = _sample_floors(len(values))
    rule = ANOMALY_RULES[metric]
    extreme = (
        len(values) >= MIN_BASELINE
        and robust_z >= z_floor
        and percentile >= percentile_floor
        and median > 0
        and value > p95
    )
    return MetricAssessment(
        metric=metric,
        value=value,
        cohort=baseline.cohort,
        median=median,
        mad=mad,
        p95=p95,
        percentile=percentile,
        robust_z=robust_z if math.isfinite(robust_z) else None,
        robust_method=method,
        practical_ratio=(value - median) / median if median > 0 else None,
        raised=bool(extreme and _practical(rule, value, median, p95)),
    )


# ---- findings -------------------------------------------------------------------------


def _finding(
    finding_type: str,
    severity: Severity,
    *,
    actor: str,
    summary: str,
    guard: str,
    confidence: Literal["low", "medium", "high"],
    metric: Metric | None = None,
    value: float | None = None,
    cohort: CohortRef | None = None,
) -> Finding:
    return Finding(
        finding_type=finding_type,
        severity=severity,
        actor=actor,
        summary=summary,
        guard=guard,
        confidence=confidence,
        metric=metric,
        value=value,
        cohort=cohort,
        recommendation=_ADVICE.get(finding_type),
    )


_STRUCTURAL_SUMMARIES: Final[dict[StructuralIssue, tuple[str, str]]] = {
    StructuralIssue.IMPOSSIBLE_TOKEN_TOTALS: (
        "provider",
        "the session's token totals are not the sums of its requests",
    ),
    StructuralIssue.TIMESTAMP_REGRESSION: (
        "provider",
        "a later request is stamped earlier than the one before it",
    ),
}


def _structural_findings(session: HistorySession) -> list[Finding]:
    return [
        _finding(
            issue.value,
            Severity.CONFIRMED_ISSUE,
            actor=_STRUCTURAL_SUMMARIES[issue][0],
            summary=_STRUCTURAL_SUMMARIES[issue][1],
            guard="raised only on a contradiction the index rows state themselves",
            confidence="high",
        )
        for issue in session.issues
    ]


def _mechanism_findings(session: HistorySession) -> list[Finding]:
    findings: list[Finding] = []
    compactions = session.metrics.get(Metric.COMPACTION_COUNT, 0.0)
    if compactions >= 2:
        findings.append(
            _finding(
                "compaction_churn",
                Severity.INVESTIGATE,
                actor="runtime",
                summary="the context was compacted more than once in one session",
                guard="avoidability is not assumed",
                confidence="high",
                metric=Metric.COMPACTION_COUNT,
                value=compactions,
            )
        )
    children = session.metrics.get(Metric.CHILD_COUNT, 0.0)
    if children > 0:
        findings.append(
            _finding(
                "orchestration_scale",
                Severity.NOTICE,
                actor="orchestrator",
                summary="subagent work is included in this session's accounting",
                guard="fan-out is neutral without duplicated work or failure",
                confidence="high",
                metric=Metric.CHILD_COUNT,
                value=children,
            )
        )
    chars = session.first_input_chars
    if chars is not None and 0 < chars <= _MINIMAL_INPUT_CHARS:
        findings.append(
            _finding(
                "minimal_input",
                Severity.NOTICE,
                actor="operator",
                summary="the session opened on a structurally minimal input",
                guard="a short input such as a choice number or a command is valid; length alone"
                " implies neither noise nor error",
                confidence="low",
            )
        )
    return findings


def _anomaly_findings(
    session: HistorySession, anomalies: Sequence[MetricAssessment], mechanism: bool
) -> list[Finding]:
    severity = Severity.INVESTIGATE if mechanism else Severity.ANOMALY
    return [
        _finding(
            f"{item.metric.value}_anomaly",
            severity,
            actor="provider" if item.metric.value.startswith("cache") else "agent",
            summary=f"{METRIC_NOUNS[item.metric]} ran far above comparable prior sessions",
            guard="statistical extremeness plus a practical gap; correlation does not prove waste",
            confidence="high" if item.cohort.sample_count >= POPULATED_COHORT else "medium",
            metric=item.metric,
            value=item.value,
            cohort=item.cohort,
        )
        for item in anomalies
        if item.raised
    ]


def _baseline_notices(session: HistorySession, largest: int) -> list[Finding]:
    if session.started_at is None:
        return [
            _finding(
                "baseline_chronology_unavailable",
                Severity.NOTICE,
                actor="environment",
                summary="no start time, so no prior cohort exists to compare against",
                guard="input order is never read as chronology",
                confidence="high",
            )
        ]
    if largest < MIN_BASELINE:
        return [
            _finding(
                "baseline_cold_start",
                Severity.NOTICE,
                actor="environment",
                summary=f"the largest prior cohort holds {largest} sessions; anomalies need"
                f" {MIN_BASELINE}",
                guard="fewer than eight prior comparable sessions raise notices only",
                confidence="high",
            )
        ]
    return []


def _strongest_first(findings: Sequence[Finding]) -> tuple[Finding, ...]:
    by_type: dict[str, Finding] = {}
    for finding in findings:
        held = by_type.get(finding.finding_type)
        if held is None or finding.severity.rank > held.severity.rank:
            by_type[finding.finding_type] = finding
    return tuple(
        sorted(by_type.values(), key=lambda item: (-item.severity.rank, item.finding_type))
    )


# ---- the analysis ---------------------------------------------------------------------


def _usage(sessions: Sequence[HistorySession], noise: int) -> UsageTotals:
    def total(metric: Metric) -> int:
        return int(sum(session.metrics.get(metric, 0.0) for session in sessions))

    priced = [
        session.cost_usd
        for session in sessions
        if session.pricing_quality is PricingQuality.VERIFIED and session.cost_usd is not None
    ]
    return UsageTotals(
        sessions=len(sessions),
        noise_sessions=noise,
        input_tokens=total(Metric.FRESH_INPUT_TOKENS),
        output_tokens=total(Metric.OUTPUT_TOKENS),
        cache_read_tokens=total(Metric.CACHE_READ_TOKENS),
        cache_write_tokens=total(Metric.CACHE_WRITE_TOKENS),
        total_tokens=total(Metric.TOTAL_TOKENS),
        cost_usd=sum(priced, Decimal(0)) if priced else None,
        unpriced_sessions=len(sessions) - len(priced),
    )


def _repeated(rows: Sequence[SessionAnalysis]) -> tuple[RepeatedPattern, ...]:
    groups: dict[str, list[tuple[str, Finding]]] = defaultdict(list)
    for row in rows:
        for finding in row.findings:
            if finding.severity is not Severity.NOTICE:
                groups[finding.finding_type].append((row.session_ref, finding))
    patterns = [
        RepeatedPattern(
            finding_type=finding_type,
            severity=max((finding.severity for _ref, finding in members), key=lambda s: s.rank),
            summary=members[0][1].summary,
            sessions=tuple(ref for ref, _finding in members),
        )
        for finding_type, members in groups.items()
        if len(members) > 1
    ]
    return tuple(
        sorted(
            patterns, key=lambda item: (-item.severity.rank, -len(item.sessions), item.finding_type)
        )
    )


def _rankings(rows: Sequence[SessionAnalysis]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    flagged = [row for row in rows if row.session_class is SessionClass.LISTED and row.findings]
    priced = [
        row
        for row in flagged
        if row.pricing_quality is PricingQuality.VERIFIED and row.cost_usd is not None
    ]
    by_cost = sorted(priced, key=lambda row: (-(row.cost_usd or Decimal(0)), row.session_ref))
    priced_refs = {row.session_ref for row in priced}
    rest = [row for row in flagged if row.session_ref not in priced_refs]
    by_tokens = sorted(
        rest, key=lambda row: (-row.metrics.get(Metric.TOTAL_TOKENS, 0.0), row.session_ref)
    )
    return (
        tuple(row.session_ref for row in by_cost[:RANKING_LIMIT]),
        tuple(row.session_ref for row in by_tokens[:RANKING_LIMIT]),
    )


Progress = Callable[[str, int, int], None]
"""Receives ``("sessions", done, total)`` as the analysis works through the sessions."""


def analyze_sessions(
    sessions: Sequence[HistorySession],
    *,
    requests: Sequence[ObservedRequest],
    now: datetime,
    titles: Mapping[str, SessionTitle] | None = None,
    progress: Progress | None = None,
) -> HistoryAnalysis:
    """Analyse every session against its own prior baselines.

    Args:
        sessions: The sessions, in any order.
        requests: Their request rows, for the rate table.
        now: When the analysis is built.
        titles: Resolved titles by session ref.
        progress: Called at the start, the end and about every tenth session.

    Returns:
        The analysis; sessions oldest first.
    """
    ordered = sorted(
        sessions,
        key=lambda item: (item.started_at is None, item.started_at, item.session_ref),
    )
    noise = {session.session_ref: _noise_reasons(session) for session in ordered}
    excluded = frozenset(ref for ref, reasons in noise.items() if reasons)
    unhealthy = frozenset(session.session_ref for session in ordered if session.issues)
    eligible = {
        metric: sorted(
            (
                session
                for session in ordered
                if session.ended_at is not None
                and session.session_ref not in excluded
                and session.metrics.get(metric, 0.0) > 0
            ),
            key=lambda session: (session.ended_at, session.session_ref),
        )
        for metric in ANOMALY_RULES
    }
    ends = {
        metric: [session.ended_at for session in members if session.ended_at is not None]
        for metric, members in eligible.items()
    }
    rows: list[SessionAnalysis] = []
    total = len(ordered)
    if progress is not None:
        progress(_PROGRESS_STAGE, 0, total)
    for done, session in enumerate(ordered, start=1):
        reasons = noise[session.session_ref]
        anomalies: list[MetricAssessment] = []
        findings: list[Finding] = []
        if not reasons:
            largest = 0
            for metric, rule in ANOMALY_RULES.items():
                value = session.metrics.get(metric)
                if value is None or (
                    rule.requires_verified_pricing
                    and session.pricing_quality is not PricingQuality.VERIFIED
                ):
                    continue
                if session.started_at is None:
                    continue
                prefix = eligible[metric][: bisect.bisect_left(ends[metric], session.started_at)]
                baseline = prior_baseline(
                    session, prefix, metric, excluded=excluded, unhealthy=unhealthy
                )
                if baseline is None:
                    continue
                largest = max(largest, baseline.cohort.sample_count)
                if baseline.cohort.sample_count >= MIN_BASELINE:
                    anomalies.append(assess_metric(metric, value, baseline))
            mechanisms = _mechanism_findings(session)
            findings = [
                *_structural_findings(session),
                *_baseline_notices(session, largest),
                *mechanisms,
                *_anomaly_findings(
                    session,
                    anomalies,
                    any(item.severity is Severity.INVESTIGATE for item in mechanisms),
                ),
            ]
        rows.append(
            SessionAnalysis(
                session_ref=session.session_ref,
                runtime=session.runtime,
                project_key=session.project_key,
                project_label=session.project_label,
                started_at=session.started_at,
                model=session.model,
                workflow_role=session.workflow_role,
                effort_band=session.effort_band,
                session_class=SessionClass.NOISE if reasons else SessionClass.LISTED,
                noise_reasons=reasons,
                pricing_quality=session.pricing_quality,
                cost_usd=(
                    session.cost_usd if session.pricing_quality is PricingQuality.VERIFIED else None
                ),
                metrics=session.metrics,
                anomalies=tuple(anomalies),
                findings=_strongest_first(findings),
                title=None if titles is None else titles.get(session.session_ref),
                context=(
                    ()
                    if session.context is None
                    else tuple(item.summary for item in session.context.generations)
                ),
                context_limitations=(
                    () if session.context is None else session.context.limitations
                ),
            )
        )
        if progress is not None and (done == total or done % max(1, total // 10) == 0):
            progress(_PROGRESS_STAGE, done, total)
    rates = rate_table(requests)
    by_cost, by_tokens = _rankings(rows)
    counts = Counter(finding.severity for row in rows for finding in row.findings)
    logger.info(f"analyze_sessions sessions={total} noise={len(excluded)} models={len(rates)}")
    return HistoryAnalysis(
        generated_at=now,
        sessions=tuple(rows),
        usage=_usage(ordered, len(excluded)),
        severity_counts={severity: counts.get(severity, 0) for severity in Severity},
        repeated_patterns=_repeated(rows),
        rates=rates,
        model_cost_ranking=cost_ranking(rates),
        session_cost_ranking=by_cost,
        session_token_ranking=by_tokens,
    )


def write_analysis(root: Path, analysis: HistoryAnalysis, *, on: date) -> Path:
    """Write *analysis* under *root* as ``analysis-<date>.json``, atomically.

    Args:
        root: The local reflection collection.
        analysis: The analysis.
        on: The day it is filed under.

    Returns:
        The written file.

    Raises:
        OpaqueBlobError: The serialized analysis holds a run shaped like a
            reasoning signature; nothing is written.
    """
    text = refuse_opaque_blob(analysis.model_dump_json(indent=2))
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{ANALYSIS_PREFIX}{on.isoformat()}.json"
    tmp = path.with_name(f"{path.name}.tmp.{secrets.token_hex(4)}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


__all__ = [
    "ANALYSIS_PREFIX",
    "ANOMALY_RULES",
    "COHORT_LABELS",
    "COHORT_LADDER",
    "HISTORY_INDEX_FILENAME",
    "METRIC_NOUNS",
    "MIN_BASELINE",
    "POPULATED_COHORT",
    "AnomalyRule",
    "Baseline",
    "CohortRef",
    "CohortRung",
    "Finding",
    "HistoryAnalysis",
    "HistorySession",
    "IndexedHistory",
    "Metric",
    "MetricAssessment",
    "NoiseReason",
    "RepeatedPattern",
    "SessionAnalysis",
    "SessionClass",
    "SessionTitle",
    "Severity",
    "StructuralIssue",
    "UsageTotals",
    "analyze_sessions",
    "assess_metric",
    "history_sessions",
    "load_indexed_history",
    "prior_baseline",
    "write_analysis",
]
