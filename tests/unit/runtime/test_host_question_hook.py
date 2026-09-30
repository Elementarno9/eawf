"""The question and tool-error hooks: what they send the daemon, and when they send nothing.

RUN-062: the host's question call and its failed calls are recorded on the Run on the
host's session. These cases pin the hook half of that seam against a recording client:
the params each verb is handed, every payload that sends nothing, and that a daemon
refusal never blocks the host. The daemon half is proven in
``tests/integration/runtime/daemon/test_host_question_producer.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.runtime.hooks.event import HookEvent, HookEventType, HookRuntime
from eawf.runtime.hooks.runner import (
    HookRunner,
    record_host_question,
    record_host_tool_error,
    register_runtime_capture_hooks,
)
from eawf.runtime.runtimes.claude.hook_map import build_plugin_hooks_json
from eawf.runtime.runtimes.host_transcript import WITHHELD_TEXT, scrubbed_words

pytestmark = pytest.mark.unit

AT: Final = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
SESSION: Final = "host-session-0001"
ASKED: Final = "Which storage backend should the cache use?"
TOOL_INPUT: Final[dict[str, Any]] = {
    "questions": [
        {
            "question": ASKED,
            "header": "Backend",
            "multiSelect": False,
            "options": [{"label": "SQLite", "description": "d"}, {"label": "Redis"}, {"x": 1}],
        },
        {"question": "   "},
        "not a question",
    ]
}


class _Client:
    """Daemon-client stand-in recording each call and answering like the verbs."""

    def __init__(self, sink: list[tuple[str, dict[str, Any]]], error: Exception | None) -> None:
        self._sink = sink
        self._error = error

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._sink.append((method, params))
        if self._error is not None:
            raise self._error
        return {"question_refs": ["eawf://WSP-MAIN/PRJ-EAWF/_/question/QST-0001"], "event_ref": "E"}


def recording(error: Exception | None = None) -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    sink: list[tuple[str, dict[str, Any]]] = []
    return sink, lambda: _Client(sink, error)


def event(
    event_type: HookEventType, payload: dict[str, Any], runtime: HookRuntime = "claude"
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        runtime=runtime,
        occurred_at=AT,
        payloads={event_type.value: {"session_id": SESSION, **payload}},
    )


def asking(event_type: HookEventType = HookEventType.PRE_TOOL_USE, **extra: Any) -> HookEvent:
    return event(
        event_type,
        {
            "tool_name": "AskUserQuestion",
            "tool_use_id": "toolu_1",
            "tool_input": TOOL_INPUT,
            **extra,
        },
    )


def test_run_062_the_question_call_is_raised_with_its_words_and_labels(tmp_path: Path) -> None:
    sink, factory = recording()

    result = record_host_question(asking(), daemon_client_factory=factory, repo_root=tmp_path)

    assert result.output == "runtime.host_question ok questions=QST-0001"
    ((method, params),) = sink
    assert method == "runtime.host.question.raise"
    assert params == {
        "harness": "claude-code",
        "host_session_id": SESSION,
        "tool_use_id": "toolu_1",
        "questions": [{"question": ASKED, "options": ["SQLite", "Redis"]}],
        "repo_root": str(tmp_path),
    }


def test_run_062_the_returned_call_carries_the_operators_answers(tmp_path: Path) -> None:
    sink, factory = recording()
    response = {"answers": {ASKED: "SQLite", "": "x", "other": 3}}

    record_host_question(
        asking(HookEventType.POST_TOOL_USE, tool_response=response),
        daemon_client_factory=factory,
        repo_root=tmp_path,
    )

    ((method, params),) = sink
    assert method == "runtime.host.question.answer"
    assert params["answers"] == {ASKED: "SQLite"}


def test_a_subagents_question_names_the_subagent(tmp_path: Path) -> None:
    sink, factory = recording()

    record_host_question(asking(agent_id="agent-7"), daemon_client_factory=factory)

    assert sink[0][1]["host_session_id"] == "agent-7"


@pytest.mark.parametrize(
    "hook_event",
    [
        event(HookEventType.PRE_TOOL_USE, {"tool_name": "Bash", "tool_use_id": "t"}),
        event(HookEventType.PRE_TOOL_USE, {"tool_name": "AskUserQuestion", "tool_input": {}}),
        event(
            HookEventType.PRE_TOOL_USE,
            {"tool_name": "AskUserQuestion", "tool_use_id": "t", "tool_input": {"questions": []}},
        ),
        event(
            HookEventType.PRE_TOOL_USE,
            {"tool_name": "AskUserQuestion", "tool_use_id": "t", "tool_input": TOOL_INPUT},
            runtime="opencode",
        ),
    ],
    ids=["another-tool", "no-call-id", "no-question", "no-subagent-runtime"],
)
def test_a_call_that_asks_nothing_sends_nothing(hook_event: HookEvent) -> None:
    sink, factory = recording()

    result = record_host_question(hook_event, daemon_client_factory=factory)

    assert sink == []
    assert "skipped" in result.output and not result.block


def test_a_daemon_refusal_never_blocks_the_host() -> None:
    _sink, factory = recording(RuntimeError("identity_not_found"))

    result = record_host_question(asking(), daemon_client_factory=factory)

    assert not result.block and "identity_not_found" in result.output


def failing(**extra: Any) -> HookEvent:
    return event(
        HookEventType.POST_TOOL_USE_FAILURE,
        {"tool_name": "Read", "tool_use_id": "toolu_2", "error": "File does not exist.", **extra},
    )


def test_run_062_a_failed_call_is_observed_with_its_error(tmp_path: Path) -> None:
    sink, factory = recording()

    result = record_host_tool_error(
        failing(is_interrupt=True), daemon_client_factory=factory, repo_root=tmp_path
    )

    assert result.output == "runtime.host_tool_error ok event=E"
    ((method, params),) = sink
    assert method == "runtime.host.error.observe"
    assert params == {
        "harness": "claude-code",
        "host_session_id": SESSION,
        "tool_use_id": "toolu_2",
        "tool_name": "Read",
        "error": "File does not exist.",
        "interrupted": True,
        "repo_root": str(tmp_path),
    }


@pytest.mark.parametrize(
    "hook_event",
    [
        failing(error="  "),
        failing(tool_use_id=None),
        failing(tool_name=""),
        event(HookEventType.POST_TOOL_USE_FAILURE, {"error": "x"}, runtime="generic"),
    ],
    ids=["blank-error", "no-call-id", "no-tool", "no-host"],
)
def test_a_failure_missing_its_facts_sends_nothing(hook_event: HookEvent) -> None:
    sink, factory = recording()

    result = record_host_tool_error(hook_event, daemon_client_factory=factory)

    assert sink == [] and "skipped" in result.output


def test_the_hooks_are_registered_and_the_plugin_subscribes_to_them() -> None:
    runner = HookRunner()
    register_runtime_capture_hooks(runner)

    names = {
        event_type: [name for name, _hook in runner.hooks_for(event_type)]
        for event_type in (
            HookEventType.PRE_TOOL_USE,
            HookEventType.POST_TOOL_USE,
            HookEventType.POST_TOOL_USE_FAILURE,
        )
    }

    assert "runtime.host_question" in names[HookEventType.PRE_TOOL_USE]
    assert "runtime.host_question" in names[HookEventType.POST_TOOL_USE]
    assert "runtime.host_tool_error" in names[HookEventType.POST_TOOL_USE_FAILURE]
    subscribed = build_plugin_hooks_json()["hooks"]
    assert {"PreToolUse", "PostToolUse", "PostToolUseFailure"} <= set(subscribed)


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("", 500, None),
        ("   ", 500, None),
        ("a", 500, "a"),
        ("abcdef", 3, "abc"),
        ("  padded  ", 500, "padded"),
        ("token ghp_" + "a" * 36, 500, WITHHELD_TEXT),
    ],
    ids=["empty", "blank", "single", "cut", "stripped", "leak"],
)
def test_scrubbed_words_cuts_or_withholds(text: str, limit: int, expected: str | None) -> None:
    assert scrubbed_words(text, limit=limit) == expected
