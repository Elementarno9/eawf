"""Find memory entries that exceed an age threshold and lack high confidence.

A memory entry is considered ``stale`` when:

- its ``status`` is currently ``ACTIVE``,
- its ``review_due`` (or fallback ``created_at`` from the JSONL envelope) is
  more than ``age_days`` old, and
- its ``confidence`` is **below** :class:`Confidence.HIGH` (i.e. ``medium`` or
  ``low``).

High-confidence entries are exempt — they age out of the auto-stale list and
must be retired explicitly via ``memory compact`` or supersession.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eawf.kernel.state.enums import Confidence, MemoryStatus
from eawf.kernel.state.models import State
from eawf.platform.memory.store import find_envelope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StaleEntry:
    """One stale memory entry."""

    id: str
    scope_id: str
    confidence: Confidence
    age_days: float


def _entry_anchor(memory_path: Path, mem_id: str, fallback: datetime) -> datetime:
    """Return ``review_due`` if set, else the envelope's ``created_at``."""
    env = find_envelope(memory_path, mem_id)
    if env is None:
        return fallback
    return env.created_at


def find_stale(
    *,
    state: State,
    memory_path: Path,
    age_days: int,
    now: datetime | None = None,
    scope_id: str | None = None,
) -> list[StaleEntry]:
    """Return memory IDs whose age exceeds *age_days* and confidence < high.

    Args:
        state: Loaded :class:`State`.
        memory_path: Path to ``memory.jsonl`` for envelope lookup.
        age_days: Threshold in days.
        now: Override for the current time.
        scope_id: Optional filter — when set, only entries with matching scope
            are considered.
    """
    moment = now if now is not None else datetime.now(UTC)
    threshold = timedelta(days=age_days)
    out: list[StaleEntry] = []
    index = state.memory_index or {}
    for mid, summary in index.items():
        if summary.status != MemoryStatus.ACTIVE:
            continue
        if summary.confidence == Confidence.HIGH:
            continue
        if scope_id is not None and summary.scope_id != scope_id:
            continue
        anchor = summary.review_due or _entry_anchor(memory_path, mid, moment)
        age = moment - anchor
        if age >= threshold:
            out.append(
                StaleEntry(
                    id=mid,
                    scope_id=summary.scope_id,
                    confidence=summary.confidence,
                    age_days=age.total_seconds() / 86400.0,
                )
            )
    out.sort(key=lambda e: (-e.age_days, e.id))
    logger.info(f"find_stale age_days={age_days} count={len(out)}")
    return out
