"""``rate_window`` statusline module — the host's rate-limit windows.

Claude Code's statusline payload carries a ``rate_limits`` block keyed by
window, each window stating how much of it is used and when it resets. The
segment renders one bar per window with its used percentage and reset time,
and each window is read on its own: a window the block omits, or states
without a usable fraction, is left out rather than drawn as zero, so one
missing window never hides the others.

The used fraction is read from ``used_percentage`` (0-100) or ``utilization``
(0-1); the reset from ``resets_at`` as epoch seconds or an ISO-8601 string.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eawf.kernel.projection.truth import Precision
from eawf.runtime.runtimes.claude.statusline_modules._host import host_source
from eawf.surfaces.render.statusline import (
    RateWindow,
    StatuslineSegment,
    rate_window_segment,
    unavailable_segment,
)

logger = logging.getLogger(__name__)

_MODULE = "rate_window"
_LABEL = "rate"
_BLOCK = "rate_limits"
# The host states the fraction; the rendered bar rounds it to an eighth of a cell.
_SOURCE = host_source(_BLOCK, precision=Precision.APPROXIMATE)


def _fraction(window: dict[str, Any]) -> float | None:
    """Return the used fraction a window states, or ``None`` when it states none."""
    percent = window.get("used_percentage")
    if isinstance(percent, int | float) and not isinstance(percent, bool) and 0 <= percent <= 100:
        return float(percent) / 100
    ratio = window.get("utilization")
    if isinstance(ratio, int | float) and not isinstance(ratio, bool) and 0 <= ratio <= 1:
        return float(ratio)
    return None


def _reset(window: dict[str, Any]) -> datetime | None:
    """Return when a window resets, or ``None`` when it names no usable time."""
    raw = window.get("resets_at")
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return datetime.fromtimestamp(raw, tz=UTC)
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def build(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``rate:`` segment, or the marker naming why it has no window.

    Args:
        claude_payload: Decoded Claude stdin JSON.
        state_path: Unused — the module reads only from the host payload.

    Returns:
        One bar per window the host reported with a usable fraction;
        ``rate:n/a(no-rate-limits)`` when the block is absent, and
        ``rate:n/a(no-usable-window)`` when no window states a fraction.
    """
    del state_path  # accepted for uniform signature
    block = claude_payload.get(_BLOCK)
    if not isinstance(block, dict) or not block:
        return unavailable_segment(_MODULE, _LABEL, "no-rate-limits", _SOURCE)
    windows = [
        RateWindow(label=str(name), ratio=fraction, resets_at=_reset(window))
        for name, window in block.items()
        if isinstance(window, dict) and (fraction := _fraction(window)) is not None
    ]
    if not windows:
        return unavailable_segment(_MODULE, _LABEL, "no-usable-window", _SOURCE)
    return rate_window_segment(windows, _SOURCE)


__all__ = ["build"]
