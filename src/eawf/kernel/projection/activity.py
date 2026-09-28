"""The Activity grouping: every Run in exactly one of eight exception buckets.

The Run register is the queried population, and the grouping is a total and disjoint
partition of it. A Run lands in the one top-level bucket its stored status names, and a
suspended Run lands under ``needs operator`` in the one sub-bucket its
:class:`~eawf.kernel.state.epoch2.run.SuspensionReason` names -- or, when its record names
none, under an explicit unknown-reason sub-bucket rather than the largest one. A row that
states no status lands in no bucket and is counted apart, so the buckets never claim a Run
they were not told about.

Three buckets are read off records the Run register does not carry -- a control outcome,
a heartbeat, a delivery stage -- so their count is unknown and names why, and never a
zero standing in for a count nobody took. Because a heartbeat is not on the row, a stale
Run cannot yet be told from a running one; the ``running`` count is therefore stated as
an estimate, so it is never read as exact while it may still hold a Run that went quiet.

Every count, the parent's included, is derived from the rows when asked for and never
stored beside them, so the rail, the strip and the summary line cannot disagree.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from eawf.kernel.projection.compute import PROJECTION_PRODUCER, ProjectionRow
from eawf.kernel.projection.registers import RegisterView
from eawf.kernel.projection.truth import (
    Freshness,
    Precision,
    TruthField,
    TruthKind,
    TruthState,
)
from eawf.kernel.state.enums import MeasurementQuality
from eawf.kernel.state.epoch2.run import RunStatus, SuspensionReason

logger = logging.getLogger(__name__)

#: The console route the grouping is drawn on.
ACTIVITY_ROUTE: Final = "activity"


class ActivityExceptionBucket(StrEnum):
    """The eight top-level Activity buckets, in the order the rail draws them."""

    NEEDS_OPERATOR = "needs operator"
    UNKNOWN_CONTROL_OUTCOME = "unknown control outcome"
    LOST_STALE = "lost/stale"
    FAILED = "failed"
    CHECKING_INTEGRATING = "checking/integrating"
    RUNNING = "running"
    QUEUED = "queued"
    TERMINAL_RECENT = "terminal recent"


#: Which bucket each stored Run status lands in. Total over :class:`RunStatus`, so a new
#: status is an import failure here rather than a Run that silently lands nowhere.
STATUS_BUCKETS: Final[Mapping[RunStatus, ActivityExceptionBucket]] = MappingProxyType(
    {
        RunStatus.QUEUED: ActivityExceptionBucket.QUEUED,
        RunStatus.RUNNING: ActivityExceptionBucket.RUNNING,
        RunStatus.SUSPENDED: ActivityExceptionBucket.NEEDS_OPERATOR,
        RunStatus.COMPLETED: ActivityExceptionBucket.TERMINAL_RECENT,
        RunStatus.FAILED: ActivityExceptionBucket.FAILED,
        RunStatus.CANCELLED: ActivityExceptionBucket.TERMINAL_RECENT,
    }
)

#: The buckets whose facts the Run register does not carry, and why each has no count.
UNSTATED_BUCKETS: Final[Mapping[ActivityExceptionBucket, str]] = MappingProxyType(
    {
        ActivityExceptionBucket.UNKNOWN_CONTROL_OUTCOME: (
            "a control outcome is on the control ledger, not the Run register"
        ),
        ActivityExceptionBucket.LOST_STALE: "a Run's heartbeat is not on the Run register",
        ActivityExceptionBucket.CHECKING_INTEGRATING: (
            "a delivery stage is on the Batch, not the Run register"
        ),
    }
)

#: Why ``running`` is an estimate: a stale Run cannot be told from a running one yet.
RUNNING_ESTIMATE_REASON: Final = "a stale Run cannot be told from a running one without a heartbeat"

#: What a Run's status must be for its record to name a reason at all.
_SUSPENDED: Final = RunStatus.SUSPENDED.value


def _check_status_buckets() -> None:
    """Refuse a status table that does not cover every Run status.

    Raises:
        ValueError: A Run status lands in no bucket. Raised at import.
    """
    missing = [status.value for status in RunStatus if status not in STATUS_BUCKETS]
    if missing:
        raise ValueError(f"run statuses land in no activity bucket: {', '.join(missing)}")


_check_status_buckets()


def reason_label(reason: SuspensionReason | None) -> str:
    """Return the words a sub-bucket is drawn with; ``None`` is the unknown reason."""
    if reason is None:
        return "unknown reason"
    return reason.value.removeprefix("AWAITING_").replace("_", " ").lower()


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivityCount:
    """One bucket's, or one ``needs operator`` sub-bucket's, count as a truth field.

    Attributes:
        bucket: The top-level bucket.
        sub: Whether this row is a sub-bucket of ``needs operator``.
        reason: The suspension reason a sub-bucket counts; ``None`` on a sub-bucket is
            the explicit unknown reason, and on a top-level row means nothing.
        count: The count, known with its precision or unknown naming why.
    """

    bucket: ActivityExceptionBucket
    sub: bool
    reason: SuspensionReason | None
    count: TruthField[str]

    @property
    def label(self) -> str:
        """Return the label a rail row draws."""
        return f"↳ {reason_label(self.reason)}" if self.sub else self.bucket.value


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivityGrouping:
    """The Run register partitioned into the eight buckets at one cursor.

    Attributes:
        total: How many Runs the register holds, which every bucket sums back to.
        counts: Every bucket in order, ``needs operator``'s sub-buckets after it.
        unbucketed: The Runs whose row states no status the grouping reads.
    """

    total: int
    counts: tuple[ActivityCount, ...]
    unbucketed: int

    def top_level(self) -> tuple[ActivityCount, ...]:
        """Return the eight top-level rows, which the 80-column strip draws alone."""
        return tuple(c for c in self.counts if not c.sub)


def _field(
    *,
    value: int | None,
    revision: int,
    reason: str | None,
    estimate: bool = False,
) -> TruthField[str]:
    """Return one count: exact, an estimate naming why, or unknown naming why."""
    if value is None:
        return TruthField[str](
            value=None,
            state=TruthState.UNKNOWN,
            truth_kind=TruthKind.DERIVED,
            producer=PROJECTION_PRODUCER,
            producer_revision=revision,
            precision=Precision.UNAVAILABLE,
            measurement_quality=MeasurementQuality.UNAVAILABLE,
            freshness=Freshness.LIVE,
            provenance_refs=(ACTIVITY_ROUTE,),
            missing_reason=reason,
        )
    return TruthField[str](
        value=str(value),
        state=TruthState.KNOWN,
        truth_kind=TruthKind.ESTIMATED if estimate else TruthKind.DERIVED,
        producer=PROJECTION_PRODUCER,
        producer_revision=revision,
        precision=Precision.BOUNDED if estimate else Precision.EXACT,
        measurement_quality=(
            MeasurementQuality.ESTIMATED if estimate else MeasurementQuality.EXACT
        ),
        freshness=Freshness.LIVE,
        provenance_refs=(ACTIVITY_ROUTE,),
    )


def _placed(row: ProjectionRow) -> tuple[ActivityExceptionBucket, SuspensionReason | None] | None:
    """Return the bucket and suspension reason of one row, or ``None`` when it states none."""
    if row.status.state is not TruthState.KNOWN or row.status.value is None:
        return None
    try:
        status = RunStatus(row.status.value)
    except ValueError:
        return None
    reason: SuspensionReason | None = None
    if row.status.value == _SUSPENDED and row.suspension_reason is not None:
        try:
            reason = SuspensionReason(row.suspension_reason)
        except ValueError:
            reason = None
    return STATUS_BUCKETS[status], reason


def group_runs(register: RegisterView) -> ActivityGrouping:
    """Return the Run register partitioned into the eight buckets.

    Args:
        register: The Activity route's read model.

    Returns:
        Every bucket's count, the sub-buckets of ``needs operator`` after it, and the
        Runs that landed in no bucket.

    Raises:
        ValueError: ``register`` is another route's read model, whose rows are not Runs.
    """
    if register.route != ACTIVITY_ROUTE:
        raise ValueError(
            f"route {register.route!r} states no Run grouping; the grouping is the "
            f"{ACTIVITY_ROUTE!r} route's register"
        )
    revision = int(register.source_cursor) + 1
    placed = [_placed(row) for row in register.rows]
    landed = [p for p in placed if p is not None]
    counts: list[ActivityCount] = []
    for bucket in ActivityExceptionBucket:
        unstated = UNSTATED_BUCKETS.get(bucket)
        if unstated is not None:
            counts.append(
                ActivityCount(
                    bucket=bucket,
                    sub=False,
                    reason=None,
                    count=_field(value=None, revision=revision, reason=unstated),
                )
            )
            continue
        estimate = bucket is ActivityExceptionBucket.RUNNING
        counts.append(
            ActivityCount(
                bucket=bucket,
                sub=False,
                reason=None,
                count=_field(
                    value=sum(1 for b, _ in landed if b is bucket),
                    revision=revision,
                    reason=RUNNING_ESTIMATE_REASON if estimate else None,
                    estimate=estimate,
                ),
            )
        )
        if bucket is ActivityExceptionBucket.NEEDS_OPERATOR:
            for reason in (*SuspensionReason, None):
                n = sum(1 for b, r in landed if b is bucket and r is reason)
                counts.append(
                    ActivityCount(
                        bucket=bucket,
                        sub=True,
                        reason=reason,
                        count=_field(value=n, revision=revision, reason=None),
                    )
                )
    logger.debug(f"group_runs cursor={register.source_cursor} total={len(register.rows)}")
    return ActivityGrouping(
        total=len(register.rows),
        counts=tuple(counts),
        unbucketed=len(placed) - len(landed),
    )


__all__ = [
    "ACTIVITY_ROUTE",
    "RUNNING_ESTIMATE_REASON",
    "STATUS_BUCKETS",
    "UNSTATED_BUCKETS",
    "ActivityCount",
    "ActivityExceptionBucket",
    "ActivityGrouping",
    "group_runs",
    "reason_label",
]
