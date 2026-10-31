"""Tests for :mod:`eawf.surfaces.render.units` (shared humanizers).

Pins the canonical operator-facing compact token count (``352.1k``).
"""

from __future__ import annotations

from eawf.surfaces.render.units import format_tokens

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
