"""Stale-segment recovery for ``eawf actual recover``.

Walks open segments held by dead lock holders and marks them
:data:`ActualStatus.ABANDONED` with ``elapsed_minutes`` capped at
:data:`STALE_HEARTBEAT_SECONDS` so the abandoned record does not poison
calibration with a runaway wall-clock interval.

A segment is *stale* when:

1. The actual summary's status is :data:`ActualStatus.ACTIVE`, AND
2. The advisory lock for the session/scope is no longer held by a live process
   (per :func:`eawf.runtime.lock.stale.is_stale`).

The recovery pass is idempotent — running it twice is a no-op when no fresh
segments became stale in between.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from eawf.kernel.state.enums import ActualStatus
from eawf.kernel.state.models import ActualSummary
from eawf.kernel.store.kinds.actual import ActualPayload
from eawf.runtime.lock.stale import STALE_HEARTBEAT_SECONDS, is_stale
from eawf.workflow.estimation.eu import as_decimal
from eawf.workflow.estimation.segments import latest_open_segment

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveredSegment:
    """One stale segment found by :func:`recover_stale_segments`.

    Attributes:
        scope_id: The scope whose actual was recovered.
        session_id: The session that held the orphaned segment.
        started_at: Original segment start time (preserved).
        capped_ended_at: Recovered close time, capped at
            ``started_at + STALE_HEARTBEAT_SECONDS``.
        elapsed_eu: The capped elapsed value in EU.
        actual_id: The :class:`ActualSummary.id` for the recovered scope.
        store_record_id: The envelope id whose payload contains the segment.
    """

    scope_id: str
    session_id: str
    started_at: datetime
    capped_ended_at: datetime
    elapsed_eu: Decimal
    actual_id: str
    store_record_id: str


def find_stale_actuals(
    actuals: dict[str, ActualSummary],
    *,
    lock_dir: Path,
    scope: str | None = None,
) -> list[ActualSummary]:
    """Return active actuals whose lockfile is stale.

    Args:
        actuals: Snapshot of ``state.actuals`` (scope_id -> summary).
        lock_dir: Directory holding the per-scope advisory lockfiles. The
            lockfile filename convention is ``actual-<scope_id>.lock``.
        scope: Optional scope filter — when set, only the matching scope is
            checked. ``None`` walks every active actual.
    """
    stale: list[ActualSummary] = []
    for scope_id, summary in actuals.items():
        if summary.status != ActualStatus.ACTIVE:
            continue
        if scope is not None and scope_id != scope:
            continue
        lock_path = lock_dir / f"actual-{scope_id}.lock"
        if is_stale(lock_path):
            stale.append(summary)
    return stale


def cap_elapsed(
    started_at: datetime,
    *,
    now: datetime,
    eu_minutes: Decimal | float | int | str,
    cap_seconds: float = STALE_HEARTBEAT_SECONDS,
) -> tuple[datetime, Decimal]:
    """Return the capped ``ended_at`` and ``elapsed_eu`` for a stale segment.

    The cap prevents calibration poisoning: if a process crashes overnight, we
    refuse to record an "8-hour wave" — instead the segment is recorded as
    ``cap_seconds`` long.

    Args:
        started_at: Original segment start.
        now: Wall-clock time at recovery.
        eu_minutes: Minutes per EU.
        cap_seconds: Maximum elapsed wall-clock interval to record (default
            :data:`STALE_HEARTBEAT_SECONDS`).
    """
    eu_m = as_decimal(eu_minutes)
    raw_seconds = (now - started_at).total_seconds()
    if raw_seconds < 0:
        logger.warning(
            f"clock_skew_detected delta_s={-raw_seconds:.1f}; "
            f"now precedes started_at, clamping to 0"
        )
        capped_seconds = 0.0
    else:
        capped_seconds = min(raw_seconds, cap_seconds)
    capped_ended_at = started_at + timedelta(seconds=capped_seconds)
    elapsed_eu = Decimal(capped_seconds) / Decimal(60) / eu_m
    return capped_ended_at, elapsed_eu


def recover_segment_payload(
    payload: ActualPayload,
    *,
    now: datetime,
    eu_minutes: Decimal | float | int | str,
) -> tuple[ActualPayload, RecoveredSegment | None]:
    """Mark the latest open segment in *payload* as :data:`ActualStatus.ABANDONED`.

    Returns the updated payload and a :class:`RecoveredSegment` describing the
    change. When *payload* has no open segment, returns the payload unchanged
    and ``None``.

    The cap is applied via :func:`cap_elapsed`; the payload's top-level
    ``elapsed_eu`` is updated to the sum of every segment's ``eu`` field
    (canonical recompute) so the summary reflects the new closed segment.
    """
    open_seg = latest_open_segment(payload.segments)
    if open_seg is None:
        return payload, None
    capped_ended_at, elapsed_eu = cap_elapsed(open_seg.started_at, now=now, eu_minutes=eu_minutes)
    new_segments = list(payload.segments)
    idx = new_segments.index(open_seg)
    new_segments[idx] = open_seg.model_copy(
        update={
            "ended_at": capped_ended_at,
            "eu": float(elapsed_eu),
            "active_minutes": float((capped_ended_at - open_seg.started_at).total_seconds() / 60.0),
            "agent_runtime_minutes": float(
                (capped_ended_at - open_seg.started_at).total_seconds() / 60.0
            ),
            "status": ActualStatus.ABANDONED,
        }
    )
    new_total_eu = sum(seg.eu for seg in new_segments)
    new_payload = payload.model_copy(
        update={
            "segments": new_segments,
            "elapsed_eu": float(new_total_eu),
            "outcome": "abandoned",
        }
    )
    recovered = RecoveredSegment(
        scope_id="",  # caller fills these in (payload doesn't carry scope_id directly)
        session_id=open_seg.session_id,
        started_at=open_seg.started_at,
        capped_ended_at=capped_ended_at,
        elapsed_eu=elapsed_eu,
        actual_id="",
        store_record_id="",
    )
    return new_payload, recovered
