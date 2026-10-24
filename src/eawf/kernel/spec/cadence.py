"""The epoch-1 audit and gate cadences and the native values that replaced them.

Filed proofs in the receipt ledger still carry the epoch-1 values, so a stored
gate row reads through :data:`LEGACY_CADENCE_ALIASES` and is written back as
stored by :class:`StoredCadenceRow`; the config migration
rewrites a layer once, and an authored profile check or audit document is
refused by :func:`refuse_retired_cadence` with the value to write instead.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, Field, field_serializer, model_validator

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


class StoredCadenceRow(BaseModel):
    """The ``cadence`` handling of a stored row that may predate the native values.

    An epoch-1 cadence reads as its native value, and the row is written back
    with the spelling it was stored with, so the digests of a gate filed before
    the rename, and the receipts keyed on them, do not move.

    Attributes:
        stored_cadence: The epoch-1 spelling the row was read with; never
            serialized, since the cadence serializer writes it in place.
    """

    stored_cadence: str | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="before")
    @classmethod
    def _read_legacy_cadence(cls, data: Any) -> Any:
        """Read the epoch-1 cadence of a stored row, a filed proof's, as its native value."""
        cadence = data.get("cadence") if isinstance(data, dict) else None
        if isinstance(cadence, str) and cadence in LEGACY_CADENCE_ALIASES:
            return {**data, "cadence": LEGACY_CADENCE_ALIASES[cadence], "stored_cadence": cadence}
        return data

    @field_serializer("cadence", check_fields=False)
    def _write_stored_cadence(self, cadence: str) -> str:
        """Write the cadence as the row stated it."""
        return self.stored_cadence or cadence


__all__ = ["LEGACY_CADENCE_ALIASES", "StoredCadenceRow", "refuse_retired_cadence"]
