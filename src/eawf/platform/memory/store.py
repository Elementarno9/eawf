"""The epoch-1 memory store, read-only: ``memory.jsonl`` envelopes and summary text.

On an epoch-1 tree ``memory.jsonl`` holds every note's body and revision
history, with ``state.memory_index`` as its summary cache. Nothing writes it
any more -- notes are filed on the generation's memory ledger by the
daemon's ``memory.*`` verbs -- but an epoch-1 tree's notes are still read
from it, and the cutover imports it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from eawf.kernel.store.envelope import Envelope


def summary_text(title: str, body: str) -> str:
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
