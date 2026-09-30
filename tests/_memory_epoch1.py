"""Lay down memory notes the way an epoch-1 tree held them.

An epoch-1 tree kept each note twice: a :class:`MemorySummary` in
``state.memory_index`` and an envelope in ``memory.jsonl``. Nothing writes
that shape any more, but its readers -- the dispatch context and the
epoch-1 read path -- still take it, so their tests build it here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from eawf.kernel.state.enums import Confidence, MemoryStatus, StoreKind
from eawf.kernel.state.models import MemorySummary, State
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.memory import MemoryPayload
from eawf.platform.memory.book import next_note_id
from eawf.platform.memory.store import read_envelopes, summary_text


@dataclass(frozen=True)
class Epoch1Note:
    """The two halves of one epoch-1 note."""

    envelope: Envelope
    summary: MemorySummary


def add_epoch1_note(
    *,
    state: State,
    memory_path: Path,
    scope_id: str,
    title: str,
    body: str,
    confidence: Confidence = Confidence.MEDIUM,
    review_due: datetime | None = None,
    now: datetime | None = None,
) -> Epoch1Note:
    """Append one note's envelope to *memory_path* and index it in *state*."""
    moment = now if now is not None else datetime.now(UTC)
    taken = [*(state.memory_index or {}), *(env.id for env in read_envelopes(memory_path))]
    mid = next_note_id(taken, now=moment)
    text = summary_text(title, body)
    env = Envelope(
        id=mid,
        kind=StoreKind.MEMORY,
        scope_id=scope_id,
        created_at=moment,
        summary=text,
        payload=MemoryPayload(body=body, confidence=confidence, review_due=review_due).model_dump(
            mode="json"
        ),
    )
    append_envelope(memory_path, env)
    summary = MemorySummary(
        id=mid,
        scope_id=scope_id,
        summary=text,
        confidence=confidence,
        status=MemoryStatus.ACTIVE,
        store_record_id=mid,
        review_due=review_due,
    )
    state.memory_index = {**(state.memory_index or {}), mid: summary}
    return Epoch1Note(envelope=env, summary=summary)
