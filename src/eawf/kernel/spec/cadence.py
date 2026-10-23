"""The epoch-1 audit and gate cadences and the native values that replaced them.

Filed proofs in the receipt ledger still carry the epoch-1 values, so a stored
gate row reads through :data:`LEGACY_CADENCE_ALIASES`; the config migration
rewrites a layer once, and an authored profile check or audit document is
refused by :func:`refuse_retired_cadence` with the value to write instead.
"""

from __future__ import annotations

from typing import Any, Final

LEGACY_CADENCE_ALIASES: Final[dict[str, str]] = {
    "every-wave": "every-task",
    "every-iter": "every-batch",
    "every-phase": "every-milestone",
}


def refuse_retired_cadence(value: Any) -> Any:
    """Refuse an epoch-1 cadence, naming the native value that replaced it.

    Args:
        value: The raw ``cadence`` value of an authored row.

    Returns:
        *value* unchanged when it is not a retired cadence, for the
        cadence Literal to check.

    Raises:
        ValueError: *value* is ``every-wave``, ``every-iter`` or ``every-phase``.
    """
    if isinstance(value, str) and value in LEGACY_CADENCE_ALIASES:
        raise ValueError(f"cadence {value!r} is retired; write {LEGACY_CADENCE_ALIASES[value]!r}")
    return value


__all__ = ["LEGACY_CADENCE_ALIASES", "refuse_retired_cadence"]
