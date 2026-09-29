"""Partition a Run's window of a Claude transcript into measured spans.

Every gap between two consecutive stamped rows is spent waiting on
whatever wrote the second row. When that row belongs to an assistant
message, the model was answering a request -- a reasoning request when the
message carries a thinking block, a plain one otherwise -- and because the
gap is closed by the message's own rows, a message's span runs to its last
row, not its first. When the second row carries a tool result, a tool was
running. Anything else -- an operator prompt, a system row -- was idle.

The gaps tile the window exactly, so the phase totals add up to the time
the transcript covers. Time inside the Run's window that no row pair
covers -- before its first row in the window or after its last -- has no
producer that saw it, and is returned as unattributed rather than filed
under ``idle``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any, Final

from eawf.kernel.state.epoch2.measurement import MeasuredSpan, SpanPhase

logger = logging.getLogger(__name__)

#: The producer name every span this module measures is attributed to.
TRANSCRIPT_SPAN_PRODUCER: Final = "claude-transcript"

#: Why the uncovered edges of a Run's window carry no producer.
_UNCOVERED_REASON: Final = "no transcript row covers the interval"

#: Content-block types that mean the model reasoned before answering.
_THINKING_BLOCKS: Final = frozenset({"thinking", "redacted_thinking"})


def _stamp(row: dict[str, Any]) -> datetime | None:
    """Return the row's timestamp as an aware UTC instant, else ``None``."""
    raw = row.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp


def _blocks(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the row's message content blocks, or an empty list."""
    message = row.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _message_id(row: dict[str, Any]) -> str | None:
    """Return the assistant message id *row* belongs to, else ``None``."""
    message = row.get("message")
    if row.get("type") != "assistant" or not isinstance(message, dict):
        return None
    ident = message.get("id")
    return ident if isinstance(ident, str) and ident else None


def _reasoning_messages(rows: Sequence[dict[str, Any]]) -> frozenset[str]:
    """Return the ids of the assistant messages that carry a thinking block."""
    return frozenset(
        ident
        for row in rows
        if (ident := _message_id(row)) is not None
        and any(block.get("type") in _THINKING_BLOCKS for block in _blocks(row))
    )


def _phase_closed_by(row: dict[str, Any], reasoning: frozenset[str]) -> SpanPhase:
    """Return what the gap ending at *row* was spent waiting on."""
    if row.get("type") == "assistant":
        ident = _message_id(row)
        if ident is not None and ident in reasoning:
            return SpanPhase.REASONING_REQUEST
        return SpanPhase.PLAIN_REQUEST
    if any(block.get("type") == "tool_result" for block in _blocks(row)):
        return SpanPhase.TOOL_CALL
    return SpanPhase.IDLE


def _unattributed(start: datetime, end: datetime) -> MeasuredSpan:
    """Return an uncovered stretch of the window as an unattributed span."""
    return MeasuredSpan(
        phase=SpanPhase.IDLE, started_at=start, ended_at=end, unattributed_reason=_UNCOVERED_REASON
    )


def transcript_spans(
    rows: Sequence[dict[str, Any]], *, window_start: datetime, window_end: datetime
) -> tuple[MeasuredSpan, ...]:
    """Return the spans tiling ``[window_start, window_end]`` of *rows*.

    Args:
        rows: The transcript's rows in written order.
        window_start: When the Run started.
        window_end: When the Run stopped.

    Returns:
        The attributed spans between stamped rows, clipped to the window,
        with an unattributed span for each uncovered edge. Empty only when
        the window itself is empty.

    Raises:
        ValueError: The window ends before it starts.
    """
    if window_end < window_start:
        raise ValueError("a Run's window cannot end before it starts")
    if window_end == window_start:
        return ()
    reasoning = _reasoning_messages(rows)
    stamped = [(stamp, row) for row in rows if (stamp := _stamp(row)) is not None]
    spans: list[MeasuredSpan] = []
    for (earlier, _), (later, row) in pairwise(stamped):
        start, end = max(earlier, window_start), min(later, window_end)
        if end <= start:
            continue
        spans.append(
            MeasuredSpan(
                phase=_phase_closed_by(row, reasoning),
                started_at=start,
                ended_at=end,
                producer=TRANSCRIPT_SPAN_PRODUCER,
            )
        )
    covered_from = min((span.started_at for span in spans), default=window_end)
    covered_to = max((span.ended_at for span in spans), default=window_end)
    edges = [(window_start, covered_from), (covered_to, window_end)]
    unattributed = [_unattributed(start, end) for start, end in edges if end > start]
    logger.debug(
        f"transcript_spans rows={len(stamped)} attributed={len(spans)} "
        f"unattributed={len(unattributed)}"
    )
    return (*unattributed, *spans)


__all__ = ["TRANSCRIPT_SPAN_PRODUCER", "transcript_spans"]
