"""``memory`` statusline module — memory entry count + total size.

Reads ``state.memory_index`` (cache projection) for the entry count and
sums the byte size of ``store/memory.jsonl`` for the total. Output is
``mem:<count>@<bytes>``. Missing state collapses to ``mem:-`` with
``status="missing"``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import orjson

from eawf.surfaces.render.statusline import StatuslineSegment

logger = logging.getLogger(__name__)


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


def build(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``mem:<count>@<size>`` (or ``mem:-``) segment.

    Args:
        claude_payload: Unused — kept for the uniform module signature.
        state_path: Resolved ``.ea/state.json`` path or ``None``.

    Returns:
        A :class:`StatuslineSegment` with ``module="memory"``. Status is
        ``ok`` when at least one memory entry was indexed, ``missing``
        otherwise.
    """
    del claude_payload  # accepted for uniform signature
    if state_path is None or not state_path.exists():
        return StatuslineSegment(module="memory", text="mem:-", status="missing")
    try:
        raw = state_path.read_bytes()
        payload = orjson.loads(raw)
    except (OSError, orjson.JSONDecodeError) as exc:
        logger.debug(f"build memory-read-decode-failed error={exc}")
        return StatuslineSegment(module="memory", text="mem:-", status="missing")
    if not isinstance(payload, dict):
        return StatuslineSegment(module="memory", text="mem:-", status="missing")
    count = _memory_count(payload)
    if count == 0:
        return StatuslineSegment(module="memory", text="mem:-", status="missing")
    size = _memory_size(state_path)
    return StatuslineSegment(
        module="memory",
        text=f"mem:{count}@{_format_bytes(size)}",
        status="ok",
    )


__all__ = ["build"]
