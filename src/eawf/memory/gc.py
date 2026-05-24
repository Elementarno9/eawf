"""Memory GC: archive STALE memory entries older than a threshold.

Soft-archival only. Matched entries' ``MemorySummary.tier`` flips from
:attr:`~eawf.kernel.state.enums.MemoryTier.WORKING` to
:attr:`~eawf.kernel.state.enums.MemoryTier.ARCHIVAL`; the entry remains addressable
via ``memory list`` / ``memory view`` but drops out of the default
render-context window.

Selection model:

- ``--threshold-days <N>`` is the age threshold. Anchor is ``review_due``
  when set, else the JSONL envelope's ``created_at``.
- Only entries currently in :class:`MemoryStatus.STALE` with
  ``tier == MemoryTier.WORKING`` are eligible. Already-archival, retrieval,
  active, or pruned rows are ignored (the auto-stale list is the natural
  feed; archival happens *after* staleness is acknowledged).
- ``--dry-run`` reports the would-archive ids and writes nothing.

This module never opens locks. The caller wraps it in
:func:`eawf.cli._mutation.state_transaction` so the lock + atomic write
discipline is shared with sibling memory commands.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eawf.kernel.state.enums import MemoryStatus, MemoryTier
from eawf.kernel.state.models import State
from eawf.memory.store import find_envelope

logger = logging.getLogger(__name__)


class GcError(ValueError):
    """Raised when the GC pass cannot run (input contradicts state)."""


@dataclass(frozen=True)
class GcReport:
    """Outcome of a :func:`gc_memory` invocation."""

    archived_ids: list[str]
    skipped_ids: list[str]
    dry_run: bool
    threshold_days: int
    now: datetime
    skipped_reasons: dict[str, str] = field(default_factory=dict)


def gc_memory(
    *,
    state: State,
    memory_path: Path,
    threshold_days: int,
    now: datetime | None = None,
    dry_run: bool = False,
) -> GcReport:
    """Archive matched memory entries by flipping their ``tier`` to ARCHIVAL.

    Args:
        state: Loaded :class:`State`. ``state.memory_index`` is mutated in
            place when ``dry_run=False``; the caller persists via the
            surrounding transaction.
        memory_path: Path to ``memory.jsonl`` for envelope lookup (the
            anchor when ``review_due`` is unset).
        threshold_days: Age threshold in days. Must be ``>= 0``.
        now: Override for the current time (tests).
        dry_run: When ``True``, computes the would-archive ids without
            mutating state.

    Returns:
        A :class:`GcReport` summarising the pass.

    Raises:
        GcError: When ``threshold_days`` is negative.
    """
    if threshold_days < 0:
        raise GcError(f"threshold_days must be >= 0; got {threshold_days}")
    moment = now if now is not None else datetime.now(UTC)
    threshold = timedelta(days=threshold_days)
    index = state.memory_index or {}

    archived_ids: list[str] = []
    skipped_ids: list[str] = []
    skipped_reasons: dict[str, str] = {}

    for mid, summary in index.items():
        if summary.status != MemoryStatus.STALE:
            skipped_ids.append(mid)
            skipped_reasons[mid] = f"status={summary.status.value}"
            continue
        if summary.tier != MemoryTier.WORKING:
            skipped_ids.append(mid)
            skipped_reasons[mid] = f"tier={summary.tier.value}"
            continue
        env = find_envelope(memory_path, mid)
        if env is None:
            skipped_ids.append(mid)
            skipped_reasons[mid] = "no-envelope"
            continue
        anchor = summary.review_due or env.created_at
        age = moment - anchor
        if age < threshold:
            skipped_ids.append(mid)
            skipped_reasons[mid] = "younger-than-threshold"
            continue
        archived_ids.append(mid)

    if dry_run:
        logger.info(
            f"gc_memory dry_run threshold_days={threshold_days} would_archive={len(archived_ids)}"
        )
        return GcReport(
            archived_ids=sorted(archived_ids),
            skipped_ids=sorted(skipped_ids),
            dry_run=True,
            threshold_days=threshold_days,
            now=moment,
            skipped_reasons=skipped_reasons,
        )

    for mid in archived_ids:
        summary = index[mid]
        index[mid] = summary.model_copy(update={"tier": MemoryTier.ARCHIVAL})

    state.memory_index = index
    logger.info(
        f"gc_memory threshold_days={threshold_days} "
        f"archived={len(archived_ids)} skipped={len(skipped_ids)}"
    )
    return GcReport(
        archived_ids=sorted(archived_ids),
        skipped_ids=sorted(skipped_ids),
        dry_run=False,
        threshold_days=threshold_days,
        now=moment,
        skipped_reasons=skipped_reasons,
    )


__all__ = [
    "GcError",
    "GcReport",
    "gc_memory",
]
