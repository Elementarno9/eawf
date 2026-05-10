"""Pure EU (Estimation Unit) calculator.

Per ``docs/architecture/state-model.md`` "Estimation model": one ``EU``
is 30 minutes of active operator+agent session time by default
(``estimation.eu_minutes``). Estimates derive ``expected_eu`` and
``pessimistic_eu`` by multiplying a *raw_minutes* baseline by
``central_multiplier`` / ``pessimistic_multiplier`` from the configured
reference class, then converting minutes to EU by dividing by ``eu_minutes``.

Everything below uses :class:`decimal.Decimal` for exactness — float arithmetic
is forbidden because the property tests round-trip multiplication and division
across the whole layer and a single binary-floating-point rounding error breaks
them.

The CLI/state-layer interface still exposes :class:`float` because the schema
in :mod:`eawf.state.models` (:class:`EstimateSummary`) is locked to
``float``. Conversions back to :class:`float` happen only at the very edge,
via :func:`to_float`.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal


def as_decimal(value: Decimal | float | int | str) -> Decimal:
    """Coerce *value* to :class:`Decimal` without losing precision.

    :class:`float` inputs are routed through ``str()`` to avoid importing
    binary-floating-point error into the Decimal layer (e.g. ``Decimal(0.1)``
    yields ``Decimal('0.1000000000000000055511151231257827021181583404541015625')``,
    while ``Decimal(str(0.1))`` yields ``Decimal('0.1')``).

    Public helper because the segments + recovery modules also need the
    coercion at their schema boundaries.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


def expected_eu(
    raw_minutes: Decimal | float | int | str,
    central_multiplier: Decimal | float | int | str,
    eu_minutes: Decimal | float | int | str,
) -> Decimal:
    """Return the *expected* EU value.

    ``expected_eu = central_multiplier * raw_minutes / eu_minutes``

    Args:
        raw_minutes: Baseline duration in minutes for the scope (operator's
            naive estimate before the reference-class adjustment).
        central_multiplier: Reference-class central coefficient (e.g.
            ``0.50`` for ``core_swe`` per the v0.1 lockbox).
        eu_minutes: Minutes per EU (``estimation.eu_minutes`` config).

    Raises:
        ValueError: When ``eu_minutes`` is zero (division-by-zero guard).
    """
    raw = as_decimal(raw_minutes)
    cm = as_decimal(central_multiplier)
    eu_m = as_decimal(eu_minutes)
    if eu_m == 0:
        raise ValueError("eu_minutes must be non-zero")
    return cm * raw / eu_m


def pessimistic_eu(
    raw_minutes: Decimal | float | int | str,
    pessimistic_multiplier: Decimal | float | int | str,
    eu_minutes: Decimal | float | int | str,
) -> Decimal:
    """Return the *pessimistic* EU value.

    ``pessimistic_eu = pessimistic_multiplier * raw_minutes / eu_minutes``

    Raises:
        ValueError: When ``eu_minutes`` is zero.
    """
    raw = as_decimal(raw_minutes)
    pm = as_decimal(pessimistic_multiplier)
    eu_m = as_decimal(eu_minutes)
    if eu_m == 0:
        raise ValueError("eu_minutes must be non-zero")
    return pm * raw / eu_m


def quantize(
    value: Decimal | float | int | str,
    eu_quantum: Decimal | float | int | str,
) -> Decimal:
    """Round *value* to the nearest multiple of *eu_quantum*.

    Uses :data:`decimal.ROUND_HALF_EVEN` ("banker's rounding") so that exactly
    half-quantum values round to the nearest even multiple. This matches the
    default Python rounding mode for the ``Decimal`` constructor and keeps the
    CLI deterministic across runs.

    Args:
        value: The unrounded EU value (typically the output of
            :func:`expected_eu` or :func:`pessimistic_eu`).
        eu_quantum: The grain to round to (``display.eu_quantum``).

    Raises:
        ValueError: When ``eu_quantum`` is zero or negative.
    """
    v = as_decimal(value)
    q = as_decimal(eu_quantum)
    if q <= 0:
        raise ValueError("eu_quantum must be positive")
    # Snap to nearest multiple of q: round(v / q) * q.
    multiples = (v / q).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
    return multiples * q


def to_float(value: Decimal) -> float:
    """Convert a Decimal back to float at the schema boundary.

    The state schema (:class:`eawf.state.models.EstimateSummary`) stores
    ``expected_eu`` / ``pessimistic_eu`` / ``elapsed_eu`` as ``float``; this
    helper makes the conversion explicit and easy to grep for.
    """
    return float(value)


def render_display(
    expected: Decimal,
    pessimistic: Decimal,
    *,
    eu_minutes: Decimal | float | int | str,
) -> str:
    """Render the canonical estimate display string.

    Format::

        "<expected_eu> EU · exp ~<expected_minutes>m · pess ~<pessimistic_minutes>m"

    Minutes are computed as ``eu_value * eu_minutes`` and rendered as
    integer-rounded values for human-friendly display. The full-precision EU
    figures (already quantized) come through unchanged.
    """
    eu_m = as_decimal(eu_minutes)
    exp_min = (expected * eu_m).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
    pess_min = (pessimistic * eu_m).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
    return f"{expected} EU · exp ~{exp_min}m · pess ~{pess_min}m"
