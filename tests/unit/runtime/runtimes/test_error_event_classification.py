"""A zero-exit error event classifies by its own message, not the default.

OpenCode reports an auth or rate-limit failure as a stream ``error``
event while the process still exits ZERO. The
per-adapter ``parse_error`` ladder keys on ``stderr``, and those raise sites
used to carry no stderr at all, so the whole zero-exit failure population
collapsed onto the conservative ``RUNTIME_API_ERROR`` default -- a switch
signal -- when the real class was ``RUNTIME_AUTH_ERROR`` (a HALT) or
``RUNTIME_RATE_LIMIT``.

These tests pin the fix end-to-end: the parse raises, and the resolved
adapter's ``parse_error`` maps the raised error to its canonical class. Boundary and
error-path fixtures pin that an event with no readable detail degrades to an
empty stderr + the ``RUNTIME_API_ERROR`` default rather than crashing the
parse.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from eawf.runtime.runtimes.adapter import (
    RUNTIME_API_ERROR,
    RUNTIME_AUTH_ERROR,
    RUNTIME_RATE_LIMIT,
    ErrorClass,
    RuntimeSpawnError,
)
from eawf.runtime.runtimes.opencode.adapter import _parse_opencode_result
from eawf.runtime.runtimes.selector import select_adapter

_T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 9, 1, 12, 0, 5, tzinfo=UTC)


def _ndjson(events: list[dict[str, object]]) -> bytes:
    """Serialize *events* as the newline-delimited stream the CLI emits."""
    return ("\n".join(json.dumps(event) for event in events) + "\n").encode()


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
    """Classify *exc* through the resolved adapter's ``parse_error``."""
    exit_status = exc.exit_status if exc.exit_status is not None else -1
    return select_adapter(runtime).parse_error(exit_status, exc.stderr)


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
