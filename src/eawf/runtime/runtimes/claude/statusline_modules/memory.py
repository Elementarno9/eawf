"""``memory`` statusline module — the memory ledger's note count + size.

Every memory note lives in the selected generation's memory ledger, so the
count is the notes the ledger stands at (a correction replaces its note rather
than adding one) and the size is the ledger's. Output is
``mem:<count>@<bytes>``. No tree, a tree still in epoch 1, an unreadable
ledger, or no note at all renders ``mem:n/a(<reason>)`` with
``status="missing"``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from eawf.kernel.projection.truth import TruthKind
from eawf.kernel.store.ledger import LedgerError
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.runtimes.claude.statusline_modules._spine import (
    NO_STATE,
    selected_generation,
)
from eawf.surfaces.render.statusline import (
    SegmentSource,
    StatuslineSegment,
    sourced_segment,
    unavailable_segment,
)

logger = logging.getLogger(__name__)

_MODULE = "memory"
_LABEL = "mem"
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


def _ledger_memory(state_path: Path) -> StatuslineSegment:
    """Return the memory segment the selected generation's memory ledger states."""
    document_path = selected_generation(state_path)
    if isinstance(document_path, str):
        return unavailable_segment(_MODULE, _LABEL, document_path, _LEDGER_SOURCE)
    path = ledger_path(document_path, Epoch2Collection.MEMORY)
    from eawf.platform.memory.book import read_book

    try:
        count = len(read_book(path))
        size = path.stat().st_size if count else 0
    except (OSError, LedgerError, ValidationError) as exc:
        logger.debug(f"_ledger_memory memory-ledger-unreadable error={exc}")
        return unavailable_segment(_MODULE, _LABEL, "memory-ledger-unreadable", _LEDGER_SOURCE)
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
        ``ok`` when the ledger holds at least one note, ``missing`` otherwise.
    """
    del claude_payload  # accepted for uniform signature
    if state_path is None:
        return unavailable_segment(_MODULE, _LABEL, NO_STATE, _LEDGER_SOURCE)
    return _ledger_memory(state_path)


__all__ = ["LEDGER_PRODUCER", "build"]
