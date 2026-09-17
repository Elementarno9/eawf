"""A zero-exit error event classifies by its own message, not the default.

Codex and opencode both report an auth or rate-limit failure as a stream
``error`` / ``turn.failed`` event while the process still exits ZERO. The
per-adapter ``parse_error`` ladder keys on ``stderr``, and those raise sites
used to carry no stderr at all, so the whole zero-exit failure population
collapsed onto the conservative ``RUNTIME_API_ERROR`` default -- a switch
signal -- when the real class was ``RUNTIME_AUTH_ERROR`` (a HALT) or
``RUNTIME_RATE_LIMIT``.

These tests pin the fix end-to-end through the production seam: the parse
raises, and :func:`_live_lane_error_classifier` (the module-level classifier
the live spawn ladder and the single-wave dispatch ``_classify`` closure both
resolve through) maps the raised error to its canonical class. Boundary and
error-path fixtures pin that an event with no readable detail degrades to an
empty stderr + the ``RUNTIME_API_ERROR`` default rather than crashing the
parse.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from eawf.runtime.daemon.methods.fleet import _live_lane_error_classifier
from eawf.runtime.runtimes.adapter import (
    RUNTIME_API_ERROR,
    RUNTIME_AUTH_ERROR,
    RUNTIME_RATE_LIMIT,
    ErrorClass,
    RuntimeSpawnError,
)
from eawf.runtime.runtimes.codex.adapter import _parse_codex_result
from eawf.runtime.runtimes.opencode.adapter import _parse_opencode_result

_T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 9, 1, 12, 0, 5, tzinfo=UTC)

#: The codex phrasing for an exhausted ChatGPT-account entitlement, as it
#: arrives on the stream: the upstream failure is a JSON STRING nested in the
#: event's ``message`` field.
_CODEX_AUTH_ENVELOPE = json.dumps(
    {"error": {"message": "stream error: chatgpt subscription expired"}}
)


def _ndjson(events: list[dict[str, object]]) -> bytes:
    """Serialize *events* as the newline-delimited stream both CLIs emit."""
    return ("\n".join(json.dumps(event) for event in events) + "\n").encode()


def _codex_failure(events: list[dict[str, object]]) -> RuntimeSpawnError:
    """Parse a ZERO-exit codex stream and return the failure it raises."""
    with pytest.raises(RuntimeSpawnError) as excinfo:
        _parse_codex_result(
            runtime="codex",
            model="gpt-5-codex",
            stdout=_ndjson(events),
            stderr=b"",
            exit_status=0,
            subprocess_pid=4242,
            started_at=_T0,
            ended_at=_T1,
        )
    return excinfo.value


def _opencode_failure(events: list[dict[str, object]]) -> RuntimeSpawnError:
    """Parse a ZERO-exit opencode stream and return the failure it raises."""
    with pytest.raises(RuntimeSpawnError) as excinfo:
        _parse_opencode_result(
            runtime="opencode",
            model="anthropic/claude-x",
            stdout=_ndjson(events),
            stderr=b"",
            exit_status=0,
            subprocess_pid=4243,
            started_at=_T0,
            ended_at=_T1,
        )
    return excinfo.value


def _classify(exc: RuntimeSpawnError, runtime: str) -> ErrorClass:
    """Classify *exc* through the production lane classifier."""
    return _live_lane_error_classifier(exc, runtime)


# ---------------------------------------------------------------------------
# codex: error / turn.failed events on a zero-exit stream
# ---------------------------------------------------------------------------


def test_codex_zero_exit_error_event_classifies_as_auth() -> None:
    """An expired-subscription error event classifies as auth, not API."""
    exc = _codex_failure(
        [
            {"type": "thread.started", "thread_id": "th_1"},
            {"type": "error", "message": _CODEX_AUTH_ENVELOPE},
        ]
    )
    assert _classify(exc, "codex") == RUNTIME_AUTH_ERROR


def test_codex_zero_exit_turn_failed_classifies_as_rate_limit() -> None:
    """A rate-limited turn.failed event classifies as rate limit, not API."""
    exc = _codex_failure(
        [
            {"type": "thread.started", "thread_id": "th_1"},
            {
                "type": "turn.failed",
                "error": {"message": "429 rate limit reached for gpt-5-codex"},
            },
        ]
    )
    assert _classify(exc, "codex") == RUNTIME_RATE_LIMIT


def test_codex_error_event_stderr_carries_the_unwrapped_detail() -> None:
    """The raised stderr is the UNWRAPPED message, not the JSON envelope."""
    exc = _codex_failure([{"type": "error", "message": _CODEX_AUTH_ENVELOPE}])
    assert exc.stderr == b"stream error: chatgpt subscription expired"
    assert exc.stdout == b""


def test_codex_turn_failed_stderr_carries_the_message() -> None:
    """A turn.failed raise carries its own message on stderr."""
    exc = _codex_failure([{"type": "turn.failed", "error": {"message": "429 slow down"}}])
    assert exc.stderr == b"429 slow down"


def test_codex_error_event_without_message_carries_empty_stderr() -> None:
    """A detail-less error event degrades to empty stderr and the default."""
    exc = _codex_failure([{"type": "error"}])
    assert exc.stderr == b""
    assert _classify(exc, "codex") == RUNTIME_API_ERROR


def test_codex_turn_failed_without_error_object_carries_empty_stderr() -> None:
    """turn.failed with no ``error`` map still raises, with empty stderr."""
    exc = _codex_failure([{"type": "turn.failed"}])
    assert exc.stderr == b""
    assert _classify(exc, "codex") == RUNTIME_API_ERROR


def test_codex_error_event_non_string_message_carries_empty_stderr() -> None:
    """A non-string ``message`` yields no detail rather than a TypeError."""
    exc = _codex_failure([{"type": "error", "message": {"code": 500}}])
    assert exc.stderr == b""


def test_codex_error_event_empty_message_carries_empty_stderr() -> None:
    """An empty-string ``message`` is the empty boundary of the detail."""
    exc = _codex_failure([{"type": "error", "message": ""}])
    assert exc.stderr == b""


def test_codex_unclassifiable_error_event_keeps_the_api_default() -> None:
    """An error event naming no known cause still answers the default."""
    exc = _codex_failure([{"type": "error", "message": "something went wrong"}])
    assert exc.stderr == b"something went wrong"
    assert _classify(exc, "codex") == RUNTIME_API_ERROR


def test_codex_first_error_event_wins_over_a_later_one() -> None:
    """The scan is fail-fast: the FIRST error event raises, so its detail wins."""
    exc = _codex_failure(
        [
            {"type": "error", "message": "429 rate limit reached"},
            {"type": "error", "message": _CODEX_AUTH_ENVELOPE},
        ]
    )
    assert _classify(exc, "codex") == RUNTIME_RATE_LIMIT


def test_codex_long_error_message_is_carried_whole() -> None:
    """A long detail is carried verbatim -- the raise applies no truncation."""
    message = f"{'x' * 4096} chatgpt subscription expired"
    exc = _codex_failure([{"type": "error", "message": message}])
    assert exc.stderr == message.encode()
    assert _classify(exc, "codex") == RUNTIME_AUTH_ERROR


# ---------------------------------------------------------------------------
# opencode: error events on a zero-exit stream
# ---------------------------------------------------------------------------


def test_opencode_zero_exit_auth_error_event_classifies_as_auth() -> None:
    """A 401 error event classifies as auth, not the generic API default."""
    exc = _opencode_failure(
        [
            {
                "type": "error",
                "sessionID": "ses_1",
                "error": {
                    "name": "ProviderAuthError",
                    "data": {"message": "AI_APICallError: 401 Unauthorized response"},
                },
            }
        ]
    )
    assert exc.stderr == b"AI_APICallError: 401 Unauthorized response"
    assert _classify(exc, "opencode") == RUNTIME_AUTH_ERROR


def test_opencode_zero_exit_rate_limit_error_event_classifies_as_rate_limit() -> None:
    """A 429 error event classifies as rate limit, not the API default."""
    exc = _opencode_failure(
        [
            {
                "type": "error",
                "error": {"name": "UnknownError", "data": {"message": "429 rate limit"}},
            }
        ]
    )
    assert _classify(exc, "opencode") == RUNTIME_RATE_LIMIT


def test_opencode_error_event_falls_back_to_the_error_name() -> None:
    """With no ``data.message`` the lifted ``error.name`` lands on stderr."""
    exc = _opencode_failure([{"type": "error", "error": {"name": "UnknownError"}}])
    assert exc.stderr == b"UnknownError"
    assert _classify(exc, "opencode") == RUNTIME_API_ERROR


def test_opencode_error_event_without_error_object_carries_empty_stderr() -> None:
    """An error event with no ``error`` value degrades to empty stderr."""
    exc = _opencode_failure([{"type": "error"}])
    assert exc.stderr == b""
    assert _classify(exc, "opencode") == RUNTIME_API_ERROR


def test_opencode_error_event_with_null_name_carries_empty_stderr() -> None:
    """A null ``error.name`` is the empty boundary of the lifted detail."""
    exc = _opencode_failure([{"type": "error", "error": {"name": None}}])
    assert exc.stderr == b""


def test_opencode_error_event_with_scalar_error_is_stringified() -> None:
    """A non-mapping ``error`` value is carried as its string form."""
    exc = _opencode_failure([{"type": "error", "error": "401 unauthorized"}])
    assert exc.stderr == b"401 unauthorized"
    assert _classify(exc, "opencode") == RUNTIME_AUTH_ERROR


def test_opencode_unclassifiable_error_event_keeps_the_api_default() -> None:
    """An error event naming no known cause still answers the default."""
    exc = _opencode_failure([{"type": "error", "error": {"data": {"message": "Model not found"}}}])
    assert exc.stderr == b"Model not found"
    assert _classify(exc, "opencode") == RUNTIME_API_ERROR


def test_opencode_error_event_after_text_still_classifies() -> None:
    """A late error event (after text fragments) still carries its detail."""
    exc = _opencode_failure(
        [
            {"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": "hi"}},
            {
                "type": "error",
                "error": {"data": {"message": "401 Unauthorized"}},
            },
        ]
    )
    assert _classify(exc, "opencode") == RUNTIME_AUTH_ERROR
