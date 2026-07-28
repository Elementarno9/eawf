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


def _codex_event(event_type: HookEventType, payload: dict[str, Any]) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        scope_id="",
        command="",
        args={},
        runtime="codex",
        occurred_at=datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC),
        payloads={event_type.value: payload},
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
                # The statusline declares its OWN measure (P30-I25-W45): its cost
                # block is a different quantity from the transcript's per-turn
                # duration, so a flip between the two sources must read as a change
                # of measure rather than as work.
                "measure_version": 101,
                "session_id": "session-1",
                "captured_at": "2026-06-10T12:00:00+00:00",
                "repo_root": "workspace",
            },
        )
    ]


def test_session_end_capture_hook_without_counters_makes_no_rpc_call() -> None:
    client = _RecordingClient()
    runner = HookRunner()
    register_runtime_capture_hooks(runner, daemon_client_factory=lambda: client)

    results = runner.run_event(_event({"hook_event_name": "Stop", "session_id": "session-1"}))

    assert client.calls == []
    assert results[0].block is False
    assert results[0].output == "runtime.capture skipped: no usable counters"


def test_session_end_capture_hook_daemon_error_is_non_blocking() -> None:
    runner = HookRunner()
    register_runtime_capture_hooks(runner, daemon_client_factory=_FailingClient)

    results = runner.run_event(_event(_runtime_payload()))

    assert len(results) == 1
    assert results[0].block is False
    assert "runtime.capture" in results[0].output
    assert "unavailable" in results[0].output


def test_codex_session_start_binds_provider_session() -> None:
    client = _RecordingClient()
    runner = HookRunner()
    register_runtime_capture_hooks(runner, daemon_client_factory=lambda: client)

    results = runner.run_event(
        _codex_event(
            HookEventType.SESSION_START,
            {
                "hook_event_name": "SessionStart",
                "session_id": "provider-session-1",
            },
        )
    )

    assert len(results) == 1
    assert results[0].name == "runtime.codex_lifecycle"
    assert client.calls[0][0] == "runtime.codex_lifecycle"
    assert client.calls[0][1]["event_type"] == "session_start"
    assert client.calls[0][1]["provider_session_id"] == "provider-session-1"


def test_codex_subagent_stop_forwards_exact_nullable_usage(tmp_path: Path) -> None:
    transcript = tmp_path / "agent-rollout.jsonl"
    transcript.write_text(
        "\n".join(
            [
                '{"type":"session_meta","payload":{"id":"agent-1"}}',
                (
                    '{"type":"event_msg","payload":{"type":"token_count","info":'
                    '{"total_token_usage":{"input_tokens":100,'
                    '"cached_input_tokens":20,"output_tokens":30,'
                    '"reasoning_output_tokens":5}}}}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    client = _RecordingClient()
    runner = HookRunner()
    register_runtime_capture_hooks(runner, daemon_client_factory=lambda: client)

    runner.run_event(
        _codex_event(
            HookEventType.SUBAGENT_STOP,
            {
                "hook_event_name": "SubagentStop",
                "session_id": "provider-session-1",
                "agent_id": "agent-1",
                "agent_transcript_path": str(transcript),
            },
        )
    )

    method, params = client.calls[0]
    assert method == "runtime.codex_lifecycle"
    assert params["event_type"] == "subagent_stop"
    assert params["measurement_quality"] == "exact"
    assert params["measurement_status"] == "usage_observed"
    assert params["measurement_reason"] is None
    assert params["counters"]["input_tokens"] == 80
    assert params["counters"]["output_tokens"] == 30
    assert params["counters"]["cache_read_input_tokens"] == 20
    assert params["counters"]["cache_creation_input_tokens"] is None
    assert params["counters"]["cost_usd"] is None


def test_codex_subagent_stop_missing_transcript_is_explicitly_unavailable() -> None:
    client = _RecordingClient()
    runner = HookRunner()
    register_runtime_capture_hooks(runner, daemon_client_factory=lambda: client)

    runner.run_event(
        _codex_event(
            HookEventType.SUBAGENT_STOP,
            {
                "hook_event_name": "SubagentStop",
                "session_id": "provider-session-1",
                "agent_id": "agent-1",
            },
        )
    )

    params = client.calls[0][1]
    assert "counters" not in params
    assert params["measurement_quality"] == "unavailable"
    assert params["measurement_status"] == "usage_unavailable"
    assert params["measurement_reason"] == "missing_transcript_path"


def test_codex_session_end_captures_only_correlated_wave(tmp_path: Path) -> None:
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        "\n".join(
            [
                '{"type":"session_meta","payload":{"id":"provider-session-1"}}',
                (
                    '{"type":"event_msg","payload":{"type":"token_count","info":'
                    '{"total_token_usage":{"input_tokens":100,'
                    '"cached_input_tokens":20,"output_tokens":30}}}}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    class _CorrelatingClient(_RecordingClient):
        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((method, params))
            if method == "runtime.codex_lifecycle":
                return {
                    "correlated": True,
                    "wave_id": "P30-I05-W03",
                }
            return {"ok": True}

    client = _CorrelatingClient()
    runner = HookRunner()
    register_runtime_capture_hooks(runner, daemon_client_factory=lambda: client)
    results = runner.run_event(
        _codex_event(
            HookEventType.SESSION_END,
            {
                "hook_event_name": "SessionEnd",
                "session_id": "provider-session-1",
                "transcript_path": str(transcript),
            },
        )
    )

    assert results[0].output.endswith("runtime.capture ok")
    assert [method for method, _params in client.calls] == [
        "runtime.codex_lifecycle",
        "runtime.capture",
    ]
    capture_params = client.calls[1][1]
    assert capture_params["wave_id"] == "P30-I05-W03"
    assert capture_params["harness"] == "codex"
    assert capture_params["input_tokens"] == 80
