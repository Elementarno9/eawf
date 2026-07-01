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
"""

from __future__ import annotations

import json

#: The stream-json event type carrying the agent's final answer.
_RESULT_EVENT_TYPE: str = "result"


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
    "extract_embedded_json",
    "unwrap_agent_json",
    "unwrap_result_envelope",
]
