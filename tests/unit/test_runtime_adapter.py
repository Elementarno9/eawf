"""Unit tests for :class:`RuntimeAdapter` Protocol + 3 implementations.

Pins C07a §5.1 (Protocol shape), §5.5 (closed error-class set +
per-runtime parse rules), and §6 F1 (Protocol-mismatch detection via
``isinstance`` runtime-checkable).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import get_args

import pytest

from eawf.runtimes.adapter import (
    ALL_ERROR_CLASSES,
    DispatchEventKind,
    ErrorClass,
    RuntimeAdapter,
    SessionResumeFailedError,
    emit_runtime_event,
)
from eawf.runtimes.claude.adapter import ClaudeAdapter
from eawf.runtimes.codex.adapter import CodexAdapter
from eawf.runtimes.opencode.adapter import OpenCodeAdapter
from eawf.state.enums import WaveStatus
from eawf.state.models import SessionAttempt, Wave

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_wave(
    wave_id: str = "P25-I01-W10", sessions: dict[int, SessionAttempt] | None = None
) -> Wave:
    return Wave(
        id=wave_id,
        iter_id="P25-I01",
        title="t",
        status=WaveStatus.IN_PROGRESS,
        opened_at=datetime(2026, 5, 19, tzinfo=UTC),
        sessions=sessions or {},
    )


# ---------------------------------------------------------------------------
# ErrorClass closed set
# ---------------------------------------------------------------------------


def test_error_class_literal_is_closed_5_tuple() -> None:
    """§5.5 — exactly 5 canonical class strings."""
    args = get_args(ErrorClass)
    assert set(args) == {
        "RUNTIME_RATE_LIMIT",
        "RUNTIME_SERVER_ERROR",
        "RUNTIME_TIMEOUT",
        "RUNTIME_API_ERROR",
        "RUNTIME_AUTH_ERROR",
    }


def test_all_error_classes_tuple_matches_literal() -> None:
    """``ALL_ERROR_CLASSES`` mirrors the ``ErrorClass`` Literal."""
    assert set(ALL_ERROR_CLASSES) == set(get_args(ErrorClass))


def test_dispatch_event_kind_is_3_tuple() -> None:
    """Adapter-emitted event-kind subset: 3 dispatch kinds only."""
    assert set(get_args(DispatchEventKind)) == {
        "runtime_switched",
        "session_continued",
        "session_failover",
    }


# ---------------------------------------------------------------------------
# Protocol conformance — runtime_checkable isinstance() (§6 F1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "adapter_cls",
    [ClaudeAdapter, CodexAdapter, OpenCodeAdapter],
    ids=["claude-code", "codex", "opencode"],
)
def test_adapter_is_runtime_adapter_instance(adapter_cls: type) -> None:
    """All 3 concrete adapters pass ``isinstance(_, RuntimeAdapter)``."""
    instance = adapter_cls()
    assert isinstance(instance, RuntimeAdapter)


def test_protocol_rejects_partial_class() -> None:
    """A class missing a Protocol method fails the isinstance gate (F1)."""

    class Incomplete:
        id = "incomplete"
        cli_binary = "x"
        accepts_continue = False
        supports_cache_control = False
        error_classes_emitted: tuple[ErrorClass, ...] = ()
        # missing open_session / continue_session / etc.

    assert not isinstance(Incomplete(), RuntimeAdapter)


# ---------------------------------------------------------------------------
# Per-runtime identity + capability flags
# ---------------------------------------------------------------------------


def test_claude_adapter_identity() -> None:
    a = ClaudeAdapter()
    assert a.id == "claude-code"
    assert a.cli_binary == "claude"
    assert a.accepts_continue is True
    assert a.supports_cache_control is True
    assert a.supports_continue() is True


def test_codex_adapter_identity() -> None:
    a = CodexAdapter()
    assert a.id == "codex"
    assert a.cli_binary == "codex"
    assert a.accepts_continue is True
    assert a.supports_cache_control is False
    assert a.supports_continue() is True


def test_opencode_adapter_identity() -> None:
    """F12 in §6: OpenCode v0.3 ships ``supports_continue=False``."""
    a = OpenCodeAdapter()
    assert a.id == "opencode"
    assert a.cli_binary == "opencode"
    assert a.accepts_continue is False
    assert a.supports_cache_control is False
    assert a.supports_continue() is False


# ---------------------------------------------------------------------------
# parse_error closed-set + per-runtime rules (§5.5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("adapter_cls", [ClaudeAdapter, CodexAdapter, OpenCodeAdapter])
def test_parse_error_rate_limit(adapter_cls: type) -> None:
    a = adapter_cls()
    assert a.parse_error(2, b"429 Too Many Requests") == "RUNTIME_RATE_LIMIT"
    assert a.parse_error(2, b"error: rate_limit hit") == "RUNTIME_RATE_LIMIT"


@pytest.mark.parametrize("adapter_cls", [ClaudeAdapter, CodexAdapter, OpenCodeAdapter])
def test_parse_error_auth(adapter_cls: type) -> None:
    a = adapter_cls()
    assert a.parse_error(2, b"401 Unauthorized") == "RUNTIME_AUTH_ERROR"
    assert a.parse_error(2, b"invalid_api_key") == "RUNTIME_AUTH_ERROR"


def test_parse_error_codex_specific_auth_phrase() -> None:
    """§5.5 row 5: Codex adds ``chatgpt subscription expired``."""
    a = CodexAdapter()
    assert a.parse_error(2, b"chatgpt subscription expired") == "RUNTIME_AUTH_ERROR"


@pytest.mark.parametrize("adapter_cls", [ClaudeAdapter, CodexAdapter, OpenCodeAdapter])
def test_parse_error_server(adapter_cls: type) -> None:
    a = adapter_cls()
    assert a.parse_error(1, b"500 Internal Server Error") == "RUNTIME_SERVER_ERROR"
    assert a.parse_error(1, b"503 service unavailable") == "RUNTIME_SERVER_ERROR"


@pytest.mark.parametrize("adapter_cls", [ClaudeAdapter, CodexAdapter, OpenCodeAdapter])
def test_parse_error_timeout(adapter_cls: type) -> None:
    a = adapter_cls()
    assert a.parse_error(124, b"") == "RUNTIME_TIMEOUT"
    assert a.parse_error(-15, b"") == "RUNTIME_TIMEOUT"
    assert a.parse_error(1, b"deadline_exceeded") == "RUNTIME_TIMEOUT"


@pytest.mark.parametrize("adapter_cls", [ClaudeAdapter, CodexAdapter, OpenCodeAdapter])
def test_parse_error_generic_api(adapter_cls: type) -> None:
    a = adapter_cls()
    assert a.parse_error(1, b"400 Bad Request") == "RUNTIME_API_ERROR"
    # Empty stderr falls back to RUNTIME_API_ERROR (closed-set default).
    assert a.parse_error(1, b"") == "RUNTIME_API_ERROR"


def test_parse_error_auth_beats_server() -> None:
    """Auth verdict takes precedence over a generic 5xx hit per §5.5 ordering."""
    a = ClaudeAdapter()
    assert a.parse_error(2, b"401 over 500") == "RUNTIME_AUTH_ERROR"


@pytest.mark.parametrize("adapter_cls", [ClaudeAdapter, CodexAdapter, OpenCodeAdapter])
def test_parse_error_returns_value_in_closed_set(adapter_cls: type) -> None:
    """parse_error MUST return one of the 5 closed-set strings."""
    a = adapter_cls()
    for stderr in [b"", b"random text", b"400", b"429", b"401", b"500"]:
        out = a.parse_error(1, stderr)
        assert out in ALL_ERROR_CLASSES


# ---------------------------------------------------------------------------
# open_session / continue_session row construction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "adapter_cls",
    [ClaudeAdapter, CodexAdapter, OpenCodeAdapter],
    ids=["claude-code", "codex", "opencode"],
)
def test_open_session_constructs_session_attempt(adapter_cls: type) -> None:
    a = adapter_cls()
    wave = _make_wave()
    attempt = asyncio.run(a.open_session(wave, "hello"))
    assert isinstance(attempt, SessionAttempt)
    assert attempt.runtime == a.id
    assert attempt.attempt == 1
    assert attempt.session_id  # non-empty UUID
    assert attempt.session_log_handle.startswith(f"urn:eawf:v1:session-log:{a.id}:")


def test_open_session_increments_attempt_counter() -> None:
    """``attempt`` is ``max(existing) + 1`` when prior attempts exist."""
    a = ClaudeAdapter()
    prior = SessionAttempt(
        attempt=3,
        runtime="claude-code",
        session_id="s-prior",
        session_log_handle="urn:eawf:v1:session-log:claude-code:s-prior",
        started_at=datetime(2026, 5, 19, tzinfo=UTC),
    )
    wave = _make_wave(sessions={3: prior})
    out = asyncio.run(a.open_session(wave, "hello"))
    assert out.attempt == 4


@pytest.mark.parametrize("adapter_cls", [ClaudeAdapter, CodexAdapter])
def test_continue_session_empty_id_raises(adapter_cls: type) -> None:
    a = adapter_cls()
    with pytest.raises(SessionResumeFailedError):
        asyncio.run(a.continue_session("", "hello"))


def test_opencode_continue_session_always_raises_v03() -> None:
    """F12: OpenCode adapter does not support continue in v0.3."""
    a = OpenCodeAdapter()
    with pytest.raises(SessionResumeFailedError):
        asyncio.run(a.continue_session("some-session-id", "hello"))


def test_claude_continue_session_returns_attempt() -> None:
    a = ClaudeAdapter()
    out = asyncio.run(a.continue_session("s-prior", "hello"))
    assert out.session_id == "s-prior"
    assert out.runtime == "claude-code"


# ---------------------------------------------------------------------------
# session_log_handle — opaque URN, no on-disk path leak (rule 16)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "adapter_cls",
    [ClaudeAdapter, CodexAdapter, OpenCodeAdapter],
    ids=["claude-code", "codex", "opencode"],
)
def test_session_log_handle_is_opaque_urn(adapter_cls: type) -> None:
    a = adapter_cls()
    handle = a.session_log_handle("abc-123")
    assert handle.startswith("urn:eawf:v1:session-log:")
    # Rule 16: handle MUST NOT carry an absolute filesystem path.
    assert "/" not in handle.split("session-log:")[1]


# ---------------------------------------------------------------------------
# emit_runtime_event — canonical Event construction (D14 / XB07)
# ---------------------------------------------------------------------------


def test_emit_runtime_event_builds_canonical_event() -> None:
    ev = emit_runtime_event(
        event_id="e-2026-05-19-0001-runtime_switched",
        scope_id="P25-I01-W10",
        occurred_at=datetime(2026, 5, 19, tzinfo=UTC),
        event_kind="runtime_switched",
        actor="claude-code",
        command="agent.dispatch",
        args_hash="hash123",
        status="ok",
        message="switched from codex to claude-code",
        error_class="RUNTIME_RATE_LIMIT",
        extras={"from_runtime": "codex", "to_runtime": "claude-code"},
    )
    assert ev.id == "e-2026-05-19-0001-runtime_switched"
    assert ev.scope_id == "P25-I01-W10"
    assert ev.payload.event_kind == "runtime_switched"
    assert ev.payload.event_type == "runtime_switched"
    assert ev.payload.error_class == "RUNTIME_RATE_LIMIT"
    assert ev.payload.extras == {"from_runtime": "codex", "to_runtime": "claude-code"}


def test_emit_runtime_event_round_trips_through_json() -> None:
    ev = emit_runtime_event(
        event_id="e-2026-05-19-0002-session_continued",
        scope_id="P25-I01-W10",
        occurred_at=datetime(2026, 5, 19, tzinfo=UTC),
        event_kind="session_continued",
        actor="claude-code",
        command="agent.dispatch",
        args_hash="h",
        status="ok",
        message="continued",
    )
    s = ev.model_dump_json()
    # Re-validate through the canonical Event model (D14 / XB07 — single source of truth).
    from eawf.store.kinds.event import Event

    reloaded = Event.model_validate_json(s)
    assert reloaded == ev
