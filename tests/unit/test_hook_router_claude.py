"""Unit tests for :func:`route_claude_payload` (Phase 4 W04 acceptance §2).

Behaviour pinned:

- Recognised Claude payloads → correct :class:`HookEventType` + the raw
  payload preserved verbatim under ``payloads["claude_code"]``.
- Unknown / malformed payloads → ``None`` + a single
  ``logging.warning(...)`` entry. The router never raises.
- ``runtime`` is always ``"claude"`` on the success path.
"""

from __future__ import annotations

import logging

import pytest

from eawf.hooks.event import HookEvent, HookEventType
from eawf.runtimes.claude.hooks_router import route_claude_payload


def test_router_session_start_maps_to_session_start() -> None:
    event = route_claude_payload(
        {"hook_event_name": "SessionStart", "session_id": "abc", "cwd": "/tmp/x"}
    )
    assert isinstance(event, HookEvent)
    assert event.event_type == HookEventType.SESSION_START
    assert event.runtime == "claude"
    assert event.scope_id == "/tmp/x"
    assert event.payloads["claude_code"]["hook_event_name"] == "SessionStart"


def test_router_session_end_via_stop_maps_to_session_end() -> None:
    event = route_claude_payload({"hook_event_name": "Stop", "session_id": "abc"})
    assert isinstance(event, HookEvent)
    assert event.event_type == HookEventType.SESSION_END


def test_router_explicit_session_end_maps_to_session_end() -> None:
    event = route_claude_payload({"hook_event_name": "SessionEnd", "session_id": "abc"})
    assert isinstance(event, HookEvent)
    assert event.event_type == HookEventType.SESSION_END


def test_router_pretooluse_bash_git_commit_maps_to_pre_commit() -> None:
    event = route_claude_payload(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m 'hello'"},
        }
    )
    assert isinstance(event, HookEvent)
    assert event.event_type == HookEventType.PRE_COMMIT


def test_router_posttooluse_bash_git_push_maps_to_post_push() -> None:
    event = route_claude_payload(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git push origin main"},
        }
    )
    assert isinstance(event, HookEvent)
    assert event.event_type == HookEventType.POST_PUSH


def test_router_unknown_hook_event_returns_none_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="eawf.runtimes.claude.hooks_router")
    out = route_claude_payload({"hook_event_name": "PromptSubmit", "session_id": "abc"})
    assert out is None
    assert any("unknown Claude hook_event_name" in r.message for r in caplog.records)


def test_router_missing_hook_event_name_returns_none_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="eawf.runtimes.claude.hooks_router")
    out = route_claude_payload({"session_id": "abc"})
    assert out is None
    assert any("missing or non-string hook_event_name" in r.message for r in caplog.records)


def test_router_pretooluse_non_bash_returns_none_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="eawf.runtimes.claude.hooks_router")
    out = route_claude_payload(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/x.txt"},
        }
    )
    assert out is None
    assert any("not mapped" in r.message for r in caplog.records)


def test_router_pretooluse_bash_unrelated_command_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="eawf.runtimes.claude.hooks_router")
    out = route_claude_payload(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
        }
    )
    assert out is None
    assert any("not mapped" in r.message for r in caplog.records)


def test_router_does_not_raise_on_completely_garbage_payload() -> None:
    """Even completely garbage payloads return ``None`` instead of raising."""
    # Lists, ints, malformed types — all return None per design spec §3.3.
    for garbage in [
        {},
        {"hook_event_name": ""},
        {"hook_event_name": None},
        {"hook_event_name": 7},
        {"hook_event_name": "PreToolUse", "tool_input": "not-a-dict"},
    ]:
        assert route_claude_payload(garbage) is None  # type: ignore[arg-type]
