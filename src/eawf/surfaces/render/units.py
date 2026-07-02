"""Shared human-readable unit formatting for rendered surfaces.

One canonical home for compact numeric display so every surface (TUI
tiles, overlay tables, output tails, CLI renders) quotes the same shape
for the same magnitude. Machine-facing emitters (the Prometheus
telemetry exporter, JSON payloads) keep raw integers — this module is
for human eyes only.
"""

from __future__ import annotations

from datetime import UTC, datetime


def format_compact_utc(value: datetime) -> str:
    """Return the compact UTC form ``YYYY-MM-DD HH:MM:SS`` of *value*.

    One canonical operator-facing datetime shape: converted to UTC, no
    microseconds, no ``+00:00`` offset suffix — a full ISO ``isoformat()``
    render is machine precision that only widens tables and detail rows.
    A naive *value* is treated as already-UTC (state stamps are stored
    UTC); an aware one is converted before formatting.

    Args:
        value: The datetime to format.

    Returns:
        The compact ``YYYY-MM-DD HH:MM:SS`` display string.
    """
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def format_tokens(value: int) -> str:
    """Return a compact humanized token count (``340.2k``, ``1.2m``).

    Lowercase suffixes with one decimal place: values at or above one
    million read as ``m``, at or above one thousand as ``k``, and
    anything smaller renders raw so a small tally stays exact.

    Args:
        value: The non-negative token tally to format.

    Returns:
        The compact display string.
    """
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


__all__ = ["format_compact_utc", "format_tokens"]
