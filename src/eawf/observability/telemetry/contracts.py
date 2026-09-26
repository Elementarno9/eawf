"""Telemetry contracts: every emitted metric names its producer, retention and consumer.

A metric family that reaches a scrape without a contract is output nobody
owns: no one knows which store row it summarises, how long the rows behind it
live, or who reads it, so nobody notices when it goes stale or wrong. The
exporter runs :func:`check_metric_contracts` over every family it builds, so
adding a family without declaring its contract fails the export instead of
shipping an orphan metric.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class RetentionClassId(StrEnum):
    """The closed set of retention classes a durable row can belong to."""

    TELEMETRY = "telemetry"
    SEMANTIC_ACTIVITY = "semantic_activity"
    RAW_DIAGNOSTIC = "raw_diagnostic"
    RUN_CONTRACT = "run_contract"
    SHORT_TERM_MEMORY = "short_term_memory"
    LONG_TERM_MEMORY = "long_term_memory"
    WORK_CANDIDATE = "work_candidate"
    SMOKE_REFLECT = "smoke_reflect"
    BACKUP_WAL_TEMP = "backup_wal_temp"
    GATE_DIAGNOSTIC = "gate_diagnostic"
    CACHE = "cache"
    QUARANTINE = "quarantine"
    CANONICAL_PROOF = "canonical_proof"
    MEASUREMENT = "measurement"
    BUDGET_NOTICE = "budget_notice"
    OBSERVED_SESSION = "observed_session"


class TelemetryContract(BaseModel):
    """What one emitted metric promises about where it comes from and goes.

    Attributes:
        metric: The emitted metric family name.
        producer: The projected store table whose rows the family aggregates.
        retention_class: The retention class governing those rows' lifetime.
        consumer: The surface that reads the emitted family.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: str = Field(pattern=r"^eawf_[a-z0-9_]+$")
    producer: str = Field(min_length=1)
    retention_class: RetentionClassId
    consumer: str = Field(min_length=1)


class UndeclaredMetricError(ValueError):
    """An emitted metric family has no declared :class:`TelemetryContract`."""


_EXPORT_CONSUMER = "eawf metrics export"


def _index(contracts: Iterable[TelemetryContract]) -> Mapping[str, TelemetryContract]:
    """Key *contracts* by metric name, refusing a metric declared twice."""
    indexed: dict[str, TelemetryContract] = {}
    for contract in contracts:
        if contract.metric in indexed:
            raise ValueError(f"telemetry contract declared twice: {contract.metric!r}")
        indexed[contract.metric] = contract
    return MappingProxyType(indexed)


#: The declared contract of every metric family the exporter emits.
TELEMETRY_CONTRACTS: Mapping[str, TelemetryContract] = _index(
    (
        TelemetryContract(
            metric="eawf_tokens_total",
            producer="telemetry_sessions",
            retention_class=RetentionClassId.TELEMETRY,
            consumer=_EXPORT_CONSUMER,
        ),
        TelemetryContract(
            metric="eawf_cost_usd_total",
            producer="telemetry_sessions",
            retention_class=RetentionClassId.TELEMETRY,
            consumer=_EXPORT_CONSUMER,
        ),
        TelemetryContract(
            metric="eawf_cache_hit_ratio",
            producer="telemetry_sessions",
            retention_class=RetentionClassId.TELEMETRY,
            consumer=_EXPORT_CONSUMER,
        ),
        TelemetryContract(
            metric="eawf_session_duration_ms",
            producer="telemetry_sessions",
            retention_class=RetentionClassId.TELEMETRY,
            consumer=_EXPORT_CONSUMER,
        ),
        TelemetryContract(
            metric="eawf_subagent_dispatch_total",
            producer="telemetry_sessions",
            retention_class=RetentionClassId.TELEMETRY,
            consumer=_EXPORT_CONSUMER,
        ),
        TelemetryContract(
            metric="eawf_compaction_total",
            producer="telemetry_sessions",
            retention_class=RetentionClassId.TELEMETRY,
            consumer=_EXPORT_CONSUMER,
        ),
        TelemetryContract(
            metric="eawf_incidents_total",
            producer="telemetry_incidents",
            retention_class=RetentionClassId.TELEMETRY,
            consumer=_EXPORT_CONSUMER,
        ),
        TelemetryContract(
            metric="eawf_run_tokens_total",
            producer="telemetry_dispatch_costs",
            retention_class=RetentionClassId.TELEMETRY,
            consumer=_EXPORT_CONSUMER,
        ),
        TelemetryContract(
            metric="eawf_run_cost_usd_total",
            producer="telemetry_dispatch_costs",
            retention_class=RetentionClassId.TELEMETRY,
            consumer=_EXPORT_CONSUMER,
        ),
    )
)


def check_metric_contracts(
    metric_names: Iterable[str],
    *,
    contracts: Mapping[str, TelemetryContract] = TELEMETRY_CONTRACTS,
) -> None:
    """Refuse any emitted metric that has no declared contract.

    Args:
        metric_names: The family names about to be emitted.
        contracts: The declared contracts, keyed by metric name.

    Raises:
        UndeclaredMetricError: One or more names lack a contract; the message
            lists every undeclared name, sorted.
    """
    undeclared = sorted({name for name in metric_names if name not in contracts})
    if undeclared:
        logger.error(f"check_metric_contracts undeclared={undeclared}")
        raise UndeclaredMetricError(
            f"emitted metrics without a telemetry contract: {', '.join(undeclared)}; "
            "declare producer, retention class and consumer in TELEMETRY_CONTRACTS"
        )


__all__ = [
    "TELEMETRY_CONTRACTS",
    "RetentionClassId",
    "TelemetryContract",
    "UndeclaredMetricError",
    "check_metric_contracts",
]
