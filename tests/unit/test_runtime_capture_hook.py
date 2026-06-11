"""Tests for the SESSION_END runtime capture hook."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.hooks.runner import HookRunner, register_runtime_capture_hooks


def _event(payload: dict[str, Any]) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.SESSION_END,
        scope_id="P30-I05-W03",
        command="",
        args={},
        runtime="claude",
        occurred_at=datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC),
        payloads={"claude_code": payload},
    )


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self) -> _RecordingClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        return {"ok": True}


class _FailingClient:
    def __enter__(self) -> _FailingClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(f"{method} unavailable")


def _runtime_payload() -> dict[str, Any]:
    return {
        "hook_event_name": "Stop",
        "session_id": "session-1",
        "cost": {
            "api_duration_ms": 17000,
            "total_duration_ms": 21000,
            "cost_usd": 0.42,
        },
        "context_window": {
            "current_usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 5,
                "cache_read_input_tokens": 7,
            }
        },
    }


def test_session_end_capture_hook_invokes_runtime_capture_rpc() -> None:
    client = _RecordingClient()
    runner = HookRunner()
    register_runtime_capture_hooks(
        runner,
        daemon_client_factory=lambda: client,
        repo_root=Path("workspace"),
    )

    results = runner.run_event(_event(_runtime_payload()))

    assert [result.name for result in results] == ["runtime.capture"]
    assert results[0].block is False
    assert results[0].output == "runtime.capture ok"
    assert client.calls == [
        (
            "runtime.capture",
            {
                "api_duration_ms": 17000,
                "total_duration_ms": 21000,
                "cost_usd": "0.42",
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 5,
                "cache_read_input_tokens": 7,
                # W19 stamps the parser harness attribution onto every capture;
                # this payload carries no model block so ``model`` stays None.
                "harness": "claude-code",
                "model": None,
                "session_id": "session-1",
                "captured_at": "2026-06-10T12:00:00+00:00",
                "repo_root": "workspace",
            },
        )
    ]


def test_session_end_capture_hook_missing_cost_makes_no_rpc_call() -> None:
    client = _RecordingClient()
    runner = HookRunner()
    register_runtime_capture_hooks(runner, daemon_client_factory=lambda: client)

    results = runner.run_event(_event({"hook_event_name": "Stop", "session_id": "session-1"}))

    assert client.calls == []
    assert results[0].block is False
    assert results[0].output == "runtime.capture skipped: no cost block"


def test_session_end_capture_hook_daemon_error_is_non_blocking() -> None:
    runner = HookRunner()
    register_runtime_capture_hooks(runner, daemon_client_factory=_FailingClient)

    results = runner.run_event(_event(_runtime_payload()))

    assert len(results) == 1
    assert results[0].block is False
    assert "runtime.capture" in results[0].output
    assert "unavailable" in results[0].output
