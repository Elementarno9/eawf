"""``memory`` statusline module — memory entry count + total size.

On an epoch-1 tree, reads ``state.memory_index`` (cache projection) for the
entry count and the byte size of ``store/memory.jsonl`` for the total. On an
epoch-2 tree the cutover moved every memory note into the selected
generation's memory ledger, so the count is the ledger's distinct records
and the size is the ledger's. Output is ``mem:<count>@<bytes>``. An
unreadable source, or no memory at all, renders ``mem:n/a(<reason>)`` with
``status="missing"``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from eawf.kernel.projection.truth import TruthKind
from eawf.kernel.store.ledger import LedgerError, effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.runtimes.claude.statusline_modules._document import (
    DocumentGap,
    document_source,
    read_legacy_document,
)
from eawf.runtime.runtimes.claude.statusline_modules._spine import selected_generation
from eawf.surfaces.render.statusline import (
    SegmentSource,
    StatuslineSegment,
    sourced_segment,
    unavailable_segment,
)

logger = logging.getLogger(__name__)

_MODULE = "memory"
_LABEL = "mem"
_SOURCE = document_source("memory_index")

#: The producer an epoch-2 count names: the generation's memory ledger.
LEDGER_PRODUCER: Final = "eawf.epoch2-memory-ledger"
_LEDGER_SOURCE = SegmentSource(
    producer=LEDGER_PRODUCER,
    provenance=f"generation#ledger/{Epoch2Collection.MEMORY.value}.jsonl",
    truth_kind=TruthKind.STORED,
)


def _format_bytes(num: int) -> str:
    """Render *num* bytes as a short string (B / KiB / MiB)."""
    if num < 1024:
        return f"{num}B"
    if num < 1024 * 1024:
        return f"{num // 1024}KiB"
    return f"{num // (1024 * 1024)}MiB"


def _memory_count(payload: dict[str, Any]) -> int:
    """Return the number of entries in ``state.memory_index`` (0 if absent)."""
    index = payload.get("memory_index")
    if isinstance(index, dict):
        return len(index)
    return 0


def _memory_size(state_path: Path) -> int:
    """Return the byte size of ``<state_dir>/store/memory.jsonl`` or 0."""
    memory_path = state_path.parent / "store" / "memory.jsonl"
    if not memory_path.exists():
        return 0
    try:
        return memory_path.stat().st_size
    except OSError as exc:
        logger.debug(f"_memory_size size-lookup-failed error={exc}")
        return 0


def _ledger_memory(state_path: Path) -> StatuslineSegment:
    """Return the memory segment the selected generation's memory ledger states."""
    document_path = selected_generation(state_path)
    if isinstance(document_path, str):
        return unavailable_segment(_MODULE, _LABEL, document_path, _LEDGER_SOURCE)
    path = ledger_path(document_path, Epoch2Collection.MEMORY)
    try:
        records = effective_records(read_ledger_records(path))
        size = path.stat().st_size if records else 0
    except (OSError, LedgerError, ValidationError) as exc:
        logger.debug(f"_ledger_memory memory-ledger-unreadable error={exc}")
        return unavailable_segment(_MODULE, _LABEL, "memory-ledger-unreadable", _LEDGER_SOURCE)
    count = len({record.record_key for record in records})
    if count == 0:
        return unavailable_segment(_MODULE, _LABEL, "no-memory-records", _LEDGER_SOURCE)
    return sourced_segment(_MODULE, _LABEL, f"{count}@{_format_bytes(size)}", _LEDGER_SOURCE)


def build(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``mem:<count>@<size>`` (or ``mem:n/a(<reason>)``) segment.

    Args:
        claude_payload: Unused — kept for the uniform module signature.
        state_path: Resolved ``.ea/state.json`` path or ``None``.

    Returns:
        A :class:`StatuslineSegment` with ``module="memory"``. Status is
        ``ok`` when at least one memory entry was indexed, ``missing``
        otherwise.
    """
    del claude_payload  # accepted for uniform signature
    if state_path is None:
        return unavailable_segment(_MODULE, _LABEL, DocumentGap.NO_STATE.value, _SOURCE)
    payload = read_legacy_document(state_path)
    if payload is DocumentGap.NO_EPOCH2_SOURCE:
        return _ledger_memory(state_path)
    if isinstance(payload, DocumentGap):
        return unavailable_segment(_MODULE, _LABEL, payload.value, _SOURCE)
    count = _memory_count(payload)
    if count == 0:
        return unavailable_segment(_MODULE, _LABEL, "no-memory-index", _SOURCE)
    size = _memory_size(state_path)
    return sourced_segment(_MODULE, _LABEL, f"{count}@{_format_bytes(size)}", _SOURCE)


__all__ = ["LEDGER_PRODUCER", "build"]
