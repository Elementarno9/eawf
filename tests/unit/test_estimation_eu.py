"""Tests for the pure EU calculator (``eawf.estimation.eu``).

Covers happy path + boundary cases (zero raw_minutes, very large, very small,
quantization rounding, banker's rounding tie-breaks) + error paths
(zero ``eu_minutes``, non-positive ``eu_quantum``).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from eawf.estimation import eu


def test_expected_eu_matches_formula() -> None:
    """expected_eu = central_multiplier * raw_minutes / eu_minutes (proposal §8)."""
    result = eu.expected_eu(60, Decimal("0.50"), 30)
    assert result == Decimal("1.0")


def test_pessimistic_eu_matches_formula() -> None:
    """pessimistic_eu = pessimistic_multiplier * raw_minutes / eu_minutes."""
    result = eu.pessimistic_eu(60, Decimal("1.8"), 30)
    assert result == Decimal("3.6")


def test_expected_eu_decimal_inputs_preserve_precision() -> None:
    """Decimal inputs avoid binary-floating-point drift."""
    result = eu.expected_eu(Decimal("60"), Decimal("0.5"), Decimal("30"))
    assert result == Decimal("1.00")


def test_expected_eu_zero_raw_minutes_is_zero() -> None:
    """Boundary: zero baseline yields zero EU."""
    assert eu.expected_eu(0, Decimal("0.5"), 30) == Decimal("0")


def test_expected_eu_very_large_raw_minutes() -> None:
    """Large inputs do not overflow."""
    huge = Decimal("10") ** 12  # 1e12 minutes
    result = eu.expected_eu(huge, Decimal("0.5"), 30)
    assert result == huge * Decimal("0.5") / Decimal("30")


def test_expected_eu_very_small_central_multiplier() -> None:
    """Tiny multipliers retain precision unlike float."""
    result = eu.expected_eu(Decimal("30"), Decimal("0.0001"), Decimal("30"))
    assert result == Decimal("0.0001")


def test_expected_eu_zero_eu_minutes_raises() -> None:
    """Division-by-zero guard."""
    with pytest.raises(ValueError, match="eu_minutes"):
        eu.expected_eu(60, Decimal("0.5"), 0)


def test_pessimistic_eu_zero_eu_minutes_raises() -> None:
    with pytest.raises(ValueError, match="eu_minutes"):
        eu.pessimistic_eu(60, Decimal("1.8"), 0)


def test_quantize_rounds_to_nearest_quantum() -> None:
    """0.30 EU snaps to 0.25 EU under a 0.25 quantum (banker's: 0.30 -> 0.25)."""
    assert eu.quantize(Decimal("0.30"), Decimal("0.25")) == Decimal("0.25")


def test_quantize_does_not_change_already_aligned_value() -> None:
    """0.50 is exactly on the 0.25 grid."""
    assert eu.quantize(Decimal("0.50"), Decimal("0.25")) == Decimal("0.50")


def test_quantize_banker_rounds_halfway_to_even() -> None:
    """0.125 rounds to 0.0 (down to nearest even multiple of 0.25)."""
    assert eu.quantize(Decimal("0.125"), Decimal("0.25")) == Decimal("0.00")


def test_quantize_banker_rounds_other_halfway_to_even() -> None:
    """0.375 rounds to 0.50 (nearest even multiple of 0.25)."""
    assert eu.quantize(Decimal("0.375"), Decimal("0.25")) == Decimal("0.50")


def test_quantize_zero_quantum_raises() -> None:
    with pytest.raises(ValueError, match="eu_quantum"):
        eu.quantize(Decimal("1.0"), Decimal("0"))


def test_quantize_negative_quantum_raises() -> None:
    with pytest.raises(ValueError, match="eu_quantum"):
        eu.quantize(Decimal("1.0"), Decimal("-0.25"))


def test_quantize_handles_negative_value() -> None:
    """Quantization of a negative EU value (theoretical) lands on a 0.25 grid."""
    assert eu.quantize(Decimal("-0.30"), Decimal("0.25")) == Decimal("-0.25")


def test_quantize_accepts_float_value() -> None:
    """Float inputs are routed through str() to avoid binary drift."""
    # 0.1 has a long binary expansion; via str() it becomes Decimal('0.1') exactly.
    assert eu.quantize(0.30, Decimal("0.25")) == Decimal("0.25")


def test_quantize_float_eu_quantum_via_str() -> None:
    assert eu.quantize(Decimal("0.30"), 0.25) == Decimal("0.25")


def test_render_display_format() -> None:
    """The display string follows the proposal example shape."""
    text = eu.render_display(Decimal("2.0"), Decimal("6.0"), eu_minutes=Decimal("30"))
    assert text == "2.0 EU · exp ~60m · pess ~180m"


def test_to_float_round_trips() -> None:
    """to_float exposes the boundary conversion explicitly."""
    assert eu.to_float(Decimal("1.25")) == 1.25


# ---- Property-style round-trip on the EU formula -----------------------------


def test_expected_eu_formula_inverse_via_minutes() -> None:
    """expected_minutes = expected_eu * eu_minutes (round-trip)."""
    raw = Decimal("75")
    cm = Decimal("0.50")
    eu_m = Decimal("30")
    expected = eu.expected_eu(raw, cm, eu_m)
    expected_minutes = expected * eu_m
    # cm * raw is the implied minutes baseline after the central multiplier.
    assert expected_minutes == cm * raw
