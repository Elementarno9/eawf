"""Find memory entries that exceed an age threshold and lack high confidence.

A memory entry is considered ``stale`` when:

- its ``status`` is currently ``ACTIVE``,
- its ``review_due`` (or fallback ``created_at``) is
  more than ``age_days`` old, and
- its ``confidence`` is **below** :class:`Confidence.HIGH` (i.e. ``medium`` or
  ``low``).

High-confidence entries are exempt — they age out of the auto-stale list and
must be retired explicitly via ``memory compact`` or supersession.

This module is purely read-only: :func:`stale_notes` recommends; the
``memory prune`` and ``memory gc`` verbs act, through the daemon.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from eawf.kernel.state.enums import Confidence, MemoryStatus
from eawf.kernel.store.kinds.memory import MemoryNote

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StaleEntry:
    """One stale memory entry."""

    id: str
    scope_id: str
    confidence: Confidence
    age_days: float


def stale_notes(
    notes: Iterable[MemoryNote],
    *,
    age_days: int,
    now: datetime | None = None,
    scope_id: str | None = None,
) -> list[StaleEntry]:
    """Return the active, below-high-confidence notes older than *age_days*.

    Args:
        notes: The notes to judge.
        age_days: Threshold in days.
        now: Override for the current time.
        scope_id: Only notes of this scope are judged, when given.

    Returns:
        The stale entries, oldest first. A note with no age anchor is
        taken as written now, so it is never stale.
    """
    moment = now if now is not None else datetime.now(UTC)
    threshold = timedelta(days=age_days)
    out: list[StaleEntry] = []
    for note in notes:
        if note.status != MemoryStatus.ACTIVE:
            continue
        if note.confidence == Confidence.HIGH:
            continue
        if scope_id is not None and note.scope_id != scope_id:
            continue
        age = moment - (note.age_anchor or moment)
        if age >= threshold:
            out.append(
                StaleEntry(
                    id=note.id,
                    scope_id=note.scope_id,
                    confidence=note.confidence,
                    age_days=age.total_seconds() / 86400.0,
                )
            )
    out.sort(key=lambda e: (-e.age_days, e.id))
    logger.info(f"stale_notes age_days={age_days} count={len(out)}")
    return out
