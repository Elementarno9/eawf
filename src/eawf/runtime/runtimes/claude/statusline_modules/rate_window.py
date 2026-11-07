"""``rate_window`` statusline module — the host's rate-limit windows.

Claude Code's statusline payload carries a ``rate_limits`` block keyed by
window, each window stating how much of it is used and when it resets.
:func:`bar_rows` gives the powerline one row per known window: ``5h`` from
``five_hour`` and ``7d`` from ``seven_day``, each with the time left until its
reset. Each window is read on its own: a window the block omits, or states
without a usable fraction, has no row, and a reset time that cannot be read
drops only that row's countdown, so one bad window never hides another.

The used fraction is read from ``used_percentage`` (0-100) or ``utilization``
(0-1); the reset from ``resets_at`` as epoch seconds, epoch milliseconds or an
ISO-8601 string.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from eawf.surfaces.render.statusline import PowerlineBar, format_countdown

_BLOCK = "rate_limits"

#: The windows the powerline draws, by host key, with the row label each takes.
_BAR_WINDOWS = (("five_hour", "5h"), ("seven_day", "7d"))

#: Epoch seconds this large lie past the year 5000, while epoch milliseconds
#: pass it already in 1973, so a timestamp from here up is read as milliseconds.
_MILLISECONDS_FROM: Final = 100_000_000_000


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
        seconds = raw / 1000 if abs(raw) >= _MILLISECONDS_FROM else raw
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except OverflowError, OSError, ValueError:
            # Out of the platform's time range, or NaN: no countdown to show.
            return None
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def bar_rows(claude_payload: dict[str, Any], now: datetime) -> list[PowerlineBar]:
    """Return the ``5h`` and ``7d`` bar rows for the windows the host reports.

    Args:
        claude_payload: Decoded Claude stdin JSON.
        now: The render time the reset countdown counts from.

    Returns:
        One row per window with a usable fraction, ``5h`` before ``7d``; a
        window without a readable reset time has no detail, and one already
        past its reset counts down from ``0m``. Empty when the block is absent.
    """
    block = claude_payload.get(_BLOCK)
    if not isinstance(block, dict):
        return []
    rows: list[PowerlineBar] = []
    for key, label in _BAR_WINDOWS:
        window = block.get(key)
        if not isinstance(window, dict) or (fraction := _fraction(window)) is None:
            continue
        reset = _reset(window)
        detail = (
            format_countdown(max(0.0, (reset - now).total_seconds())) if reset is not None else None
        )
        rows.append(PowerlineBar(label=label, ratio=fraction, detail=detail))
    return rows


__all__ = ["bar_rows"]
