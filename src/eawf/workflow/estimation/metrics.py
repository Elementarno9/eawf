"""Estimation metrics for the ``eawf metrics`` CLI and the telemetry projection.

The CLI's bare view and its ``variance`` sub-verb read the selected
generation's estimate and actual ledgers through
:func:`compute_ledger_metrics`: how many estimates and actuals the
generation holds, and the M26 estimate-actual variance over the subjects
that carry both. The functions are **pure**: they take ledger records and
return typed models without touching disk, locks or events.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.state.models import ActualSummary, EstimateSummary
from eawf.kernel.store.ledger import LedgerRecord

#: Schema version of the ``eawf metrics`` JSON envelope. Version 2 is the
#: ledger roll-up; version 1 was the retired wave table.
METRICS_SCHEMA_VERSION: Literal[2] = 2

#: The estimate-ledger statuses a re-fit files a mapping revision under.
_MAPPING_STATUSES: Final = frozenset({"applied", "proposed"})


class EstimateActualVarianceMetric(BaseModel):
    """M26 ``eawf_estimate_actual_variance_pct`` roll-up.

    The C09 §5.9.6 M26 gauge: ``(actual EU - planned EU) / planned EU *
    100`` aggregated over CLOSED waves that carry both an estimate and an
    actual. ``planned_eu`` sums :class:`EstimateSummary.expected_eu`,
    ``actual_eu`` sums :class:`ActualSummary.elapsed_eu` over the same
    contributing waves.

    ``variance_pct`` is ``None`` when ``sample_count == 0`` or when the
    planned-EU denominator is zero — the VarianceTile and the ship-gate
    Variance section surface "no data" rather than a fabricated ``0%``
    or a divide-by-zero.

    ``mapping_revisions`` labels the effort-unit mapping revisions the
    contributing estimates were made under, ``"unrecorded"`` for an estimate
    that recorded none, so an aggregate spanning revisions says so.
    """

    model_config = ConfigDict(extra="forbid")

    sample_count: int = Field(ge=0)
    planned_eu: float = Field(ge=0.0)
    actual_eu: float = Field(ge=0.0)
    variance_pct: float | None
    mapping_revisions: list[str] = Field(default_factory=list)


class LedgerMetrics(BaseModel):
    """The ``eawf metrics`` payload: a generation's estimate and actual ledgers.

    Attributes:
        schema_version: The envelope version, :data:`METRICS_SCHEMA_VERSION`.
        estimate_count: The estimates the ledger holds, one per subject.
        actual_count: The actuals the ledger holds, one per subject.
        unreadable_count: Lines that did not read as their kind.
        variance: The M26 variance over subjects carrying both.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = METRICS_SCHEMA_VERSION
    estimate_count: int = Field(ge=0)
    actual_count: int = Field(ge=0)
    unreadable_count: int = Field(ge=0)
    variance: EstimateActualVarianceMetric


def compute_ledger_metrics(
    estimates: Sequence[LedgerRecord], actuals: Sequence[LedgerRecord]
) -> LedgerMetrics:
    """Roll up a generation's estimate and actual ledgers.

    A line carries its estimate or actual under ``payload`` when the cutover
    imported it, and is the statement itself otherwise. A mapping revision
    filed on the estimate ledger is no estimate, and a line that does not
    read as its kind is counted as unreadable rather than guessed at.

    A subject contributes to the variance when it carries both an estimate
    and an actual that is not ``calibration_excluded``: an excluded figure
    is a floor or a split, so folding it in would report a measurement
    artefact as an over- or under-run. The variance is the aggregate
    ``(sum actual EU - sum planned EU) / sum planned EU * 100``, so one large
    over-run is not diluted by many small subjects.

    Args:
        estimates: The estimate ledger's lines, in file order.
        actuals: The actual ledger's lines, in file order.

    Returns:
        The counts and the variance; ``variance_pct`` is ``None`` when no
        subject contributes or the planned EU sums to zero.
    """
    planned: dict[str, EstimateSummary] = {}
    unreadable = 0
    for record in estimates:
        if record.status in _MAPPING_STATUSES:
            continue
        try:
            planned[record.record_key] = EstimateSummary.model_validate(
                record.payload.get("payload", record.payload)
            )
        except ValidationError:
            unreadable += 1
    measured: dict[str, ActualSummary] = {}
    for record in actuals:
        try:
            measured[record.record_key] = ActualSummary.model_validate(
                record.payload.get("payload", record.payload)
            )
        except ValidationError:
            unreadable += 1
    planned_eu = 0.0
    actual_eu = 0.0
    sample_count = 0
    revisions: set[str] = set()
    for subject, estimate in planned.items():
        actual = measured.get(subject)
        if actual is None or actual.calibration_excluded:
            continue
        planned_eu += estimate.expected_eu
        actual_eu += actual.elapsed_eu
        sample_count += 1
        revision = estimate.mapping_revision
        revisions.add("unrecorded" if revision is None else str(revision))
    variance_pct = (actual_eu - planned_eu) / planned_eu * 100.0 if planned_eu > 0 else None
    return LedgerMetrics(
        estimate_count=len(planned),
        actual_count=len(measured),
        unreadable_count=unreadable,
        variance=EstimateActualVarianceMetric(
            sample_count=sample_count,
            planned_eu=planned_eu,
            actual_eu=actual_eu,
            variance_pct=variance_pct,
            mapping_revisions=sorted(revisions),
        ),
    )


__all__ = [
    "METRICS_SCHEMA_VERSION",
    "EstimateActualVarianceMetric",
    "LedgerMetrics",
    "compute_ledger_metrics",
]
