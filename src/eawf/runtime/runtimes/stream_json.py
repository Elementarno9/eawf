"""Unwrap a runtime CLI's stream-json output into readable / parseable text.

The claude-code CLI (``claude -p --output-format json`` / ``stream-json``)
wraps the agent's answer in a result envelope::

    {"type": "result", "subtype": "success", "result": "<prose + optional ```json fence>"}

Two consumers must see PAST that envelope:

- the report binder
  (:func:`~eawf.workflow.dispatch.llm_assist.assist_with_schema`) must recover
  the embedded report JSON so a well-formed report binds on the first attempt
  instead of being rejected as ``invalid_json`` and replaced with a synthesized
  placeholder; and
- the Watch tail render must show the human-readable ``result`` text (and a
  compact result summary), not the raw JSON envelope.

Both share the two primitives here: :func:`unwrap_result_envelope` peels the
stream-json result line down to its ``result`` payload, and
:func:`extract_embedded_json` strips prose / code fences to isolate the JSON a
report body validates against. :func:`unwrap_agent_json` composes the two for
the bind path.

A third consumer reads the same transcript for a different reason: under
``--output-format stream-json`` a *failing* call routes the vendor's error
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

#: The stream-json event type carrying the agent's final answer.
_RESULT_EVENT_TYPE: str = "result"

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


def unwrap_result_envelope(raw: str) -> str:
    """Return the ``result`` payload of a claude stream-json envelope.

    Accepts either a single JSON result envelope or a multi-line stream-json
    transcript (one JSON event per line, terminated by a ``type=="result"``
    line). When a result line is found its ``result`` string is returned;
    otherwise *raw* is returned unchanged (a bare report, or output this
    helper does not recognise, passes through untouched).

    Args:
        raw: The runtime's stdout text (an envelope, a stream-json transcript,
            or already-bare content).

    Returns:
        The unwrapped ``result`` string, or *raw* verbatim when no result
        envelope is present.
    """
    stripped = raw.strip()
    if not stripped:
        return raw
    # Fast path: the whole payload is a single result envelope.
    envelope = _as_result_envelope(stripped)
    if envelope is not None:
        return envelope
    # Stream-json transcript: scan lines for the terminal result event.
    for line in reversed(stripped.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        envelope = _as_result_envelope(candidate)
        if envelope is not None:
            return envelope
    return raw


def terminal_result_envelope(raw: str) -> str | None:
    """Return the raw JSON text of a claude stream-json terminal result event.

    ``--output-format stream-json`` emits one JSON event per line terminated by a
    ``type=="result"`` envelope carrying ``usage`` / ``total_cost_usd`` /
    ``session_id`` / ``modelUsage`` -- the fields the metering parse reads. This
    returns that terminal envelope's raw JSON text (the whole object, not just
    its inner ``result`` string) so a caller can ``json.loads`` it directly.
    Accepts a single result envelope (the fast path) as well as a multi-line
    transcript. Returns ``None`` when no ``type=="result"`` event is present
    (e.g. the legacy single-object ``{"result": ...}`` json format, which the
    caller parses whole instead).

    Args:
        raw: The runtime's stdout text (a stream-json transcript or a single
            envelope).

    Returns:
        The terminal result event's raw JSON text, or ``None`` when none is
        present.
    """
    stripped = raw.strip()
    if not stripped:
        return None
    if _is_result_object(stripped):
        return stripped
    for line in reversed(stripped.splitlines()):
        candidate = line.strip()
        if candidate and _is_result_object(candidate):
            return candidate
    return None


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


def _is_result_object(candidate: str) -> bool:
    """Return whether *candidate* parses to a JSON object with ``type=="result"``.

    Args:
        candidate: A single (stripped) line or the whole payload.

    Returns:
        ``True`` when *candidate* is a JSON object whose ``type`` is
        ``"result"``; ``False`` otherwise.
    """
    if not candidate.startswith("{"):
        return False
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return False
    return isinstance(data, dict) and data.get("type") == _RESULT_EVENT_TYPE


def _as_result_envelope(candidate: str) -> str | None:
    """Return the ``result`` string when *candidate* is a result envelope.

    Args:
        candidate: A single (stripped) line or the whole payload.

    Returns:
        The ``result`` string when *candidate* parses to a JSON object with
        ``type == "result"`` and a string ``result`` field; ``None`` otherwise.
    """
    if not candidate.startswith("{"):
        return None
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("type") != _RESULT_EVENT_TYPE:
        return None
    result = data.get("result")
    return result if isinstance(result, str) else None


def extract_embedded_json(text: str) -> str:
    """Isolate the JSON object / array embedded in *text*.

    Handles the three shapes a model emits around its report body: a bare JSON
    document; a fenced ```` ```json ... ``` ```` (or bare ```` ``` ... ``` ````)
    block; and JSON surrounded by prose. Returns the first balanced ``{...}`` /
    ``[...]`` span; when none is found *text* is returned unchanged so the
    caller's ``json.loads`` raises the honest decode error.

    Args:
        text: Candidate text that may wrap a JSON document in prose or fences.

    Returns:
        The embedded JSON substring, or *text* verbatim when none is found.
    """
    stripped = text.strip()
    if not stripped:
        return text
    fenced = _fenced_block(stripped)
    candidate = fenced if fenced is not None else stripped
    if candidate.startswith(("{", "[")):
        return candidate
    span = _first_balanced_span(candidate)
    return span if span is not None else text


def unwrap_agent_json(raw: str) -> str:
    """Return the report JSON embedded in a runtime's raw output.

    Composes :func:`unwrap_result_envelope` (peel the stream-json envelope)
    then :func:`extract_embedded_json` (strip prose / fences) so a report body
    that a model wrapped in an envelope + prose + a code fence still decodes on
    the first bind attempt.

    Args:
        raw: The runtime's raw stdout text.

    Returns:
        The best-effort JSON substring ready for ``json.loads``.
    """
    return extract_embedded_json(unwrap_result_envelope(raw))


def _fenced_block(text: str) -> str | None:
    """Return the body of the first ```` ``` ```` fenced block in *text*.

    Recognises an optional info string (e.g. ``json``) on the opening fence.
    Returns ``None`` when no closed fence is present.

    Args:
        text: Text that may contain a fenced code block.

    Returns:
        The fenced block's inner text (stripped), or ``None``.
    """
    open_marker = text.find("```")
    if open_marker == -1:
        return None
    # Skip the opening fence + optional info string up to the newline.
    body_start = text.find("\n", open_marker)
    if body_start == -1:
        return None
    close_marker = text.find("```", body_start + 1)
    if close_marker == -1:
        return None
    return text[body_start + 1 : close_marker].strip()


def _first_balanced_span(text: str) -> str | None:
    """Return the first balanced ``{...}`` or ``[...]`` span in *text*.

    Scans for the first ``{`` or ``[`` and walks to its matching close,
    honouring nested brackets and skipping bracket characters inside JSON
    strings (with escape handling). Returns ``None`` when no opener is found
    or the span never closes.

    Args:
        text: Text that may contain a JSON object / array amid prose.

    Returns:
        The balanced substring, or ``None``.
    """
    opener_index = _first_opener(text)
    if opener_index is None:
        return None
    open_char = text[opener_index]
    close_char = "}" if open_char == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for i in range(opener_index, len(text)):
        char = text[i]
        if in_string:
            in_string, escaped = _advance_string(char, escaped=escaped)
            continue
        if char == '"':
            in_string = True
        elif char in (open_char, close_char):
            depth += 1 if char == open_char else -1
            if depth == 0:
                return text[opener_index : i + 1]
    return None


def _advance_string(char: str, *, escaped: bool) -> tuple[bool, bool]:
    """Advance the in-string scan state by one character.

    Args:
        char: The current character (known to be inside a JSON string).
        escaped: Whether the previous character was a backslash escape.

    Returns:
        ``(still_in_string, next_escaped)`` -- the string closes on an
        unescaped ``"``.
    """
    if escaped:
        return True, False
    if char == "\\":
        return True, True
    if char == '"':
        return False, False
    return True, False


def _first_opener(text: str) -> int | None:
    """Return the index of the first ``{`` or ``[`` in *text*, or ``None``."""
    brace = text.find("{")
    bracket = text.find("[")
    if brace == -1 and bracket == -1:
        return None
    if brace == -1:
        return bracket
    if bracket == -1:
        return brace
    return min(brace, bracket)


__all__ = [
    "VendorErrorSignal",
    "extract_embedded_json",
    "terminal_result_envelope",
    "unwrap_agent_json",
    "unwrap_result_envelope",
    "vendor_error_signal",
]
