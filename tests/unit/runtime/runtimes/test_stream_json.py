"""Unit tests for :mod:`eawf.runtime.runtimes.stream_json`.

Pin the shared unwrap helper both the report binder + the Watch render reuse:
peel the claude stream-json result envelope, then strip prose / code fences to
isolate the embedded report JSON. Boundary + error paths cover an empty
payload, a bare document, a multi-line transcript, a non-result envelope, and
prose with no JSON at all.
"""

from __future__ import annotations

import json

from eawf.runtime.runtimes.stream_json import (
    extract_embedded_json,
    unwrap_agent_json,
    unwrap_result_envelope,
)


def test_unwrap_result_envelope_returns_result_field() -> None:
    """A single result envelope yields its ``result`` string."""
    env = json.dumps({"type": "result", "subtype": "success", "result": "hello there"})
    assert unwrap_result_envelope(env) == "hello there"


def test_unwrap_result_envelope_scans_stream_transcript() -> None:
    """A multi-line stream-json transcript returns the terminal result line."""
    transcript = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "assistant", "message": {"content": "thinking"}}),
            json.dumps({"type": "result", "result": "final answer"}),
        ]
    )
    assert unwrap_result_envelope(transcript) == "final answer"


def test_unwrap_result_envelope_passes_through_bare_text() -> None:
    """Text with no result envelope is returned verbatim."""
    assert unwrap_result_envelope("just some prose") == "just some prose"


def test_unwrap_result_envelope_ignores_non_result_object() -> None:
    """A JSON object that is not a result envelope passes through unchanged."""
    other = json.dumps({"type": "assistant", "message": "x"})
    assert unwrap_result_envelope(other) == other


def test_unwrap_result_envelope_empty_is_verbatim() -> None:
    """An empty / whitespace payload is returned unchanged (boundary)."""
    assert unwrap_result_envelope("") == ""
    assert unwrap_result_envelope("   ") == "   "


def test_extract_embedded_json_bare_object() -> None:
    """A bare JSON object is returned as-is."""
    doc = '{"a": 1, "b": [2, 3]}'
    assert extract_embedded_json(doc) == doc


def test_extract_embedded_json_from_json_fence() -> None:
    """A ```json fenced block yields its inner JSON."""
    inner = '{"role": "executor", "verdict": "pass"}'
    text = f"prose before\n```json\n{inner}\n```\nprose after"
    assert json.loads(extract_embedded_json(text)) == json.loads(inner)


def test_extract_embedded_json_from_bare_fence() -> None:
    """A bare ``` fenced block (no info string) still yields its inner JSON."""
    inner = '{"x": 1}'
    text = f"```\n{inner}\n```"
    assert extract_embedded_json(text) == inner


def test_extract_embedded_json_from_prose_balanced_span() -> None:
    """JSON amid prose (no fence) is isolated by balanced-brace scan."""
    text = 'The answer is {"nested": {"k": "}"}, "done": true} -- cheers'
    extracted = extract_embedded_json(text)
    assert json.loads(extracted) == {"nested": {"k": "}"}, "done": True}


def test_extract_embedded_json_array_span() -> None:
    """A top-level array embedded in prose is isolated too."""
    text = "results: [1, 2, [3, 4]] end"
    assert extract_embedded_json(text) == "[1, 2, [3, 4]]"


def test_extract_embedded_json_no_json_returns_verbatim() -> None:
    """Prose with no JSON is returned unchanged so json.loads fails honestly."""
    assert extract_embedded_json("no json here") == "no json here"


def test_extract_embedded_json_empty_verbatim() -> None:
    """An empty payload is returned unchanged (boundary)."""
    assert extract_embedded_json("") == ""


def test_unwrap_agent_json_composes_envelope_and_fence() -> None:
    """The composed helper peels the envelope AND strips the prose/fence."""
    inner = '{"role": "executor", "verdict": "pass", "summary": "ok"}'
    prose = f"Report follows.\n```json\n{inner}\n```\n"
    envelope = json.dumps({"type": "result", "result": prose})
    assert json.loads(unwrap_agent_json(envelope)) == json.loads(inner)


def test_unwrap_agent_json_bare_report_untouched() -> None:
    """A bare report JSON (no envelope, no fence) is returned decodable."""
    inner = '{"role": "researcher", "findings": []}'
    assert json.loads(unwrap_agent_json(inner)) == json.loads(inner)
