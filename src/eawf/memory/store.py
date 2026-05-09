"""Memory store writer: append-only ``memory.jsonl`` + ``state.memory_index`` cache.

Authority model:
- ``memory.jsonl`` is the **source of truth** for memory entries (full body and
  supersede history).
- ``state.memory_index`` is a derived cache mirroring the latest record per ID
  for fast ``memory list`` lookups.

The :func:`add_memory` helper writes the JSONL record first, fsyncs it, then
mirrors a :class:`MemorySummary` into the typed state. Callers that hold
``portalock`` on ``state.json`` must also acquire it on ``memory.jsonl`` before
touching the file (``portalock.acquire`` is reentrant per-target via the
sibling-lock convention).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from eawf.state.enums import Confidence, MemoryStatus, StoreKind
from eawf.state.models import MemorySummary, State
from eawf.store.append import append_envelope as _append_canonical
from eawf.store.envelope import Envelope
from eawf.store.kinds.memory import MemoryPayload

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemoryRecord:
    """Composite return value: the JSONL envelope + derived state summary."""

    envelope: Envelope
    summary: MemorySummary


def _summary_text(title: str, body: str) -> str:
    """Compose a short ``summary`` line from title + body for the envelope."""
    head = title.strip()
    tail = body.strip().splitlines()[0] if body.strip() else ""
    text = f"{head}: {tail}" if tail else head
    if len(text) > 480:
        text = text[:477] + "..."
    return text


def content_hash(scope_id: str, title: str, body: str) -> str:
    """Stable SHA-256 over scope+title+body — used by compaction dedup."""
    h = hashlib.sha256()
    h.update(scope_id.encode("utf-8"))
    h.update(b"\x1f")
    h.update(title.encode("utf-8"))
    h.update(b"\x1f")
    h.update(body.encode("utf-8"))
    return h.hexdigest()


def _next_memory_id(memory_path: Path, existing: dict[str, MemorySummary] | None) -> str:
    """Allocate a new ``MEM-<UTC-date>-<NN>`` id avoiding collisions."""
    today = datetime.now(UTC).strftime("%Y%m%d")
    used: set[int] = set()
    if existing:
        for mid in existing:
            if mid.startswith(f"MEM-{today}-"):
                try:
                    used.add(int(mid.rsplit("-", 1)[-1]))
                except ValueError:
                    continue
    if memory_path.exists():
        for line in memory_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                env = Envelope.model_validate_json(line)
            except Exception:
                continue
            if env.id.startswith(f"MEM-{today}-"):
                try:
                    used.add(int(env.id.rsplit("-", 1)[-1]))
                except ValueError:
                    continue
    for n in range(1, 100):
        if n not in used:
            return f"MEM-{today}-{n:02d}"
    raise ValueError("memory id allocation saturated for today")


def append_envelope(memory_path: Path, env: Envelope) -> None:
    """Append a single :class:`Envelope` to *memory_path* under sibling lock.

    Thin compatibility wrapper around
    :func:`eawf.store.append.append_envelope`; the underlying writer is the
    single canonical helper.
    """
    _append_canonical(memory_path, env)
    logger.info(f"memory store appended id={env.id} at {memory_path}")


def add_memory(
    *,
    state: State,
    memory_path: Path,
    scope_id: str,
    title: str,
    body: str,
    confidence: Confidence = Confidence.MEDIUM,
    review_due: datetime | None = None,
    now: datetime | None = None,
) -> MemoryRecord:
    """Add a memory entry: append to ``memory.jsonl`` then mirror into state cache.

    The state object is mutated in place: ``state.memory_index`` is created if
    missing and the new ``MemorySummary`` is inserted under its allocated ID.
    Callers must validate + atomically write ``state`` themselves.
    """
    moment = now if now is not None else datetime.now(UTC)
    mid = _next_memory_id(memory_path, state.memory_index)
    summary_text = _summary_text(title, body)

    payload = MemoryPayload(body=body, confidence=confidence, review_due=review_due)
    env = Envelope(
        id=mid,
        kind=StoreKind.MEMORY,
        scope_id=scope_id,
        created_at=moment,
        updated_at=None,
        summary=summary_text,
        payload=payload.model_dump(mode="json"),
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(memory_path, env)

    summary = MemorySummary(
        id=mid,
        scope_id=scope_id,
        summary=summary_text,
        confidence=confidence,
        status=MemoryStatus.ACTIVE,
        store_record_id=mid,
        review_due=review_due,
    )
    if state.memory_index is None:
        state.memory_index = {}
    state.memory_index[mid] = summary
    logger.info(f"add_memory id={mid} scope={scope_id} confidence={confidence.value}")
    return MemoryRecord(envelope=env, summary=summary)


def read_envelopes(memory_path: Path) -> list[Envelope]:
    """Read every record from ``memory.jsonl`` (returns []
    when the file is missing).
    """
    if not memory_path.exists():
        return []
    out: list[Envelope] = []
    for line in memory_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        out.append(Envelope.model_validate_json(line))
    return out


def find_envelope(memory_path: Path, mem_id: str) -> Envelope | None:
    """Return the **latest** envelope whose ``id == mem_id``.

    The JSONL file is append-only; later writes shadow earlier ones for the
    same ID. Returns ``None`` when no record matches.
    """
    latest: Envelope | None = None
    for env in read_envelopes(memory_path):
        if env.id == mem_id:
            latest = env
    return latest
