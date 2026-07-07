"""P30-I21-W30 (G4): claude spawns default to stream-json + envelope unwrap.

Before this wave the claude adapter spawned with ``--output-format json``, which
withholds a single envelope until the spawn ends -- a multi-minute output
blackout even though the watch tail already renders stream-json. This wave makes
``--output-format stream-json`` (plus the print-mode-required ``--verbose``) the
unconditional default and routes ``_parse_claude_result`` through
``terminal_result_envelope`` so the usage / cost / session parse survives the
multi-line transcript. These tests pin both halves.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from eawf.runtime.runtimes.claude.adapter import _parse_claude_result
from eawf.runtime.runtimes.stream_json import terminal_result_envelope

_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 1, 12, 0, 5, tzinfo=UTC)

_RESULT_EVENT: dict[str, object] = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "session_id": "sess-stream",
    "result": "the answer text",
    "total_cost_usd": 0.0123,
    "usage": {
        "input_tokens": 100,
        "output_tokens": 42,
        "cache_creation_input_tokens": 80,
        "cache_read_input_tokens": 200,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 50,
            "ephemeral_1h_input_tokens": 30,
        },
    },
    "modelUsage": {"claude-opus-4-8": {"inputTokens": 100}},
}


def _transcript() -> str:
    """A representative multi-line stream-json transcript ending in a result."""
    return "\n".join(
        json.dumps(event)
        for event in (
            {"type": "system", "subtype": "init", "session_id": "sess-stream"},
            {"type": "assistant", "message": {"content": "thinking"}},
            _RESULT_EVENT,
        )
    )


def test_terminal_result_envelope_isolates_result_line_from_transcript() -> None:
    """The terminal ``type=="result"`` line is isolated from a JSONL transcript."""
    envelope = terminal_result_envelope(_transcript())
    assert envelope is not None
    assert json.loads(envelope)["session_id"] == "sess-stream"
    assert json.loads(envelope)["usage"]["input_tokens"] == 100


def test_terminal_result_envelope_accepts_single_envelope() -> None:
    """A single result envelope (no transcript) is returned via the fast path."""
    single = json.dumps(_RESULT_EVENT)
    assert terminal_result_envelope(single) == single


def test_terminal_result_envelope_returns_none_for_bare_result_object() -> None:
    """A legacy ``{"result": ...}`` object with no ``type`` yields None."""
    assert terminal_result_envelope('{"result": "hi"}') is None


def test_terminal_result_envelope_returns_none_for_empty() -> None:
    """Empty / whitespace input has no result event."""
    assert terminal_result_envelope("   ") is None


def test_parse_claude_result_parses_stream_json_transcript() -> None:
    """The metering parse reads usage / cost / session off the terminal result.

    A multi-line stream-json transcript is the new default output; the parse
    must isolate its terminal result event and extract the same fields the
    legacy single envelope carried.
    """
    result = _parse_claude_result(
        runtime="claude-code",
        model="opus",
        stdout=_transcript().encode("utf-8"),
        stderr=b"",
        exit_status=0,
        subprocess_pid=4242,
        started_at=_T0,
        ended_at=_T1,
    )
    assert result.session_id == "sess-stream"
    assert result.text == "the answer text"
    assert result.input_tokens == 100
    assert result.output_tokens == 42
    assert result.cache_read_input_tokens == 200
    assert result.cache_creation_5m_input_tokens == 50
    assert result.cache_creation_1h_input_tokens == 30
    assert result.resolved_model == "claude-opus-4-8"
    assert result.cost_usd_reported == Decimal("0.0123")


def test_parse_claude_result_still_parses_bare_result_object() -> None:
    """A bare ``{"result": ...}`` object (no type) still parses whole (back-compat)."""
    result = _parse_claude_result(
        runtime="claude-code",
        model="opus",
        stdout=b'{"result": "hi", "session_id": "s1"}',
        stderr=b"",
        exit_status=0,
        subprocess_pid=1,
        started_at=_T0,
        ended_at=_T1,
    )
    assert result.text == "hi"
    assert result.session_id == "s1"
    assert result.input_tokens == 0
