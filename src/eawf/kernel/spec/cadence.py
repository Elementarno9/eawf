"""The epoch-1 audit and gate cadences and the native values that replaced them.

Filed proofs in the receipt ledger still carry the epoch-1 values, so a stored
gate row reads through :data:`LEGACY_CADENCE_ALIASES` and is written back as
stored by :class:`StoredCadenceRow`; the config migration
rewrites a layer once, and an authored profile check or audit document is
refused by :func:`refuse_retired_cadence` with the value to write instead.
"""

from __future__ import annotations

from typing import Any, Final, Self

from pydantic import (
    BaseModel,
    ModelWrapValidatorHandler,
    PrivateAttr,
    field_serializer,
    model_validator,
)

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
    the rename, and the receipts keyed on them, do not move. The stored
    spelling is read state, never an input: a row cannot name it, and a copy
    whose cadence changed writes the new value.
    """

    _stored_cadence: str | None = PrivateAttr(default=None)

    @model_validator(mode="wrap")
    @classmethod
    def _read_legacy_cadence(cls, data: Any, handler: ModelWrapValidatorHandler[Self]) -> Self:
        """Read the epoch-1 cadence of a stored row, a filed proof's, as its native value."""
        cadence = data.get("cadence") if isinstance(data, dict) else None
        if not (isinstance(cadence, str) and cadence in LEGACY_CADENCE_ALIASES):
            return handler(data)
        row = handler({**data, "cadence": LEGACY_CADENCE_ALIASES[cadence]})
        row._stored_cadence = cadence
        return row

    @field_serializer("cadence", check_fields=False)
    def _write_stored_cadence(self, cadence: str) -> str:
        """Write the cadence as the row stated it, unless it has since changed."""
        stored = self._stored_cadence
        if stored is not None and LEGACY_CADENCE_ALIASES[stored] == cadence:
            return stored
        return cadence


__all__ = ["LEGACY_CADENCE_ALIASES", "StoredCadenceRow", "refuse_retired_cadence"]
