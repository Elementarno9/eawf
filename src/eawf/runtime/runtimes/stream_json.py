"""Read the structured vendor error out of a runtime CLI's stream-json stdout.

Under ``--output-format stream-json`` a *failing* call routes the vendor's error
envelope to **stdout**, leaving stderr empty, so an error taxonomy that only
matches stderr classifies every such failure as its default.
:func:`vendor_error_signal` extracts the structured error fields
(:class:`VendorErrorSignal`) from that stdout transcript so the classifier reads
the stream the vendor actually writes to. Extraction is deliberately key-driven
-- it reads named JSON fields and never matches substrings of the human-readable
message, which drifts between vendor releases.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict

#: The stream-json event type carrying a vendor error envelope.
_ERROR_EVENT_TYPE: str = "error"

#: JSON keys carrying a vendor's structured error *type*, in precedence order.
#: Spans the three stream-json dialects: claude nests ``error.type`` and stamps
#: ``subtype`` on an ``is_error`` result envelope, codex emits ``error.code``,
#: opencode emits ``error.name``.
_ERROR_TYPE_KEYS: tuple[str, ...] = ("type", "code", "name", "subtype", "reason")

#: JSON keys carrying a vendor's HTTP status code, in precedence order.
_STATUS_KEYS: tuple[str, ...] = ("status", "status_code", "statusCode", "http_status", "code")


class VendorErrorSignal(BaseModel):
    """Structured error fields lifted off a vendor's stream-json stdout.

    The vendor-neutral projection of a failing stream-json transcript: the
    machine-readable error *type* the vendor named and the HTTP status it
    reported. Both are optional because the dialects disagree on which they
    emit; a signal with neither is never produced (:func:`vendor_error_signal`
    returns ``None`` instead), so a caller can treat a returned signal as
    carrying at least one classifiable field.

    Transient -- NOT state-resident. Frozen + ``extra='forbid'`` so a signal is
    a closed, immutable fact about one failed call.

    Attributes:
        error_type: The vendor's structured error type token (e.g.
            ``"rate_limit_error"``, ``"authentication_error"``). Never the
            human-readable message.
        status_code: The HTTP status the vendor reported, when it reported one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    error_type: str | None = None
    status_code: int | None = None


def vendor_error_signal(raw: str) -> VendorErrorSignal | None:
    """Return the structured error signal a stream-json stdout transcript carries.

    Scans *raw* from the end (a failing transcript terminates in its error
    envelope) for the first event that declares a vendor error, then lifts the
    named error fields off it. Recognises the three shapes the dialects use: a
    nested ``{"error": {...}}`` object, a ``{"type": "error", ...}`` event, and
    claude's ``{"type": "result", "is_error": true, "subtype": ...}`` envelope.

    Reads named keys only (:data:`_ERROR_TYPE_KEYS` / :data:`_STATUS_KEYS`) --
    never a substring of the human-readable message, which is not a stable
    contract.

    Args:
        raw: The runtime's stdout text (a stream-json transcript, a single
            envelope, or unrelated output).

    Returns:
        The :class:`VendorErrorSignal` when an error-bearing event carries at
        least one classifiable field; ``None`` when *raw* is empty, carries no
        error event, or names no structured field.
    """
    stripped = raw.strip()
    if not stripped:
        return None
    candidates = [stripped, *reversed(stripped.splitlines())]
    for candidate in candidates:
        text = candidate.strip()
        if not text.startswith("{"):
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        error_object = _error_object(data)
        if error_object is None:
            continue
        signal = _signal_from(error_object)
        if signal is not None:
            return signal
    return None


def _error_object(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return the error-bearing object inside a stream-json *event*.

    Descends into a nested ``error`` object when present. When the event itself
    is the error carrier its ``type`` key is dropped: in that shape ``type``
    names the *event* (``"error"``, or ``"result"`` on claude's ``is_error``
    envelope), never the failure, so leaving it in would shadow the ``subtype``
    / ``code`` key that does name the failure.

    Args:
        event: One decoded stream-json event object.

    Returns:
        The object carrying the error fields, or ``None`` when *event* declares
        no error.
    """
    nested = event.get("error")
    if isinstance(nested, dict):
        return nested
    if event.get("type") == _ERROR_EVENT_TYPE or event.get("is_error") is True:
        return {key: value for key, value in event.items() if key != "type"}
    return None


def _signal_from(error_object: dict[str, Any]) -> VendorErrorSignal | None:
    """Lift the named error fields off an error object.

    Args:
        error_object: The object carrying the vendor's error fields.

    Returns:
        A :class:`VendorErrorSignal` when at least one field resolves; ``None``
        when the object names neither an error type nor a status code.
    """
    error_type = _first_error_type(error_object)
    status_code = _first_status_code(error_object)
    if error_type is None and status_code is None:
        return None
    return VendorErrorSignal(error_type=error_type, status_code=status_code)


def _first_error_type(error_object: dict[str, Any]) -> str | None:
    """Return the first non-generic error-type token in *error_object*.

    Skips the literal ``"error"`` discriminator (it names the event, not the
    failure) and any purely numeric value (that is a status code, read by
    :func:`_first_status_code`).

    Args:
        error_object: The object carrying the vendor's error fields.

    Returns:
        The error-type token, or ``None`` when none of the keys resolve.
    """
    for key in _ERROR_TYPE_KEYS:
        value = error_object.get(key)
        if not isinstance(value, str):
            continue
        token = value.strip()
        if not token or token == _ERROR_EVENT_TYPE or token.isdigit():
            continue
        return token
    return None


def _first_status_code(error_object: dict[str, Any]) -> int | None:
    """Return the first HTTP status code named in *error_object*.

    Accepts an integer status as well as a digit-only string (codex stamps the
    status as a string on some events). ``bool`` is rejected explicitly -- it is
    an ``int`` subclass and would otherwise read as status ``0`` / ``1``.

    Args:
        error_object: The object carrying the vendor's error fields.

    Returns:
        The status code, or ``None`` when none of the keys resolve.
    """
    for key in _STATUS_KEYS:
        value = error_object.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


__all__ = [
    "VendorErrorSignal",
    "vendor_error_signal",
]
