"""``context_tokens`` statusline module — context-window occupancy.

Claude Code's statusline payload carries the last call's usage under
``context_window.current_usage`` and the window size under
``context_window.context_window_size``. :func:`bar_row` draws the tokens the
context holds (input plus both cache classes) as the powerline's ``ctx`` row:
the used share of the window as a bar, with ``<used>/<size>`` such as
``64K/1.0M`` in place of a reset time.

The module reads only the host payload — no transcript, no on-disk lookup —
so the row is cheap and never disagrees with what the host shows.
"""

from __future__ import annotations

from typing import Any

from eawf.runtime.runtimes.claude.runtime_counters import parse_runtime_counters
from eawf.surfaces.render.statusline import PowerlineBar, format_context_tokens

_LABEL = "ctx"


def _window_size(claude_payload: dict[str, Any]) -> int | None:
    """Return the host's context-window size, or ``None`` when it names none."""
    window = claude_payload.get("context_window")
    size = window.get("context_window_size") if isinstance(window, dict) else None
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return None
    return size


def bar_row(claude_payload: dict[str, Any]) -> PowerlineBar | None:
    """Return the ``ctx`` bar row, or ``None`` when the host states no occupancy.

    Args:
        claude_payload: Decoded Claude stdin JSON.

    Returns:
        The row with the used share of the window, capped at full, and
        ``<used>/<size>`` as its detail; ``None`` when the host reports no
        window size or not all three input classes, since a missing class is
        not a zero and a share needs both sides.
    """
    counters = parse_runtime_counters(claude_payload)
    size = _window_size(claude_payload)
    if counters is None or size is None:
        return None
    classes = (
        counters.input_tokens,
        counters.cache_creation_input_tokens,
        counters.cache_read_input_tokens,
    )
    if any(count is None for count in classes):
        return None
    used = sum(count for count in classes if count is not None)
    return PowerlineBar(
        label=_LABEL,
        ratio=min(1.0, used / size),
        detail=f"{format_context_tokens(used)}/{format_context_tokens(size)}",
    )


__all__ = ["bar_row"]
