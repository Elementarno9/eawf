"""``rate_window`` statusline module — the host's rate-limit windows.

Claude Code's statusline payload carries a ``rate_limits`` block keyed by
window, each window stating how much of it is used and when it resets.
:func:`bar_rows` gives the powerline one row per known window: ``5h`` from
``five_hour`` and ``7d`` from ``seven_day``, each with its pace, the marker at
an even spend's share and the time left until its reset. Each window is read
on its own: a window the block omits, or states without a usable fraction,
has no row, and a reset time that cannot be read blanks only that row's pace,
marker and countdown, so one bad window never hides another.

The used share is read from ``used_percentage`` (0-100) or ``utilization``
(0-1); the reset from ``resets_at`` as epoch seconds, epoch milliseconds or an
ISO-8601 string.

Pace is how far the used share runs ahead of an even spend across the window:
with ``L`` the window's length, ``expected = 100 * clamp(L - (resets_at -
now), 0, L) / L`` and ``pace = round_half_up(used - expected)``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from eawf.surfaces.render.statusline import PowerlineBar, format_countdown, round_half_up

_BLOCK = "rate_limits"

#: The windows the powerline draws, by host key, with the row label and the
#: window's length in seconds.
_BAR_WINDOWS: Final = (("five_hour", "5h", 18_000), ("seven_day", "7d", 604_800))

#: Epoch seconds this large lie past the year 5000, while epoch milliseconds
#: pass it already in 1973, so a timestamp from here up is read as milliseconds.
_MILLISECONDS_FROM: Final = 100_000_000_000

#: Below this expected share a window has barely started, and a few early
#: calls would read as a large overspend, so the pace prints as even.
_SETTLING_PERCENT: Final = 2.0


def _used_percent(window: dict[str, Any]) -> float | None:
    """Return the used share in percent a window states, or ``None`` when it states none."""
    percent = window.get("used_percentage")
    if isinstance(percent, int | float) and not isinstance(percent, bool) and 0 <= percent <= 100:
        return float(percent)
    ratio = window.get("utilization")
    if isinstance(ratio, int | float) and not isinstance(ratio, bool) and 0 <= ratio <= 1:
        return float(ratio) * 100
    return None


def _reset_seconds(raw: object) -> float | None:
    """Return a ``resets_at`` value as Unix seconds, or ``None`` when it is unknown.

    A number of at least 1e11 is milliseconds and a smaller one seconds; a
    string is ISO-8601, naive meaning UTC. Anything else, and a time outside
    the platform's range, is unknown.
    """
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        seconds = raw / 1000 if raw >= _MILLISECONDS_FROM else raw
        try:
            # Rejects NaN, infinities and times the platform cannot represent.
            datetime.fromtimestamp(seconds, tz=UTC)
        except OverflowError, OSError, ValueError:
            return None
        return float(seconds)
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return (parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)).timestamp()
    return None


def _pace(used: float, expected: float, remaining: float, length: int) -> int:
    """Return the points *used* runs ahead of *expected*, ``0`` where pace means nothing.

    A passed reset, a reset further away than the window is long, and the
    window's first two percent all read as on pace.
    """
    if remaining <= 0 or remaining > length or expected < _SETTLING_PERCENT:
        return 0
    return round_half_up(used - expected)


def bar_rows(claude_payload: dict[str, Any], now: datetime) -> list[PowerlineBar]:
    """Return the ``5h`` and ``7d`` bar rows for the windows the host reports.

    Args:
        claude_payload: Decoded Claude stdin JSON.
        now: The render time the pace and the reset countdown count from.

    Returns:
        One row per window with a usable fraction, ``5h`` before ``7d``; a
        window without a readable reset time has no pace, marker or detail,
        and one already past its reset is on pace and counts down from
        ``0m``. Empty when the block is absent.
    """
    block = claude_payload.get(_BLOCK)
    if not isinstance(block, dict):
        return []
    rows: list[PowerlineBar] = []
    for key, label, length in _BAR_WINDOWS:
        window = block.get(key)
        if not isinstance(window, dict) or (used := _used_percent(window)) is None:
            continue
        reset = _reset_seconds(window.get("resets_at"))
        if reset is None:
            rows.append(PowerlineBar(label=label, ratio=used / 100))
            continue
        remaining = reset - now.timestamp()
        expected = 100 * min(max(length - remaining, 0), length) / length
        rows.append(
            PowerlineBar(
                label=label,
                ratio=used / 100,
                detail=format_countdown(max(0.0, remaining)),
                pace=_pace(used, expected, remaining, length),
                expected=expected,
            )
        )
    return rows


__all__ = ["bar_rows"]
