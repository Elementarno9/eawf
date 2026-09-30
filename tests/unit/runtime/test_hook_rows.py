"""SURF-102: every hook event the Claude plugin declares writes a row or decides.

Each event the packaged manifest subscribes to is fired with the payload the host
sends, and at least one of its handlers must reach a daemon write verb, so the
daemon either writes a durable row or answers with its typed refusal, or must
hand the host a typed decision about what it was asked. An event whose handlers
all skip on a Claude payload would be a declared no-op.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.hooks.host_lane import HOST_SKILL_HOOK
from eawf.runtime.hooks.runner import HookRunner, register_runtime_capture_hooks
from eawf.runtime.runtimes.claude.hook_map import handler_backed_plugin_hooks

pytestmark = pytest.mark.unit

_AT: Final = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

#: The daemon verbs that write a durable row or refuse typed.
_WRITE_VERBS: Final = frozenset(
    {
        "runtime.capture",
        "runtime.host.context.boundary",
        "runtime.host.error.observe",
        "runtime.host.file_edit.after",
        "runtime.host.file_edit.before",
        "runtime.host.permission.request",
        "runtime.host.question.answer",
        "runtime.host.question.raise",
        "runtime.host.subagent.start",
        "runtime.host.subagent.stop",
        "runtime.host.tool.observe",
    }
)

#: What the host sends for each event, reduced to the fields the handlers read.
_PAYLOADS: Final[dict[HookEventType, dict[str, Any]]] = {
    HookEventType.SESSION_START: {"session_id": "host-1", "source": "startup"},
    HookEventType.SESSION_END: {
        "session_id": "host-1",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "model": {"id": "claude-opus-4-8"},
    },
    HookEventType.SUBAGENT_START: {"session_id": "host-1", "agent_id": "agent-1"},
    HookEventType.SUBAGENT_STOP: {"session_id": "host-1", "agent_id": "agent-1"},
    HookEventType.PRE_COMPACT: {"session_id": "host-1", "trigger": "auto"},
    HookEventType.PERMISSION_REQUEST: {
        "session_id": "host-1",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    },
    HookEventType.PRE_TOOL_USE: {
        "session_id": "host-1",
        "tool_name": "Read",
        "tool_use_id": "toolu_1",
        "tool_input": {"file_path": "README.md"},
    },
    HookEventType.POST_TOOL_USE: {
        "session_id": "host-1",
        "tool_name": "Read",
        "tool_use_id": "toolu_1",
        "tool_input": {"file_path": "README.md"},
        "tool_response": {"content": "text"},
    },
    HookEventType.POST_TOOL_USE_FAILURE: {
        "session_id": "host-1",
        "tool_name": "Read",
        "tool_use_id": "toolu_1",
        "tool_input": {"file_path": "README.md"},
        "error": "no such file",
    },
    HookEventType.USER_PROMPT_SUBMIT: {"session_id": "host-1", "prompt": "/research x --bogus"},
}

#: Events whose handler answers the host with a decision rather than writing a row.
_DECIDING: Final = frozenset({HookEventType.USER_PROMPT_SUBMIT})


class _Client:
    """Daemon-client stand-in recording each verb and answering like it."""

    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._sink.append(method)
        return {
            "run_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000011",
            "permission": {"key": "PRM-0001", "resolution": {"decision": "approved"}},
            "question_refs": [],
            "drift": [],
            "restatement": "anchors",
        }


def test_surf_102_every_declared_event_has_a_payload_here() -> None:
    declared = {spec.event_type for spec in handler_backed_plugin_hooks()}
    assert declared == set(_PAYLOADS)


@pytest.mark.parametrize(
    "event_type", sorted({spec.event_type for spec in handler_backed_plugin_hooks()})
)
def test_surf_102_each_declared_event_writes_through_a_daemon_verb(
    event_type: HookEventType, tmp_path: Path
) -> None:
    calls: list[str] = []
    runner = HookRunner()
    register_runtime_capture_hooks(
        runner, daemon_client_factory=lambda: _Client(calls), repo_root=tmp_path
    )
    event = HookEvent(
        event_type=event_type,
        runtime="claude",
        occurred_at=_AT,
        payloads={"claude_code": _PAYLOADS[event_type]},
    )

    results = runner.run_event(event)

    if event_type in _DECIDING:
        (decision,) = (result for result in results if result.name == HOST_SKILL_HOOK)
        assert decision.block
        assert "unknown_argument" in decision.output
        return
    assert calls, f"{event_type.value} reached no daemon verb"
    assert set(calls) <= _WRITE_VERBS
