"""Segment open/close logic for ``actual start`` / ``actual stop``.

A *segment* is a contiguous interval ``[started_at, ended_at]`` for a given
``(scope_id, session_id)`` pair. The store representation is
:class:`eawf.kernel.store.kinds.actual.ActualSegment`; this module wraps that with
the v0.1 open/close transitions.

Conventions:

- An *open* segment has ``status == ActualStatus.ACTIVE`` and
  ``ended_at == started_at`` (sentinel — the duration is zero until close).
- ``close_segment`` requires the *same* ``ActualSegment`` instance and the
  current wall-clock time; it computes ``elapsed_eu`` from
  ``(ended_at - started_at)`` divided by ``eu_minutes``.
- ``elapsed_eu`` is rendered in :class:`Decimal` for exactness; callers convert
  to :class:`float` at the schema boundary via :func:`eawf.estimation.eu.to_float`.

The functions here are pure — they do not touch state.json or the store. CLI
handlers feed them inputs and persist the outputs.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from eawf.estimation.eu import as_decimal
from eawf.kernel.state.enums import ActualStatus
from eawf.kernel.store.kinds.actual import ActualSegment


def open_segment(
    *,
    session_id: str,
    started_at: datetime,
) -> ActualSegment:
    """Return a freshly-opened :class:`ActualSegment` with zero elapsed time.

    The segment carries ``status == ActualStatus.ACTIVE`` and
    ``ended_at == started_at`` so the schema is satisfied while the segment
    remains open. ``close_segment`` overwrites both fields.
    """
    return ActualSegment(
        session_id=session_id,
        started_at=started_at,
        ended_at=started_at,
        eu=0.0,
        active_minutes=0.0,
        idle_excluded_minutes=0.0,
        external_wait_minutes=0.0,
        agent_runtime_minutes=0.0,
        status=ActualStatus.ACTIVE,
    )


def close_segment(
    segment: ActualSegment,
    *,
    ended_at: datetime,
    eu_minutes: Decimal | float | int | str,
    status: ActualStatus = ActualStatus.DONE,
) -> ActualSegment:
    """Return a closed copy of *segment* with elapsed_eu computed.

    Args:
        segment: The open segment to close. Must have ``status == ACTIVE``.
        ended_at: Wall-clock close time (must be ``>=`` ``segment.started_at``).
        eu_minutes: Minutes per EU (``estimation.eu_minutes`` config).
        status: Close status — :data:`ActualStatus.DONE` for a normal stop,
            :data:`ActualStatus.ABANDONED` for ``actual recover``, or
            :data:`ActualStatus.INTERRUPTED` for explicit operator abort.

    Raises:
        ValueError: When *segment* is not currently active or ``ended_at`` is
            earlier than ``segment.started_at``.
    """
    if segment.status != ActualStatus.ACTIVE:
        raise ValueError(f"close_segment expected status=active, got status={segment.status.value}")
    if ended_at < segment.started_at:
        raise ValueError(
            f"close_segment ended_at {ended_at.isoformat()} earlier than "
            f"started_at {segment.started_at.isoformat()}"
        )
    eu_m = as_decimal(eu_minutes)
    elapsed_seconds = Decimal((ended_at - segment.started_at).total_seconds())
    elapsed_minutes = elapsed_seconds / Decimal(60)
    elapsed_eu = elapsed_minutes / eu_m
    return ActualSegment(
        session_id=segment.session_id,
        started_at=segment.started_at,
        ended_at=ended_at,
        eu=float(elapsed_eu),
        active_minutes=float(elapsed_minutes),
        idle_excluded_minutes=0.0,
        external_wait_minutes=0.0,
        agent_runtime_minutes=float(elapsed_minutes),
        status=status,
    )


def is_open_for(
    segments: list[ActualSegment],
    *,
    session_id: str,
) -> bool:
    """Return ``True`` if *segments* contains an active segment for *session_id*.

    Used by ``actual start`` to reject double-open within the same
    ``(scope, session)`` pair.
    """
    return any(
        seg.status == ActualStatus.ACTIVE and seg.session_id == session_id for seg in segments
    )


def latest_open_segment(segments: list[ActualSegment]) -> ActualSegment | None:
    """Return the most recently-started active segment in *segments*, if any.

    Used by ``actual stop`` to find the segment to close when no explicit
    segment id is passed in.
    """
    open_segs = [seg for seg in segments if seg.status == ActualStatus.ACTIVE]
    if not open_segs:
        return None
    return max(open_segs, key=lambda s: s.started_at)
