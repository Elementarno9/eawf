"""The permission hook: the host's held call reaches the daemon and the host is never held.

RUN-051: a provider permission is produced when a call is held pending a principal
decision. The Claude plugin subscribes to the host's ``PermissionRequest`` event, the
router maps it, the runner registers a real handler for it, and the handler records the
held call through ``runtime.host.permission.request`` -- then returns without a decision,
whatever the daemon answered, so the host goes on asking its own operator.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.hooks.runner import (
    HookRunner,
    record_host_permission,
    register_runtime_capture_hooks,
    registered_handler_event_types,
)
from eawf.runtime.runtimes.claude.hook_map import build_plugin_hooks_json
from eawf.runtime.runtimes.claude.hooks_router import route_claude_payload

pytestmark = pytest.mark.unit

AT: Final = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
METHOD: Final = "runtime.host.permission.request"
PAYLOAD: Final[dict[str, Any]] = {
    "hook_event_name": "PermissionRequest",
    "session_id": "host-session-0001",
    "cwd": "repo",
    "permission_mode": "default",
    "tool_name": "Bash",
    "tool_input": {"command": "pytest -q", "description": "Run the test suite"},
}


class _Client:
    """Daemon-client stand-in recording each call and answering like the verb."""

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
        return {"permission": {"key": "PERM-0001"}}


def recording(error: Exception | None = None) -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    sink: list[tuple[str, dict[str, Any]]] = []
    return sink, lambda: _Client(sink, error)


def permission_event(runtime: str = "claude", **payload: Any) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.PERMISSION_REQUEST,
        runtime=runtime,  # type: ignore[arg-type]
        occurred_at=AT,
        payloads={HookEventType.PERMISSION_REQUEST.value: payload} if payload else {},
    )


def test_run_051_the_host_permission_event_is_subscribed_and_routed() -> None:
    routed = route_claude_payload(dict(PAYLOAD))

    assert routed is not None
    assert routed.event_type is HookEventType.PERMISSION_REQUEST
    assert "PermissionRequest" in build_plugin_hooks_json()["hooks"]
    assert HookEventType.PERMISSION_REQUEST in registered_handler_event_types()


def test_run_051_the_runner_records_the_held_call_through_the_daemon(tmp_path: Path) -> None:
    sink, factory = recording()
    runner = HookRunner()
    register_runtime_capture_hooks(runner, daemon_client_factory=factory, repo_root=tmp_path)

    (result,) = runner.run_event(permission_event(**PAYLOAD))

    assert (result.name, result.block) == ("runtime.host_permission", False)
    assert result.output == "runtime.host_permission ok permission=PERM-0001"
    assert sink == [
        (
            METHOD,
            {
                "harness": "claude-code",
                "host_session_id": "host-session-0001",
                "tool_name": "Bash",
                "tool_input": PAYLOAD["tool_input"],
                "repo_root": str(tmp_path),
            },
        )
    ]


def test_run_051_a_call_inside_a_subagent_is_bound_to_the_subagent(tmp_path: Path) -> None:
    sink, factory = recording()

    record_host_permission(
        permission_event(**PAYLOAD, agent_id="agent-7"),
        daemon_client_factory=factory,
        repo_root=tmp_path,
    )

    assert sink[0][1]["host_session_id"] == "agent-7"


def test_run_051_a_daemon_refusal_never_blocks_the_host(tmp_path: Path) -> None:
    sink, factory = recording(RuntimeError("identity_not_found: 0 live Runs"))

    result = record_host_permission(
        permission_event(**PAYLOAD), daemon_client_factory=factory, repo_root=tmp_path
    )

    assert len(sink) == 1
    assert result.block is False
    assert "identity_not_found" in result.output


@pytest.mark.parametrize("missing", ["session_id", "tool_name"])
def test_run_051_a_request_naming_no_session_or_tool_is_skipped(
    missing: str, tmp_path: Path
) -> None:
    sink, factory = recording()
    payload = {key: value for key, value in PAYLOAD.items() if key != missing}

    result = record_host_permission(
        permission_event(**payload), daemon_client_factory=factory, repo_root=tmp_path
    )

    assert sink == []
    assert result.output == "runtime.host_permission skipped: missing session_id or tool_name"


def test_run_051_an_input_that_is_not_an_object_is_sent_empty(tmp_path: Path) -> None:
    sink, factory = recording()

    record_host_permission(
        permission_event(**{**PAYLOAD, "tool_input": "pytest -q"}),
        daemon_client_factory=factory,
        repo_root=tmp_path,
    )

    assert sink[0][1]["tool_input"] == {}


@pytest.mark.parametrize("runtime", ["opencode", "generic"])
def test_run_051_a_runtime_with_no_host_harness_is_skipped(runtime: str, tmp_path: Path) -> None:
    sink, factory = recording()

    result = record_host_permission(
        permission_event(runtime, **PAYLOAD), daemon_client_factory=factory, repo_root=tmp_path
    )

    assert sink == []
    assert result.output == f"runtime.host_permission skipped: {runtime} holds no call"
