"""Unit tests for the session-handle Pydantic models.

Covers :class:`~eawf.kernel.state.models.SessionAttempt`,
:class:`~eawf.kernel.state.models.DispatchAnnotation`, and the additive
``Wave.sessions`` / ``Wave.runtime_preference`` / ``Wave.dispatch_history``
fields wired in W07.

``session_log_handle`` is an **opaque** string (blob-URN or daemon-side
index key) — the model accepts any non-empty string and the daemon's
in-process registry owns the canonical format. Tests assert the field
rejects empty strings and non-string types so the boundary is enforced
at the schema layer.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import DispatchNote, WaveStatus
from eawf.kernel.state.models import DispatchAnnotation, SessionAttempt, Wave

pytestmark = pytest.mark.unit


def _now() -> datetime:
    return datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)


def _make_attempt(**overrides: object) -> SessionAttempt:
    defaults: dict[str, object] = {
        "attempt": 1,
        "runtime": "claude-code",
        "session_id": "abc-123-uuid",
        "session_log_handle": "urn:eawf:v1:session-log:claude-code:cafe1234",
        "started_at": _now(),
    }
    defaults.update(overrides)
    return SessionAttempt.model_validate(defaults)


def _make_annotation(**overrides: object) -> DispatchAnnotation:
    defaults: dict[str, object] = {
        "attempt": 1,
        "note": DispatchNote.FRESH_DISPATCH,
        "runtime_to": "claude-code",
        "occurred_at": _now(),
    }
    defaults.update(overrides)
    return DispatchAnnotation.model_validate(defaults)


def _make_wave(**overrides: object) -> Wave:
    defaults: dict[str, object] = {
        "id": "P24-I01-W07",
        "iter_id": "P24-I01",
        "title": "session-handle",
        "status": WaveStatus.IN_PROGRESS,
        "opened_at": _now(),
    }
    defaults.update(overrides)
    return Wave.model_validate(defaults)


def test_session_attempt_accepts_urn_form_handle() -> None:
    attempt = _make_attempt(
        session_log_handle="urn:eawf:v1:session-log:claude-code:abc123",
    )
    assert attempt.session_log_handle == "urn:eawf:v1:session-log:claude-code:abc123"


def test_session_attempt_accepts_opaque_index_key() -> None:
    """The handle is opaque; index-key style strings are also accepted."""
    attempt = _make_attempt(session_log_handle="session-log-index-42")
    assert attempt.session_log_handle == "session-log-index-42"


def test_session_attempt_rejects_empty_handle() -> None:
    with pytest.raises(ValidationError, match=r"string_too_short|min_length"):
        _make_attempt(session_log_handle="")


def test_session_attempt_rejects_non_string_handle() -> None:
    with pytest.raises(ValidationError):
        _make_attempt(session_log_handle=12345)


def test_session_attempt_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|extra"):
        SessionAttempt.model_validate(
            {
                "attempt": 1,
                "runtime": "claude-code",
                "session_id": "abc",
                "session_log_handle": "urn:eawf:v1:session-log:claude-code:abc",
                "started_at": _now().isoformat(),
                "unexpected_field": "should reject",
            }
        )


def test_session_attempt_rejects_attempt_below_one() -> None:
    with pytest.raises(ValidationError):
        _make_attempt(attempt=0)


def test_session_attempt_accepts_optional_fields_as_none() -> None:
    attempt = _make_attempt()
    assert attempt.ended_at is None
    assert attempt.exit_status is None
    assert attempt.subprocess_pid is None
    assert attempt.input_tokens is None
    assert attempt.output_tokens is None
    assert attempt.cache_creation_input_tokens is None
    assert attempt.cache_read_input_tokens is None


def test_session_attempt_token_fields_optional_round_trip() -> None:
    attempt = _make_attempt(
        cache_creation_input_tokens=10,
        cache_read_input_tokens=20,
        input_tokens=30,
        output_tokens=40,
    )
    payload = attempt.model_dump(mode="json")
    revived = SessionAttempt.model_validate(payload)
    assert revived == attempt


def test_dispatch_annotation_validates_enum() -> None:
    annotation = _make_annotation(note=DispatchNote.CONTINUE_FROM_SESSION)
    assert annotation.note is DispatchNote.CONTINUE_FROM_SESSION


def test_dispatch_annotation_rejects_unknown_note() -> None:
    with pytest.raises(ValidationError):
        DispatchAnnotation.model_validate(
            {
                "attempt": 1,
                "note": "bogus_note",
                "runtime_to": "claude-code",
                "occurred_at": _now().isoformat(),
            }
        )


def test_dispatch_annotation_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|extra"):
        DispatchAnnotation.model_validate(
            {
                "attempt": 1,
                "note": DispatchNote.FRESH_DISPATCH.value,
                "runtime_to": "claude-code",
                "occurred_at": _now().isoformat(),
                "rogue_field": True,
            }
        )


def test_dispatch_annotation_optional_runtime_from() -> None:
    annotation = _make_annotation()
    assert annotation.runtime_from is None
    annotation_with_from = _make_annotation(
        runtime_from="codex",
        note=DispatchNote.SWITCH_ON_ERROR,
    )
    assert annotation_with_from.runtime_from == "codex"


def test_wave_defaults_sessions_to_empty_dict() -> None:
    wave = _make_wave()
    assert wave.sessions == {}
    assert wave.runtime_preference is None
    assert wave.dispatch_history == []


def test_wave_runtime_preference_accepts_list() -> None:
    wave = _make_wave(runtime_preference=["claude-code", "codex"])
    assert wave.runtime_preference == ["claude-code", "codex"]


def test_wave_sessions_round_trip_through_json() -> None:
    attempt_1 = _make_attempt(attempt=1, runtime="claude-code")
    attempt_2 = _make_attempt(attempt=2, runtime="codex", session_id="def-456-uuid")
    annotation_1 = _make_annotation(attempt=1)
    annotation_2 = _make_annotation(
        attempt=2,
        note=DispatchNote.SWITCH_ON_ERROR,
        runtime_from="claude-code",
        runtime_to="codex",
    )
    wave = _make_wave(
        sessions={1: attempt_1, 2: attempt_2},
        runtime_preference=["claude-code", "codex"],
        dispatch_history=[annotation_1, annotation_2],
    )
    serialised = wave.model_dump_json()
    revived = Wave.model_validate_json(serialised)
    assert revived == wave
    assert revived.sessions[1].runtime == "claude-code"
    assert revived.sessions[2].runtime == "codex"
    assert revived.dispatch_history[0].note is DispatchNote.FRESH_DISPATCH
    assert revived.dispatch_history[1].note is DispatchNote.SWITCH_ON_ERROR


def test_wave_rejects_extra_session_fields_via_json() -> None:
    """Round-tripping a Wave with a bogus session row must fail at validate."""
    payload = {
        "id": "P24-I01-W07",
        "iter_id": "P24-I01",
        "title": "session-handle",
        "status": WaveStatus.IN_PROGRESS.value,
        "opened_at": _now().isoformat(),
        "sessions": {
            "1": {
                "attempt": 1,
                "runtime": "claude-code",
                "session_id": "abc",
                "session_log_handle": "urn:eawf:v1:session-log:claude-code:abc",
                "started_at": _now().isoformat(),
                "bogus": True,
            }
        },
    }
    with pytest.raises(ValidationError, match=r"extra_forbidden|extra"):
        Wave.model_validate(payload)
