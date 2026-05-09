"""Promote a session-scoped store record into a memory entry.

The source record may be any JSONL store envelope (``research``, ``audit``,
``decision``, …); the promotion copies its ``summary`` + payload-body into a
new ``memory.jsonl`` envelope and mirrors a :class:`MemorySummary` into the
state cache. The original envelope is preserved unmodified — promotion is a
forward link, not a delete.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from eawf.memory.store import MemoryRecord, add_memory
from eawf.state.enums import Confidence, MemoryStatus
from eawf.state.models import State
from eawf.store.envelope import Envelope

logger = logging.getLogger(__name__)


class PromotionError(ValueError):
    """Raised when the source record is missing, of an unsupported kind, or stale."""


@dataclass(frozen=True)
class PromotionResult:
    """Composite return value for ``promote_record``."""

    record: MemoryRecord
    source_store_record_id: str


def _load_source(store_path: Path, source_id: str) -> Envelope:
    """Return the latest envelope for *source_id* in *store_path*."""
    if not store_path.exists():
        raise PromotionError(f"store file does not exist: {store_path}")
    latest: Envelope | None = None
    for line in store_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        env = Envelope.model_validate_json(line)
        if env.id == source_id:
            latest = env
    if latest is None:
        raise PromotionError(f"source record {source_id!r} not found in {store_path.name}")
    return latest


def _extract_body(env: Envelope) -> str:
    """Choose the most informative payload field as the promoted body."""
    payload = env.payload
    for key in ("body", "text", "rationale", "findings", "summary"):
        if key in payload:
            value = payload[key]
            if isinstance(value, str):
                return value
            if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
                return "\n".join(value)
    return env.summary


def promote_record(
    *,
    state: State,
    source_store_path: Path,
    source_id: str,
    memory_path: Path,
    scope_id: str | None = None,
    confidence: Confidence = Confidence.MEDIUM,
    now: datetime | None = None,
) -> PromotionResult:
    """Promote a store record to a memory entry.

    Args:
        state: Loaded :class:`State`; ``state.memory_index`` is mutated in place.
        source_store_path: Path to the JSONL store containing the source record.
        source_id: ID of the source record.
        memory_path: Destination ``memory.jsonl`` path.
        scope_id: Optional override; defaults to the source record's
            ``scope_id`` (and falls back to ``"unscoped"`` when null).
        confidence: Confidence assigned to the new memory entry.
        now: Override for the current time (for tests).

    Returns:
        :class:`PromotionResult` carrying the new memory record and the source
        envelope ID.
    """
    src = _load_source(source_store_path, source_id)
    final_scope = scope_id or src.scope_id or "unscoped"
    body = _extract_body(src)
    title = src.summary if src.summary else source_id

    record = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id=final_scope,
        title=title,
        body=body,
        confidence=confidence,
        now=now,
    )
    logger.info(
        f"promote_record source={source_id} -> memory={record.summary.id} scope={final_scope}"
    )
    return PromotionResult(record=record, source_store_record_id=source_id)


def supersede(
    *,
    state: State,
    memory_path: Path,
    old_id: str,
    new_id: str,
) -> None:
    """Mark *old_id* superseded; *new_id* becomes the active replacement.

    Both entries must already exist in ``state.memory_index``. The old entry's
    ``status`` flips to :class:`MemoryStatus.SUPERSEDED`; ``memory.jsonl`` is
    not rewritten — supersession is captured by the cache flip plus compaction
    metadata when ``memory compact`` runs.
    """
    index = state.memory_index or {}
    if old_id not in index:
        raise PromotionError(f"memory entry {old_id!r} not in state.memory_index")
    if new_id not in index:
        raise PromotionError(f"memory entry {new_id!r} not in state.memory_index")
    old = index[old_id]
    index[old_id] = old.model_copy(update={"status": MemoryStatus.SUPERSEDED})
    state.memory_index = index
    logger.info(f"supersede old={old_id} new={new_id}")
