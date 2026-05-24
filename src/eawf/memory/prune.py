"""Memory prune (soft-delete-only).

Pruning a memory entry flips its
:class:`~eawf.kernel.state.enums.MemoryStatus` to :class:`MemoryStatus.PRUNED` in the
state cache and appends a fresh JSONL envelope carrying the same ``id`` plus
``payload.expired_at = <now>``. The original record is preserved — compaction
reclaims space later (`memory compact` keeps the latest envelope per id, so
the pruned envelope shadows but does not erase the prior body). No hard
delete in v0.1.

Selection model:

- ``--older-than <days>`` is the age threshold; the anchor is
  ``review_due`` when set, else the entry's first envelope ``created_at``.
- ``--status`` filters to entries currently in that status (``stale`` is the
  default — the auto-stale list is the natural prune source). ``active`` is
  permitted but exits 7 (``USER_DECLINED``) at the CLI layer when the
  operator declines or ``--no-input`` is set without ``--yes``.
- ``--scope`` further filters by scope ID.
- ``--dry-run`` reports the would-prune IDs and writes nothing.

Returns a :class:`PruneResult` whose ``pruned_ids`` lists the IDs that were
flipped (or *would be* flipped under ``--dry-run``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eawf.kernel.state.enums import MemoryStatus
from eawf.kernel.state.models import State
from eawf.memory.store import append_envelope, find_envelope

logger = logging.getLogger(__name__)


class PruneError(ValueError):
    """Raised when the prune cannot be carried out (input contradicts state)."""


@dataclass(frozen=True)
class PruneResult:
    """Outcome of a :func:`prune_memory` invocation."""

    pruned_ids: list[str]
    skipped_ids: list[str]
    dry_run: bool
    older_than_days: int
    scope_id: str | None = None
    status_filter: MemoryStatus | None = None
    expired_at: datetime | None = None
    # Frozen-default workaround: the dataclass is frozen so mutable defaults
    # must use ``field(default_factory=...)``.
    skipped_reasons: dict[str, str] = field(default_factory=dict)


def prune_memory(
    *,
    state: State,
    memory_path: Path,
    age_days: int,
    status_filter: MemoryStatus | None = MemoryStatus.STALE,
    scope_id: str | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> PruneResult:
    """Soft-delete prune: flip matched entries' status to ``PRUNED``.

    Args:
        state: Loaded :class:`State`; ``state.memory_index`` is mutated in
            place when ``dry_run=False``. Callers must persist the state via
            the surrounding transaction.
        memory_path: Path to ``memory.jsonl`` (used for both the age-anchor
            envelope lookup and the appended ``expired_at`` row).
        age_days: Threshold in days; entries younger than this are skipped.
            Must be ``>= 0``.
        status_filter: When set, only entries currently in this status are
            considered. Defaults to :class:`MemoryStatus.STALE` — the
            auto-stale list is the natural prune source. Passing ``None``
            considers entries of any status (handy for tests; the CLI
            requires an explicit value).
        scope_id: Optional further filter by scope ID.
        now: Override for the current time (for tests).
        dry_run: When ``True``, computes the would-prune IDs and writes
            nothing.

    Returns:
        A :class:`PruneResult` summarising the operation.

    Raises:
        PruneError: When ``age_days`` is negative, or when an entry already in
            :class:`MemoryStatus.PRUNED` is matched (double-prune is rejected
            so the CLI surface stays idempotent — the second call reports the
            entry under ``skipped_ids`` with ``skipped_reasons[id]="pruned"``
            rather than erroring out).
    """
    if age_days < 0:
        raise PruneError(f"age_days must be >= 0; got {age_days}")
    moment = now if now is not None else datetime.now(UTC)
    threshold = timedelta(days=age_days)
    index = state.memory_index or {}

    pruned_ids: list[str] = []
    skipped_ids: list[str] = []
    skipped_reasons: dict[str, str] = {}

    for mid, summary in index.items():
        if scope_id is not None and summary.scope_id != scope_id:
            skipped_ids.append(mid)
            skipped_reasons[mid] = "scope-mismatch"
            continue
        if status_filter is not None and summary.status != status_filter:
            skipped_ids.append(mid)
            skipped_reasons[mid] = f"status={summary.status.value}"
            continue
        if summary.status == MemoryStatus.PRUNED:
            # Idempotent surface: a second prune over an already-pruned entry
            # is a no-op rather than an error.
            skipped_ids.append(mid)
            skipped_reasons[mid] = "already-pruned"
            continue
        env = find_envelope(memory_path, mid)
        if env is None:
            # Cache without a backing envelope: refuse to prune to keep the
            # JSONL authoritative.
            skipped_ids.append(mid)
            skipped_reasons[mid] = "no-envelope"
            continue
        anchor = summary.review_due or env.created_at
        age = moment - anchor
        if age < threshold:
            skipped_ids.append(mid)
            skipped_reasons[mid] = "younger-than-threshold"
            continue
        pruned_ids.append(mid)

    if dry_run:
        logger.info(f"prune_memory dry_run age_days={age_days} would_prune={len(pruned_ids)}")
        return PruneResult(
            pruned_ids=sorted(pruned_ids),
            skipped_ids=sorted(skipped_ids),
            dry_run=True,
            older_than_days=age_days,
            scope_id=scope_id,
            status_filter=status_filter,
            expired_at=None,
            skipped_reasons=skipped_reasons,
        )

    for mid in pruned_ids:
        summary = index[mid]
        env = find_envelope(memory_path, mid)
        if env is None:  # pragma: no cover — guarded above
            continue
        new_payload = dict(env.payload)
        new_payload["expired_at"] = moment.isoformat()
        refreshed = env.model_copy(
            update={
                "updated_at": moment,
                "payload": new_payload,
            }
        )
        append_envelope(memory_path, refreshed)
        index[mid] = summary.model_copy(update={"status": MemoryStatus.PRUNED})

    state.memory_index = index
    logger.info(
        f"prune_memory age_days={age_days} pruned={len(pruned_ids)} skipped={len(skipped_ids)}"
    )
    return PruneResult(
        pruned_ids=sorted(pruned_ids),
        skipped_ids=sorted(skipped_ids),
        dry_run=False,
        older_than_days=age_days,
        scope_id=scope_id,
        status_filter=status_filter,
        expired_at=moment,
        skipped_reasons=skipped_reasons,
    )


__all__ = [
    "PruneError",
    "PruneResult",
    "prune_memory",
]
