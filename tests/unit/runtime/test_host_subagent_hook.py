"""The hook handlers the plugin declares are registered, observable and never idle.

SURF-055: every hook handler is a real runner registration, checked against the
declarations rather than assumed.
SURF-076 and SURF-102: every event a packaged plugin declares has a registered handler,
and the subagent pair's handler writes a durable Run through the daemon or answers with
the daemon's typed refusal -- it never exits having done nothing. SURF-054 and SURF-097:
the host's subagent start and stop events reach the adoption verbs for both harnesses,
so harness-side fan-out lands in the daemon's Run register.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.hooks.runner import (
    HookRunner,
    adopt_host_subagent,
    register_runtime_capture_hooks,
    registered_handler_event_types,
)
from eawf.runtime.runtimes.claude.hook_map import (
    build_plugin_hooks_json,
    handler_backed_plugin_hooks,
)
from eawf.runtime.runtimes.claude.hooks_router import route_claude_payload
from eawf.runtime.runtimes.codex.hook_map import CODEX_HOOK_EVENT_TYPES

pytestmark = pytest.mark.unit

AT: Final = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
SUBAGENT_EVENTS: Final = (HookEventType.SUBAGENT_START, HookEventType.SUBAGENT_STOP)


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
        return {"run_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000011"}


def recording(error: Exception | None = None) -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    sink: list[tuple[str, dict[str, Any]]] = []
    return sink, lambda: _Client(sink, error)


def hook_event(event_type: HookEventType, runtime: str, **payload: Any) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        scope_id="",
        command="",
        args={},
        runtime=runtime,  # type: ignore[arg-type]
        occurred_at=AT,
        payloads={event_type.value: payload} if payload else {},
    )


# ---- SURF-054 / SURF-097: both harnesses reach the adoption verbs ----------------


@pytest.mark.parametrize(("runtime", "harness"), [("claude", "claude-code"), ("codex", "codex")])
def test_surf_097_subagent_start_reaches_the_adoption_verb(
    runtime: str, harness: str, tmp_path: Path
) -> None:
    sink, factory = recording()
    event = hook_event(
        HookEventType.SUBAGENT_START, runtime, agent_id="agent-1", session_id="host-1"
    )

    result = adopt_host_subagent(event, daemon_client_factory=factory, repo_root=tmp_path)

    assert sink == [
        (
            "runtime.host.subagent.start",
            {
                "harness": harness,
                "agent_id": "agent-1",
                "repo_root": str(tmp_path),
                "host_session_id": "host-1",
            },
        )
    ]
    assert result.block is False
    assert (
        result.output
        == "runtime.host_subagent ok run=eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000011"
    )


def test_surf_054_subagent_stop_carries_the_transcript_path(tmp_path: Path) -> None:
    sink, factory = recording()
    event = hook_event(
        HookEventType.SUBAGENT_STOP,
        "claude",
        agent_id="agent-1",
        agent_transcript_path=str(tmp_path / "agent-1.jsonl"),
    )

    adopt_host_subagent(event, daemon_client_factory=factory, repo_root=tmp_path)

    ((method, params),) = sink
    assert method == "runtime.host.subagent.stop"
    assert params["transcript_path"] == str(tmp_path / "agent-1.jsonl")
    assert "host_session_id" not in params


def test_surf_054_start_never_sends_a_transcript_path(tmp_path: Path) -> None:
    sink, factory = recording()
    event = hook_event(
        HookEventType.SUBAGENT_START, "claude", agent_id="a", agent_transcript_path="t.jsonl"
    )

    adopt_host_subagent(event, daemon_client_factory=factory, repo_root=tmp_path)

    assert "transcript_path" not in sink[0][1]


def test_surf_054_repo_root_defaults_to_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    sink, factory = recording()

    adopt_host_subagent(
        hook_event(HookEventType.SUBAGENT_START, "claude", agent_id="a"),
        daemon_client_factory=factory,
    )

    assert sink[0][1]["repo_root"] == str(Path.cwd())


@pytest.mark.parametrize(
    ("runtime", "payload", "reason"),
    [
        pytest.param("opencode", {"agent_id": "a"}, "opencode spawns no subagent", id="opencode"),
        pytest.param("generic", {"agent_id": "a"}, "generic spawns no subagent", id="generic"),
        pytest.param("claude", {}, "missing agent_id", id="no-payload"),
        pytest.param("claude", {"agent_id": ""}, "missing agent_id", id="empty-agent-id"),
        pytest.param("claude", {"agent_id": 7}, "missing agent_id", id="agent-id-not-a-string"),
    ],
)
def test_surf_102_unadoptable_event_names_why_and_calls_nothing(
    runtime: str, payload: dict[str, Any], reason: str
) -> None:
    sink, factory = recording()

    result = adopt_host_subagent(
        hook_event(HookEventType.SUBAGENT_START, runtime, **payload),
        daemon_client_factory=factory,
    )

    assert sink == []
    assert result.output == f"runtime.host_subagent skipped: {reason}"


def test_surf_102_daemon_refusal_is_answered_not_raised(tmp_path: Path) -> None:
    refusal = RuntimeError("validation_failed: native_authority_required: epoch 1")
    _sink, factory = recording(error=refusal)

    result = adopt_host_subagent(
        hook_event(HookEventType.SUBAGENT_START, "claude", agent_id="a"),
        daemon_client_factory=factory,
        repo_root=tmp_path,
    )

    assert result.block is False
    assert "native_authority_required" in result.output


# ---- SURF-055: registered and observable ------------------------------------------


def test_surf_055_subagent_handlers_are_real_runner_registrations() -> None:
    runner = HookRunner()
    register_runtime_capture_hooks(runner)

    for event_type in SUBAGENT_EVENTS:
        assert "runtime.host_subagent" in {name for name, _hook in runner.hooks_for(event_type)}
    assert set(SUBAGENT_EVENTS) <= registered_handler_event_types()


def test_surf_055_router_maps_both_subagent_events() -> None:
    for name, expected in (
        ("SubagentStart", HookEventType.SUBAGENT_START),
        ("SubagentStop", HookEventType.SUBAGENT_STOP),
    ):
        routed = route_claude_payload({"hook_event_name": name, "agent_id": "a"})
        assert routed is not None
        assert routed.event_type is expected


# ---- SURF-076 / SURF-102: every declared event is handled -------------------------


def test_surf_076_every_claude_manifest_event_has_a_registered_handler() -> None:
    declared = {spec.event_type for spec in handler_backed_plugin_hooks()}
    assert set(SUBAGENT_EVENTS) <= declared
    assert declared <= registered_handler_event_types()
    manifest_events = set(build_plugin_hooks_json()["hooks"])
    assert manifest_events == {
        "SessionStart",
        "Stop",
        "SubagentStart",
        "SubagentStop",
        "PermissionRequest",
    }


def test_surf_076_every_codex_manifest_event_has_a_registered_handler() -> None:
    assert set(CODEX_HOOK_EVENT_TYPES) <= registered_handler_event_types()


@pytest.mark.parametrize("event_type", SUBAGENT_EVENTS)
@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_surf_102_subagent_handler_writes_through_the_daemon(
    event_type: HookEventType, runtime: str, tmp_path: Path
) -> None:
    sink, factory = recording()
    runner = HookRunner()
    register_runtime_capture_hooks(runner, daemon_client_factory=factory, repo_root=tmp_path)

    results = runner.run_event(hook_event(event_type, runtime, agent_id="a", session_id="s"))

    adoption = [r for r in results if r.name == "runtime.host_subagent"]
    assert [r.output.split(" run=")[0] for r in adoption] == ["runtime.host_subagent ok"]
    assert any(method.startswith("runtime.host.subagent.") for method, _ in sink)
