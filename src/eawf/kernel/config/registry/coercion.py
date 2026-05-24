"""Raw-answer coercion + validation for the ``eawf config`` menu surface.

Turns a raw string answer from the interactive menu into the typed value
matching a :class:`~eawf.kernel.config.registry.config_keys.ConfigKey` entry's
declared type, raising :class:`~eawf.surfaces.cli.errors.UserError`
(``kind="InvalidInput"``) on failure.

Public API:

- :func:`coerce_and_validate` — turn a raw string answer into a typed value
  matching the entry's declared type.
- :func:`is_known_key` — ``True`` when a dotted key has a registry entry or
  appears in the supplied merged config.

The module also runs :func:`_registry_self_check_defaults` at import time so
a future contributor cannot land a registry entry whose default contradicts
its declared type, range, or choices.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from eawf.kernel.config.registry.config_keys import CONFIG_REGISTRY, ConfigKey, registry_lookup
from eawf.surfaces.cli.errors import UserError


def _coerce_bool(raw: str | bool) -> bool:
    """Coerce a string answer to bool.

    Raises:
        UserError: Empty / unknown value (``kind="InvalidInput"``).
    """
    if isinstance(raw, bool):
        return raw
    lowered = raw.strip().lower()
    if lowered in ("true", "yes", "y", "1", "on"):
        return True
    if lowered in ("false", "no", "n", "0", "off"):
        return False
    raise UserError(f"cannot coerce {raw!r} to bool", kind="InvalidInput")


def _coerce_number(raw: str | int | float, *, want_int: bool) -> int | float:
    """Coerce a string answer to int or float.

    Raises:
        UserError: When the string fails the requested numeric parse
            (``kind="InvalidInput"``).
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw) if want_int else float(raw)
    text = str(raw).strip()
    try:
        return int(text) if want_int else float(text)
    except ValueError as exc:
        kind = "int" if want_int else "float"
        raise UserError(f"cannot coerce {raw!r} to {kind}", kind="InvalidInput") from exc


def _coerce_ranged_number(entry: ConfigKey, raw: Any, *, want_int: bool) -> int | float:
    """Coerce *raw* to int/float and range-check it against *entry*'s bounds.

    Raises:
        UserError: When *raw* does not parse as the declared numeric type
            or falls outside ``[min_value, max_value]`` (``kind="InvalidInput"``).
    """
    value = _coerce_number(raw, want_int=want_int)
    if entry.min_value is not None and value < entry.min_value:
        raise UserError(
            f"value {value} below minimum {entry.min_value} for {entry.key}",
            kind="InvalidInput",
        )
    if entry.max_value is not None and value > entry.max_value:
        raise UserError(
            f"value {value} above maximum {entry.max_value} for {entry.key}",
            kind="InvalidInput",
        )
    return value


def _coerce_choice(entry: ConfigKey, raw: Any) -> str:
    """Coerce *raw* to one of *entry*'s declared single-choice options.

    Raises:
        UserError: When the stringified value is not a declared choice
            (``kind="InvalidInput"``).
    """
    text = str(raw)
    if entry.choices is None or text not in entry.choices:
        raise UserError(
            f"value {text!r} not in choices {list(entry.choices or ())} for {entry.key}",
            kind="InvalidInput",
        )
    return text


def _coerce_multichoice(entry: ConfigKey, raw: Any) -> list[str]:
    """Coerce *raw* to a list of *entry*'s declared multi-choice options.

    Accepts a sequence (tuple/list) or a comma-separated string.

    Raises:
        UserError: When the key declares no choices, or any parsed item is
            not a declared choice (``kind="InvalidInput"``).
    """
    if isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        items = [chunk.strip() for chunk in str(raw).split(",") if chunk.strip()]
    if entry.choices is None:
        raise UserError(
            f"multichoice key {entry.key} declared without choices", kind="InvalidInput"
        )
    unknown = [item for item in items if item not in entry.choices]
    if unknown:
        raise UserError(
            f"value(s) {unknown!r} not in choices {list(entry.choices)} for {entry.key}",
            kind="InvalidInput",
        )
    return items


def coerce_and_validate(entry: ConfigKey, raw: Any) -> Any:
    """Convert *raw* into the typed value declared by *entry*.

    Args:
        entry: Registry entry describing the key.
        raw: Raw answer from the menu — typically a string from questionary,
            but already-typed values (e.g. bool from ``questionary.confirm``)
            are accepted unchanged.

    Returns:
        The coerced + range-checked value, ready to be written to the YAML
        layer through the existing :func:`_atomic_write_yaml` helper.

    Raises:
        UserError: When the raw value cannot be parsed as the declared
            type, is outside the declared range, or is not one of the
            declared choices (``kind="InvalidInput"``).
    """
    if entry.type == "bool":
        return _coerce_bool(raw)
    if entry.type == "int":
        return _coerce_ranged_number(entry, raw, want_int=True)
    if entry.type == "float":
        return _coerce_ranged_number(entry, raw, want_int=False)
    if entry.type == "str":
        return str(raw)
    if entry.type == "choice":
        return _coerce_choice(entry, raw)
    if entry.type == "multichoice":
        return _coerce_multichoice(entry, raw)
    raise UserError(f"unknown registry type: {entry.type}", kind="InvalidInput")


def _registry_self_check_defaults() -> None:
    """Module-load assertion: each entry's ``default`` coerces under its declared type.

    Failing this assertion at import time is loud — a future contributor
    cannot land a registry entry whose default contradicts its declared
    type or violates its declared range / choices.
    """
    for entry in CONFIG_REGISTRY:
        try:
            coerce_and_validate(entry, entry.default)
        except UserError as exc:  # pragma: no cover  asserted, not branched
            raise AssertionError(
                f"registry entry {entry.key!r}: default {entry.default!r} fails its own "
                f"declared validation ({exc})"
            ) from exc


_registry_self_check_defaults()


def is_known_key(merged: Mapping[str, Any] | None, key: str) -> bool:
    """Return True when *key* either has a registry entry or appears in *merged*.

    Helper for the menu's "save back to merged config" round-trip. The
    registry is the authoritative metadata source for menu UX, but the
    layered config may carry keys that pre-date the registry — the helper
    treats either presence as sufficient to flag the key as known.
    """
    if registry_lookup(key) is not None:
        return True
    if merged is None:
        return False
    cur: Any = merged
    for part in key.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return False
        cur = cur[part]
    return True
