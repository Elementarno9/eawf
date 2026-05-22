"""Telemetry exporter — Prometheus / JSON / CSV serialisation (C09 §5.9.5).

The exporter is the pure, deterministic seam between the projected metrics
store (:class:`~eawf.telemetry.store.base.AbstractMetricsStore`) and the
three operator-facing output formats wired into ``eawf metrics export``:

* ``prom`` — Prometheus textfile-collector v0.0.4 format with one
  ``# HELP`` / ``# TYPE`` block per metric family (C09 §5.9.5). Power
  users wire ``--format prom --out <path>`` into a cron / launchd job.
* ``json`` — a typed snapshot envelope serialised through Pydantic so the
  shape is schema-stable across consumers (TUI tile, release notes).
* ``csv`` — a flat ``metric,labels,value`` table for spreadsheet import.

Aggregation is a single pass over the projected
:class:`~eawf.telemetry.models.TelemetrySession` and
:class:`~eawf.telemetry.models.TelemetryIncident` rows; the result is a
:class:`MetricsSnapshot` of typed metric families. Every step is pure
(given the same rows it returns byte-identical output) so each format is
golden-matchable against a seeded fixture DB.

Determinism rules the formatters obey:

* metric families emit in a fixed declaration order;
* label-set rows within a family sort by their rendered label string;
* ``Decimal`` cost values quantise to a fixed scale so the textual form
  does not drift with the input's internal precision;
* ratios quantise to three decimal places.
"""

from __future__ import annotations

import csv as _csv
import io
import logging
from collections import defaultdict
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.telemetry.models import TelemetryIncident, TelemetrySession
from eawf.telemetry.store.base import AbstractMetricsStore

logger = logging.getLogger(__name__)

#: Snapshot envelope schema version (bumped when the JSON shape changes).
EXPORT_SCHEMA_VERSION: Literal[1] = 1

#: Decimal scale cost values quantise to before rendering (cents → 2dp).
_COST_QUANTUM = Decimal("0.01")
#: Decimal scale ratio gauges quantise to before rendering.
_RATIO_QUANTUM = Decimal("0.001")

ExportFormat = Literal["prom", "json", "csv"]
"""Closed set of exporter output formats."""


class MetricType(StrEnum):
    """Prometheus metric type for a family's ``# TYPE`` line."""

    COUNTER = "counter"
    GAUGE = "gauge"


class MetricSample(BaseModel):
    """One labelled sample within a metric family.

    Attributes:
        labels: Ordered label key/value pairs. Rendering sorts label keys
            so the emitted string is deterministic regardless of insertion
            order.
        value: The numeric sample value, pre-quantised by the aggregator.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    labels: tuple[tuple[str, str], ...]
    value: Decimal

    def label_string(self) -> str:
        """Render the label set as a sorted ``k="v",...`` Prometheus fragment."""
        if not self.labels:
            return ""
        ordered = sorted(self.labels, key=lambda kv: kv[0])
        inner = ",".join(f'{key}="{value}"' for key, value in ordered)
        return f"{{{inner}}}"


class MetricFamily(BaseModel):
    """A named metric family (one ``# HELP`` / ``# TYPE`` block).

    Attributes:
        name: Prometheus metric name (e.g. ``eawf_tokens_total``).
        help_text: One-line ``# HELP`` description.
        metric_type: ``counter`` or ``gauge`` for the ``# TYPE`` line.
        samples: Labelled samples; rendering sorts them by label string.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    help_text: str
    metric_type: MetricType
    samples: tuple[MetricSample, ...]

    def sorted_samples(self) -> tuple[MetricSample, ...]:
        """Return the samples sorted by their rendered label string."""
        return tuple(sorted(self.samples, key=lambda s: s.label_string()))


class MetricsSnapshot(BaseModel):
    """Typed snapshot of every metric family at export time.

    Attributes:
        schema_version: Snapshot envelope version literal.
        scope: Scope label the snapshot was aggregated for (e.g.
            ``repo/eawf``).
        families: Metric families in fixed declaration order.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = EXPORT_SCHEMA_VERSION
    scope: str
    families: tuple[MetricFamily, ...] = Field(default_factory=tuple)


def _quantise_cost(value: Decimal) -> Decimal:
    """Quantise a USD cost to two decimal places (banker's rounding)."""
    return value.quantize(_COST_QUANTUM, rounding=ROUND_HALF_EVEN)


def _quantise_ratio(value: Decimal) -> Decimal:
    """Quantise a ratio gauge to three decimal places (banker's rounding)."""
    return value.quantize(_RATIO_QUANTUM, rounding=ROUND_HALF_EVEN)


def build_snapshot(store: AbstractMetricsStore, *, scope: str) -> MetricsSnapshot:
    """Aggregate the projected store rows into a typed metrics snapshot.

    One pass over the projected
    :class:`~eawf.telemetry.models.TelemetrySession` and
    :class:`~eawf.telemetry.models.TelemetryIncident` rows produces the
    metric families C09 §5.9.5 surfaces. The aggregation is pure: the same
    store state yields a byte-identical snapshot.

    Args:
        store: An initialised metrics store (schema already created).
        scope: Scope label stamped onto token / cost / cache families
            (e.g. ``repo/eawf``).

    Returns:
        A :class:`MetricsSnapshot` of metric families in declaration order.
    """
    sessions = [
        row
        for row in store.fetch_all("telemetry_sessions", TelemetrySession)
        if isinstance(row, TelemetrySession)
    ]
    incidents = [
        row
        for row in store.fetch_all("telemetry_incidents", TelemetryIncident)
        if isinstance(row, TelemetryIncident)
    ]
    families: list[MetricFamily] = [
        _tokens_family(sessions, scope=scope),
        _cost_family(sessions, scope=scope),
        _cache_hit_ratio_family(sessions, scope=scope),
        _subagent_dispatch_family(sessions),
        _compaction_family(sessions),
        _incidents_family(incidents),
    ]
    snapshot = MetricsSnapshot(scope=scope, families=tuple(families))
    logger.info(
        f"build_snapshot scope={scope!r} sessions={len(sessions)} "
        f"incidents={len(incidents)} families={len(families)}"
    )
    return snapshot


def _tokens_family(sessions: list[TelemetrySession], *, scope: str) -> MetricFamily:
    """Build the ``eawf_tokens_total`` counter (M01), split by direction + runtime."""
    totals: dict[tuple[str, str], int] = defaultdict(int)
    for session in sessions:
        totals[("input", session.runtime)] += session.total_input_tokens
        totals[("output", session.runtime)] += session.total_output_tokens
        totals[("cache_read", session.runtime)] += session.total_cache_read
        totals[("cache_create", session.runtime)] += session.total_cache_write
    samples = tuple(
        MetricSample(
            labels=(("direction", direction), ("runtime", runtime), ("scope", scope)),
            value=Decimal(count),
        )
        for (direction, runtime), count in totals.items()
    )
    return MetricFamily(
        name="eawf_tokens_total",
        help_text="Total tokens by direction.",
        metric_type=MetricType.COUNTER,
        samples=samples,
    )


def _cost_family(sessions: list[TelemetrySession], *, scope: str) -> MetricFamily:
    """Build the ``eawf_cost_usd_total`` counter (M02), summed per runtime."""
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for session in sessions:
        totals[session.runtime] += session.total_cost_usd
    samples = tuple(
        MetricSample(
            labels=(("runtime", runtime), ("scope", scope)),
            value=_quantise_cost(total),
        )
        for runtime, total in totals.items()
    )
    return MetricFamily(
        name="eawf_cost_usd_total",
        help_text="Cumulative cost in USD.",
        metric_type=MetricType.COUNTER,
        samples=samples,
    )


def _cache_hit_ratio_family(sessions: list[TelemetrySession], *, scope: str) -> MetricFamily:
    """Build the ``eawf_cache_hit_ratio`` gauge (M04), per runtime.

    Ratio = cache_read / (cache_read + cache_create); a runtime with no
    cache activity contributes a ``0`` gauge so the family is never empty
    for an observed runtime.
    """
    reads: dict[str, int] = defaultdict(int)
    creates: dict[str, int] = defaultdict(int)
    for session in sessions:
        reads[session.runtime] += session.total_cache_read
        creates[session.runtime] += session.total_cache_write
    samples: list[MetricSample] = []
    for runtime in reads:
        denom = reads[runtime] + creates[runtime]
        ratio = Decimal("0") if denom == 0 else Decimal(reads[runtime]) / Decimal(denom)
        samples.append(
            MetricSample(
                labels=(("runtime", runtime), ("scope", scope)),
                value=_quantise_ratio(ratio),
            )
        )
    return MetricFamily(
        name="eawf_cache_hit_ratio",
        help_text="Cache-read / (cache-read + cache-create).",
        metric_type=MetricType.GAUGE,
        samples=tuple(samples),
    )


def _subagent_dispatch_family(sessions: list[TelemetrySession]) -> MetricFamily:
    """Build the ``eawf_subagent_dispatch_total`` counter (M21), summed across sessions."""
    total = sum(session.subagent_dispatch_count for session in sessions)
    samples = (MetricSample(labels=(), value=Decimal(total)),)
    return MetricFamily(
        name="eawf_subagent_dispatch_total",
        help_text="Cumulative subagent dispatches across all sessions.",
        metric_type=MetricType.COUNTER,
        samples=samples,
    )


def _compaction_family(sessions: list[TelemetrySession]) -> MetricFamily:
    """Build the ``eawf_compaction_total`` counter (M23), summed per runtime."""
    totals: dict[str, int] = defaultdict(int)
    for session in sessions:
        totals[session.runtime] += session.compaction_count
    samples = tuple(
        MetricSample(labels=(("runtime", runtime),), value=Decimal(count))
        for runtime, count in totals.items()
    )
    return MetricFamily(
        name="eawf_compaction_total",
        help_text="Per-runtime context-window compaction count.",
        metric_type=MetricType.COUNTER,
        samples=samples,
    )


def _incidents_family(incidents: list[TelemetryIncident]) -> MetricFamily:
    """Build the ``eawf_incidents_total`` counter (M07), by severity + cause."""
    totals: dict[tuple[str, str], int] = defaultdict(int)
    for incident in incidents:
        totals[(incident.severity.value, incident.cause.value)] += 1
    samples = tuple(
        MetricSample(
            labels=(("cause", cause), ("severity", severity)),
            value=Decimal(count),
        )
        for (severity, cause), count in totals.items()
    )
    return MetricFamily(
        name="eawf_incidents_total",
        help_text="Recorded incidents by severity and cause.",
        metric_type=MetricType.COUNTER,
        samples=samples,
    )


def _render_value(value: Decimal) -> str:
    """Render a Decimal sample value without an exponent and without trailing zeros.

    Integer-valued samples (counters) render as bare integers; fractional
    gauges render in plain decimal notation so the textual form is stable
    across both backends and golden runs.
    """
    normalised = value.normalize()
    text = format(normalised, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def render_prom(snapshot: MetricsSnapshot) -> str:
    """Render *snapshot* as Prometheus textfile-collector v0.0.4 text.

    Each family emits a ``# HELP`` and ``# TYPE`` line followed by its
    sorted samples. Families separate with a blank line; the document ends
    with a single trailing newline (the Prometheus textfile convention).

    Args:
        snapshot: The aggregated metrics snapshot.

    Returns:
        The Prometheus exposition text.
    """
    blocks: list[str] = []
    for family in snapshot.families:
        lines = [
            f"# HELP {family.name} {family.help_text}",
            f"# TYPE {family.name} {family.metric_type.value}",
        ]
        for sample in family.sorted_samples():
            lines.append(f"{family.name}{sample.label_string()} {_render_value(sample.value)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def render_json(snapshot: MetricsSnapshot) -> str:
    """Render *snapshot* as a deterministic, indented JSON string.

    Args:
        snapshot: The aggregated metrics snapshot.

    Returns:
        A two-space-indented JSON document with a trailing newline. Sample
        values render as strings so ``Decimal`` precision survives the
        round-trip without binary-float drift.
    """
    families_out: list[dict[str, object]] = []
    for family in snapshot.families:
        samples_out = [
            {
                "labels": dict(sorted(sample.labels)),
                "value": _render_value(sample.value),
            }
            for sample in family.sorted_samples()
        ]
        families_out.append(
            {
                "name": family.name,
                "help": family.help_text,
                "type": family.metric_type.value,
                "samples": samples_out,
            }
        )
    document = {
        "schema_version": snapshot.schema_version,
        "scope": snapshot.scope,
        "families": families_out,
    }
    import json as _json

    return _json.dumps(document, indent=2, sort_keys=True) + "\n"


def render_csv(snapshot: MetricsSnapshot) -> str:
    """Render *snapshot* as a flat ``metric,type,labels,value`` CSV.

    The ``labels`` column carries the sorted Prometheus label fragment so a
    spreadsheet import keeps one row per labelled sample. Uses ``\\n`` line
    endings (not the CSV-default ``\\r\\n``) so the golden bytes stay
    platform-stable.

    Args:
        snapshot: The aggregated metrics snapshot.

    Returns:
        The CSV document with a trailing newline.
    """
    buffer = io.StringIO()
    writer = _csv.writer(buffer, lineterminator="\n")
    writer.writerow(("metric", "type", "labels", "value"))
    for family in snapshot.families:
        for sample in family.sorted_samples():
            writer.writerow(
                (
                    family.name,
                    family.metric_type.value,
                    sample.label_string(),
                    _render_value(sample.value),
                )
            )
    return buffer.getvalue()


def render(snapshot: MetricsSnapshot, *, fmt: ExportFormat) -> str:
    """Render *snapshot* in the requested format.

    Args:
        snapshot: The aggregated metrics snapshot.
        fmt: One of ``"prom"``, ``"json"``, ``"csv"``.

    Returns:
        The rendered document text.

    Raises:
        ValueError: When *fmt* is not a recognised export format.
    """
    if fmt == "prom":
        return render_prom(snapshot)
    if fmt == "json":
        return render_json(snapshot)
    if fmt == "csv":
        return render_csv(snapshot)
    raise ValueError(f"unknown export format: {fmt!r}")


__all__ = [
    "EXPORT_SCHEMA_VERSION",
    "ExportFormat",
    "MetricFamily",
    "MetricSample",
    "MetricType",
    "MetricsSnapshot",
    "build_snapshot",
    "render",
    "render_csv",
    "render_json",
    "render_prom",
]
