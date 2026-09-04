"""Tests for :mod:`eawf.surfaces.render.units` (shared humanizers).

Pins the two canonical operator-facing display shapes: the compact token
count (``352.1k``) and the compact UTC datetime (``YYYY-MM-DD HH:MM:SS``
-- converted to UTC, no microseconds, no ``+00:00`` offset).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from eawf.surfaces.render.units import format_compact_utc, format_tokens

# --- format_compact_utc -----------------------------------------------------


def test_format_compact_utc_aware_utc_datetime() -> None:
    value = datetime(2026, 7, 2, 15, 4, 5, tzinfo=UTC)
    assert format_compact_utc(value) == "2026-07-02 15:04:05"


def test_format_compact_utc_non_utc_aware_converts() -> None:
    plus_two = timezone(timedelta(hours=2))
    value = datetime(2026, 7, 2, 17, 4, 5, tzinfo=plus_two)
    assert format_compact_utc(value) == "2026-07-02 15:04:05"


def test_format_compact_utc_naive_treated_as_utc() -> None:
    value = datetime(2026, 7, 2, 15, 4, 5)
    assert format_compact_utc(value) == "2026-07-02 15:04:05"


def test_format_compact_utc_drops_microseconds_and_offset() -> None:
    value = datetime(2026, 7, 2, 15, 4, 5, 123456, tzinfo=UTC)
    rendered = format_compact_utc(value)
    assert "." not in rendered
    assert "+00:00" not in rendered
    assert rendered == "2026-07-02 15:04:05"


# --- format_tokens ----------------------------------------------------------


def test_format_tokens_small_value_renders_raw() -> None:
    assert format_tokens(0) == "0"
    assert format_tokens(999) == "999"


def test_format_tokens_thousands_suffix() -> None:
    assert format_tokens(1_000) == "1.0k"
    assert format_tokens(352_107) == "352.1k"


def test_format_tokens_millions_suffix() -> None:
    assert format_tokens(1_000_000) == "1.0m"
    assert format_tokens(2_450_000) == "2.5m"
