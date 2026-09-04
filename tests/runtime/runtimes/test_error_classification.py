"""Runtime error classification reads the stream the vendor actually writes to.

Under ``--output-format stream-json`` a failing vendor call routes its error
envelope to **stdout** and leaves stderr empty, so the per-adapter
``parse_error`` stderr ladder saw nothing and answered ``RUNTIME_API_ERROR`` for
every such failure regardless of cause. These tests pin the fix from both ends:

* one stdout fixture per canonical :data:`ErrorClass` classifies to THAT class
  and never to the default; and
* the bounded retry ladder --- the production seam that classifies a live spawn
  failure --- reads the stdout payload before falling through to the injected
  per-runtime classifier.

Extraction is key-driven, never message-substring-driven, so a fixture whose
human-readable message names a *different* failure still classifies off the
structured field.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from eawf.runtime.runtimes.adapter import (
    ALL_ERROR_CLASSES,
    RUNTIME_API_ERROR,
    RUNTIME_AUTH_ERROR,
    RUNTIME_RATE_LIMIT,
    RUNTIME_SERVER_ERROR,
    RUNTIME_TIMEOUT,
    ErrorClass,
    RuntimeSpawnError,
    classify_stream_error,
)
from eawf.runtime.runtimes.stream_json import VendorErrorSignal, vendor_error_signal
from eawf.workflow.dispatch.retry import RetryExhaustedError, spawn_with_retry

#: One stream-json stdout payload per canonical error class. Every payload
#: carries its signal on stdout with stderr empty -- the shape that made the
#: stderr-only taxonomy answer its default for the whole failure population.
_PAYLOAD_FOR_CLASS: dict[ErrorClass, str] = {
    RUNTIME_AUTH_ERROR: json.dumps(
        {"type": "error", "error": {"type": "authentication_error", "message": "nope"}}
    ),
    RUNTIME_RATE_LIMIT: json.dumps(
        {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
    ),
    RUNTIME_SERVER_ERROR: json.dumps(
        {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
    ),
    RUNTIME_TIMEOUT: json.dumps({"type": "error", "code": "deadline_exceeded"}),
    RUNTIME_API_ERROR: json.dumps(
        {"type": "result", "is_error": True, "subtype": "error_max_turns"}
    ),
}


def _fail_with(stdout: str) -> RuntimeSpawnError:
    """Build a spawn failure carrying *stdout* and an EMPTY stderr."""
    return RuntimeSpawnError("spawn failed", exit_status=1, stderr=b"", stdout=stdout.encode())


# ---------------------------------------------------------------------------
# One fixture per class: the classifier returns that class, never the default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("error_class", list(_PAYLOAD_FOR_CLASS))
def test_classify_stream_error_returns_the_fixture_class(error_class: ErrorClass) -> None:
    """Each class fixture on stdout classifies to THAT class."""
    assert classify_stream_error(_PAYLOAD_FOR_CLASS[error_class].encode()) == error_class


def test_payload_fixtures_cover_every_declared_error_class() -> None:
    """The fixture table is total over the closed error-class set."""
    assert set(_PAYLOAD_FOR_CLASS) == set(ALL_ERROR_CLASSES)


@pytest.mark.parametrize("error_class", [c for c in _PAYLOAD_FOR_CLASS if c != RUNTIME_API_ERROR])
def test_classify_stream_error_never_falls_back_to_the_default(error_class: ErrorClass) -> None:
    """A non-default class fixture never degrades to ``RUNTIME_API_ERROR``."""
    assert classify_stream_error(_PAYLOAD_FOR_CLASS[error_class].encode()) != RUNTIME_API_ERROR


def test_classify_stream_error_reads_status_code_when_no_type_is_named() -> None:
    """An envelope naming only an HTTP status classifies off that status."""
    payload = json.dumps({"type": "error", "error": {"status": 429}}).encode()
    assert classify_stream_error(payload) == RUNTIME_RATE_LIMIT


def test_classify_stream_error_ignores_the_human_readable_message() -> None:
    """A message naming a different failure never overrides the structured type."""
    payload = json.dumps(
        {"type": "error", "error": {"type": "rate_limit_error", "message": "401 unauthorized"}}
    ).encode()
    assert classify_stream_error(payload) == RUNTIME_RATE_LIMIT


def test_classify_stream_error_finds_the_error_line_in_a_transcript() -> None:
    """A multi-line transcript classifies off its terminal error event."""
    transcript = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "assistant", "message": {"content": "working"}}),
            json.dumps({"type": "error", "error": {"type": "overloaded_error"}}),
        ]
    ).encode()
    assert classify_stream_error(transcript) == RUNTIME_SERVER_ERROR


# ---------------------------------------------------------------------------
# Boundary + error paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stdout", [b"", b"   ", b"not json at all", b"[1, 2, 3]", b"{"])
def test_classify_stream_error_returns_none_without_a_signal(stdout: bytes) -> None:
    """Empty / non-JSON / non-object stdout yields no class (never a default)."""
    assert classify_stream_error(stdout) is None


def test_classify_stream_error_returns_none_on_a_successful_envelope() -> None:
    """A clean result envelope names no error, so the stderr ladder still owns it."""
    payload = json.dumps({"type": "result", "subtype": "success", "result": "done"}).encode()
    assert classify_stream_error(payload) is None


def test_classify_stream_error_returns_none_for_an_unmapped_token() -> None:
    """An unrecognised error-type token falls through rather than defaulting."""
    payload = json.dumps({"type": "error", "error": {"type": "brand_new_error"}}).encode()
    assert classify_stream_error(payload) is None


def test_classify_stream_error_survives_undecodable_bytes() -> None:
    """Invalid UTF-8 decodes with replacement instead of raising."""
    assert classify_stream_error(b"\xff\xfe garbage") is None


def test_vendor_error_signal_skips_the_event_discriminator() -> None:
    """``type`` names the event, not the failure, on a self-carrying envelope."""
    payload = json.dumps({"type": "result", "is_error": True, "subtype": "error_max_turns"})
    assert vendor_error_signal(payload) == VendorErrorSignal(error_type="error_max_turns")


def test_vendor_error_signal_rejects_a_boolean_status() -> None:
    """A ``bool`` never reads as a status code (it is an ``int`` subclass)."""
    payload = json.dumps({"type": "error", "error": {"status": True}})
    assert vendor_error_signal(payload) is None


def test_vendor_error_signal_accepts_a_digit_string_status() -> None:
    """A status stamped as a digit-only string still resolves."""
    payload = json.dumps({"type": "error", "error": {"status": "503"}})
    assert vendor_error_signal(payload) == VendorErrorSignal(status_code=503)


def test_vendor_error_signal_returns_none_for_an_empty_payload() -> None:
    """An empty transcript names no signal."""
    assert vendor_error_signal("") is None


# ---------------------------------------------------------------------------
# The production seam: the retry ladder classifies from stdout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("error_class", list(_PAYLOAD_FOR_CLASS))
def test_retry_ladder_classifies_the_stdout_payload(error_class: ErrorClass) -> None:
    """The ladder reads the stdout payload instead of the stderr default.

    The injected classifier stands in for a real adapter's ``parse_error``: it
    sees empty stderr and answers the ``RUNTIME_API_ERROR`` default. Any class
    other than that default therefore proves the stdout reading won.
    """
    payload = _PAYLOAD_FOR_CLASS[error_class]

    async def _spawn(_runtime: str) -> object:
        raise _fail_with(payload)

    def _stderr_default(_exc: RuntimeSpawnError, _runtime: str) -> ErrorClass:
        return RUNTIME_API_ERROR

    with pytest.raises(RetryExhaustedError) as caught:
        asyncio.run(
            spawn_with_retry(
                runtime="claude-code",
                preference=[],
                spawn=_spawn,
                classify=_stderr_default,
                max_attempts=1,
            )
        )
    assert caught.value.failures[0].error_class == error_class


def test_retry_ladder_falls_through_to_the_injected_classifier() -> None:
    """A failure whose stdout names no signal still reaches the stderr ladder."""

    async def _spawn(_runtime: str) -> object:
        raise RuntimeSpawnError("boom", exit_status=1, stderr=b"401 unauthorized")

    def _stderr_classifier(_exc: RuntimeSpawnError, _runtime: str) -> ErrorClass:
        return RUNTIME_AUTH_ERROR

    with pytest.raises(RetryExhaustedError) as caught:
        asyncio.run(
            spawn_with_retry(
                runtime="claude-code",
                preference=[],
                spawn=_spawn,
                classify=_stderr_classifier,
                max_attempts=1,
            )
        )
    assert caught.value.failures[0].error_class == RUNTIME_AUTH_ERROR
